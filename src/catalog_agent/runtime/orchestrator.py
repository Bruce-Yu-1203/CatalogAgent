"""Deterministic Orchestrator: claims tasks, routes them through the agents and commits outcomes.

It is not an agent and never calls a model. Budgets are reserved in the database before dispatch.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from catalog_agent.agents.classifier import ClassifierAgent
from catalog_agent.agents.extractor import ExtractorAgent, ExtractorInput
from catalog_agent.agents.navigator import NavigatorAgent, NavigatorInput
from catalog_agent.agents.recovery import RecoveryAgent
from catalog_agent.agents.validator import ValidatorAgent
from catalog_agent.config import AppConfig
from catalog_agent.contracts import (
    AgentResult,
    ChildTask,
    CrawlTask,
    ErrorCode,
    FetchError,
    PageSnapshot,
    PageType,
    RecoveryDecision,
    RunStatus,
    TaskStatus,
    TaskType,
)
from catalog_agent.extraction.normalize import canonicalize_url
from catalog_agent.runtime.fetcher import Fetcher, SnapshotStore
from catalog_agent.storage.repository import Repository, utcnow
from catalog_agent.telemetry import log_event

EXPECTED_KIND = {TaskType.DISCOVERY: "discovery", TaskType.LISTING: "listing", TaskType.PRODUCT: "product"}


@dataclass
class RunControl:
    deadline: float
    stop: bool = False
    stop_reason: str | None = None  # access_restricted | time_limit | pages | products
    active: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)


class Orchestrator:
    def __init__(
        self,
        cfg: AppConfig,
        repo: Repository,
        fetcher: Fetcher,
        store: SnapshotStore,
        adapter: ModuleType,
        mode: str,
        model: Any = None,
        model_unavailable_reason: str | None = None,
        url_check: Any = None,
    ):
        self.cfg = cfg
        self.repo = repo
        self.fetcher = fetcher
        self.store = store
        self.adapter = adapter
        self.mode = mode
        self.model = model
        self.model_unavailable_reason = model_unavailable_reason
        self.url_check = url_check or self._default_url_check
        self.classifier = ClassifierAgent(adapter, fallback_enabled=cfg.agents.classifier.llm_fallback_enabled)
        self.navigator = NavigatorAgent(adapter)
        self.extractor = ExtractorAgent(adapter, cfg.agents.extractor, cfg.llm)
        self.validator = ValidatorAgent(adapter, adapter.SOURCE)
        self.recovery = RecoveryAgent(
            cfg.crawler.max_attempts, cfg.crawler.backoff_base_seconds, cfg.crawler.backoff_max_seconds
        )

    def _default_url_check(self, url: str) -> str | None:
        check = getattr(self.fetcher, "check_url", None)
        if check is None:
            return None
        try:
            check(url)
        except FetchError as e:
            return "robots_disallowed" if e.code == ErrorCode.ROBOTS_DISALLOWED else "off_scope"
        return None

    # ---------------------------------------------------------------- seeding

    async def seed(self, run_id: str) -> None:
        site = self.cfg.site
        if site.sitemap_sample:
            # Seeded random sample of sitemap products: exercises extraction on categories outside the two
            # configured roots, through the permitted sitemap path. Category comes from each page's breadcrumb.
            import random

            xml = await self.fetcher.fetch_text(site.sitemap_url)
            urls = self.adapter.sitemap_product_urls(xml)
            pick = random.Random(site.sitemap_seed).sample(urls, min(site.sitemap_sample, len(urls)))
            await self.repo.bump_counters(run_id, sitemap_fetches=1, sitemap_product_urls=len(urls))
            await self.repo.enqueue(
                run_id,
                [
                    ChildTask(task_type=TaskType.PRODUCT, url=u, root="sitemap-sample", lineage=[], depth=1)
                    for u in pick
                ],
            )
            log_event(
                "sitemap_seeded",
                run_id=run_id,
                outcome="ok",
                sitemap_urls=len(urls),
                sampled=len(pick),
                seed=site.sitemap_seed,
            )
            return
        url = canonicalize_url(site.discovery_url) or site.discovery_url
        await self.repo.enqueue(
            run_id, [ChildTask(task_type=TaskType.DISCOVERY, url=url, root=None, lineage=[], depth=0)]
        )

    # -------------------------------------------------------------- main loop

    async def run(self, run_id: str, max_seconds: float) -> str:
        ctl = RunControl(deadline=time.monotonic() + max_seconds)
        workers = [asyncio.create_task(self._worker(run_id, ctl, i)) for i in range(self.cfg.crawler.concurrency)]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            ctl.stop = True
            for w in workers:
                w.cancel()
            raise
        if ctl.stop_reason != "access_restricted":
            await self.repair_suggestions(run_id)
        return await self.finalize(run_id, ctl)

    async def _worker(self, run_id: str, ctl: RunControl, worker_id: int) -> None:
        per_task_calls = self.cfg.agents.extractor.max_model_calls_per_task if self.mode == "hybrid" else 0
        idle_reason: str | None = None
        while not ctl.stop:
            if time.monotonic() >= ctl.deadline:
                ctl.stop, ctl.stop_reason = True, "time_limit"
                break
            task, reason = await self.repo.claim_next(run_id, per_task_calls)
            if task is None:
                if ctl.active > 0 or reason == "waiting":
                    due = await self.repo.next_retry_due(run_id)
                    wait = 0.5 if due is None else max(0.2, min(5.0, due - time.time()))
                    await asyncio.sleep(wait)
                    continue
                idle_reason = reason
                break
            ctl.active += 1
            try:
                await self._process(task, ctl)
            finally:
                ctl.active -= 1
        if idle_reason in ("pages", "products") and not ctl.stop_reason:
            ctl.stop_reason = idle_reason

    async def _process(self, task: CrawlTask, ctl: RunControl) -> None:
        t0 = time.perf_counter()
        max_attempts = self.cfg.crawler.max_attempts
        log_event(
            "task_claimed",
            run_id=task.run_id,
            task_id=task.task_id,
            outcome=task.task_type.value,
            url=task.url,
            attempt=task.attempts,
        )
        try:
            snapshot = await self.fetcher.fetch(
                task.url, EXPECTED_KIND[task.task_type], require_ready=task.attempts < max_attempts
            )
        except FetchError as e:
            await self._handle_error(task, ctl, e.code, str(e), e.retry_after, t0)
            return
        except Exception as e:  # unexpected fetch failure
            await self._handle_error(task, ctl, ErrorCode.INTERNAL, f"{type(e).__name__}: {e}", None, t0)
            return
        snapshot = self.store.save(snapshot)
        cres = await self._classify(task, snapshot)
        page_type = cres.output.page_type
        snap_meta = {
            "snapshot_id": snapshot.snapshot_id,
            "path": snapshot.path,
            "content_hash": snapshot.content_hash,
            "page_type": page_type.value,
        }
        log_event(
            "page_classified",
            run_id=task.run_id,
            task_id=task.task_id,
            agent="classifier",
            outcome=page_type.value,
            reason=cres.output.reason,
        )
        if page_type == PageType.BLOCKED:
            await self._handle_error(task, ctl, ErrorCode.ACCESS_DENIED, cres.output.reason, None, t0, snap_meta)
            return
        try:
            if task.task_type in (TaskType.DISCOVERY, TaskType.LISTING) and page_type in (
                PageType.CATEGORY,
                PageType.LISTING,
            ):
                await self._navigate(task, snapshot, page_type, snap_meta, t0)
            elif task.task_type == TaskType.PRODUCT and page_type == PageType.PRODUCT:
                await self._extract(task, snapshot, snap_meta, t0)
            else:
                # Unknown/unsupported page (after the optional model fallback): recorded for review.
                await self.repo.fail_task(
                    task,
                    TaskStatus.FAILED,
                    ErrorCode.UNSUPPORTED_PAGE.value,
                    f"expected {task.task_type.value}, classified {page_type.value}: {cres.output.reason}",
                    snapshot=snap_meta,
                    outcome={
                        "review": True,
                        "page_type": page_type.value,
                        "classifier_events": [e.model_dump() for e in cres.events],
                    },
                )
                log_event(
                    "task_failed",
                    run_id=task.run_id,
                    task_id=task.task_id,
                    outcome="unsupported_page",
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )
        except Exception as e:
            await self._handle_error(task, ctl, ErrorCode.INTERNAL, f"{type(e).__name__}: {e}", None, t0, snap_meta)

    async def _classify(self, task: CrawlTask, snapshot: PageSnapshot) -> AgentResult:
        """Rules first; a model fallback call is reserved in the run budget before it is made."""
        rule_type, _ = self.adapter.classify_signals(snapshot)
        allowed = False
        if (
            rule_type == PageType.UNKNOWN
            and self.model is not None
            and self.mode == "hybrid"
            and self.cfg.agents.classifier.llm_fallback_enabled
            and self.cfg.agents.classifier.max_model_calls_per_task > 0
        ):
            allowed = await self.repo.reserve_model_calls(task.run_id, 1)
        cres = await self.classifier.run(snapshot, model=self.model, model_allowed=allowed)
        b = cres.budget_used
        if allowed:
            await self.repo.add_model_usage(task.run_id, b.input_tokens, b.output_tokens)
        fb = next((e for e in cres.events if e.event == "classifier_fallback"), None)
        if fb is not None:
            log_event(
                "classifier_fallback",
                run_id=task.run_id,
                task_id=task.task_id,
                agent="classifier",
                outcome=fb.outcome,
                **fb.data,
            )
            await self.repo.bump_counters(task.run_id, **{f"classifier_fallback_{fb.outcome}": 1})
        return cres

    async def _navigate(
        self, task: CrawlTask, snapshot: PageSnapshot, page_type: PageType, snap_meta: dict, t0: float
    ) -> None:
        res: AgentResult = await self.navigator.run(
            NavigatorInput(
                task=task,
                snapshot=snapshot,
                page_type=page_type,
                scope=self.cfg.site.scope,
                configured_roots=self.cfg.site.category_roots,
                allowed_hosts=self.cfg.site.allowed_hosts,
                url_check=self.url_check,
            )
        )
        nav = res.output
        outcome = {
            "agent": "navigator",
            "children": len(nav.children),
            "pagination_stop": nav.pagination_stop,
            "rejected": nav.rejected[:50],
            "degraded_discovery": nav.degraded_discovery,
            "template": page_type.value,
            "field_failures": self.adapter.field_failures(snapshot, page_type.value),
            "events": [e.model_dump() for e in res.events],
        }
        n = await self.repo.complete_navigation(task, nav.children, outcome, snap_meta, TaskStatus.COMPLETED)
        log_event(
            "navigation_committed",
            run_id=task.run_id,
            task_id=task.task_id,
            agent="navigator",
            outcome=nav.pagination_stop or "ok",
            new_tasks=n,
            children=len(nav.children),
            rejected=len(nav.rejected),
            degraded_discovery=nav.degraded_discovery,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

    async def _extract(self, task: CrawlTask, snapshot: PageSnapshot, snap_meta: dict, t0: float) -> None:
        allowance = await self.repo.reserved_model_calls(task.task_id)
        eres = await self.extractor.run(
            ExtractorInput(
                snapshot=snapshot,
                mode=self.mode,
                model_allowance=allowance,
                model=self.model,
                model_unavailable_reason=self.model_unavailable_reason,
            )
        )
        ex = eres.output
        vres = await self.validator.run((ex.candidate, snapshot, task.run_id, task.lineage))
        val = vres.output
        status = {
            "accepted": TaskStatus.COMPLETED,
            "partial": TaskStatus.PARTIAL,
            "quarantined": TaskStatus.QUARANTINED,
        }[val.decision]
        b = eres.budget_used
        outcome = {
            "agent": "validator",
            "template": "product",
            "field_failures": self.adapter.field_failures(snapshot, "product"),
            "render_notes": self.adapter.render_notes(snapshot, "product"),
            "decision": val.decision,
            "expected_skus": val.expected_skus,
            "variants": val.observed_skus,
            "rejected_values": val.rejected_values,
            "issues": [i.model_dump() for i in vres.issues + eres.issues],
            "gaps_initial": next(
                (e.data.get("gaps") for e in eres.events if e.event == "deterministic_extraction"), []
            ),
            "gaps_remaining": [g.kind for g in ex.gaps_remaining],
            "model_used": ex.model_used,
            "patches_applied": ex.patches_applied,
            "patches_rejected": ex.patches_rejected,
            "degraded": ex.degraded,
            "model_calls": b.model_calls,
            "tool_calls": b.tool_calls,
            "input_tokens": b.input_tokens,
            "output_tokens": b.output_tokens,
            "extract_seconds": round(b.seconds, 3),
            "root": task.root,
            "events": [e.model_dump() for e in eres.events + vres.events],
        }
        product = val.product if val.decision != "quarantined" else None
        await self.repo.complete_product(
            task, product, status, outcome, snap_meta, b.model_calls, b.input_tokens, b.output_tokens
        )
        model_event = next((e for e in eres.events if e.event == "model_loop"), None)
        if model_event:
            log_event(
                "model_loop",
                run_id=task.run_id,
                task_id=task.task_id,
                agent="extractor",
                outcome=model_event.outcome,
                **model_event.data,
            )
        log_event(
            "product_committed",
            run_id=task.run_id,
            task_id=task.task_id,
            agent="validator",
            outcome=val.decision,
            variants=val.observed_skus,
            expected=val.expected_skus,
            extraction_method=product.extraction_method if product else None,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

    async def _handle_error(
        self,
        task: CrawlTask,
        ctl: RunControl,
        code: ErrorCode,
        message: str,
        retry_after: float | None,
        t0: float,
        snap_meta: dict | None = None,
    ) -> None:
        rres = await self.recovery.run((code, message, task.attempts, retry_after))
        d: RecoveryDecision = rres.output
        if d.action == "retry":
            await self.repo.fail_task(
                task,
                TaskStatus.RETRY_WAIT,
                code.value,
                message,
                next_retry_at=time.time() + d.delay_seconds,
                snapshot=snap_meta,
            )
        elif d.action == "pause_scope":
            # Stop the affected scope (the whole host in this POC); the task stays resumable. No bypass.
            await self.repo.fail_task(
                task, TaskStatus.RETRY_WAIT, code.value, message, next_retry_at=time.time(), snapshot=snap_meta
            )
            ctl.stop, ctl.stop_reason = True, "access_restricted"
        else:
            await self.repo.fail_task(
                task,
                TaskStatus.FAILED,
                code.value,
                message,
                snapshot=snap_meta,
                outcome={"recovery": d.model_dump(mode="json")},
            )
        log_event(
            "task_error",
            run_id=task.run_id,
            task_id=task.task_id,
            agent="recovery",
            outcome=d.action,
            error_code=code.value,
            message=message[:200],
            delay_s=round(d.delay_seconds, 2),
            attempt=task.attempts,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

    # ---------------------------------------------------------------- finish

    async def repair_suggestions(self, run_id: str) -> list[dict]:
        """P1 (FR-12): after repeated failures of one field on one template, ask the model for replacement
        selectors and test them on the frozen failing and passing snapshots. Report only (ADR-10)."""
        rc = self.cfg.agents.recovery
        if not (rc.repair_suggestions_enabled and self.model is not None and self.mode == "hybrid"):
            return []
        from catalog_agent.agents.recovery import suggest_repair

        groups: dict[tuple[str, str], list[dict]] = {}
        passing: dict[str, list[dict]] = {}
        for t in await self.repo.tasks(run_id, ["COMPLETED", "PARTIAL", "QUARANTINED"]):
            o = json.loads(t["outcome_json"] or "{}")
            tmpl = o.get("template")
            if not tmpl or not t["snapshot_path"]:
                continue
            fails = o.get("field_failures") or []
            for f in fails:
                groups.setdefault((tmpl, f), []).append(t)
            if not fails:
                passing.setdefault(tmpl, []).append(t)
        out: list[dict] = []
        for (tmpl, fname), tasks in sorted(groups.items()):
            if len(out) >= rc.max_suggestions_per_run or len(tasks) < rc.trigger_after_failures:
                continue
            old = self.adapter.FIELD_SELECTORS.get(tmpl, {}).get(fname)
            if not old or not await self.repo.reserve_model_calls(run_id, 3):
                continue
            failing = [self.store.load(t["snapshot_path"]) for t in tasks[:3]]
            control = [self.store.load(t["snapshot_path"]) for t in passing.get(tmpl, [])[:3]]
            sugg, b = await suggest_repair(
                self.model, fname, tmpl, old, failing, control, self.adapter.dom_outline(failing[0])
            )
            await self.repo.release_model_calls(run_id, 3 - b.model_calls)
            await self.repo.add_model_usage(run_id, b.input_tokens, b.output_tokens)
            entry = {
                "failures_observed": len(tasks),
                **sugg.model_dump(),
                "model_calls": b.model_calls,
                "tool_calls": b.tool_calls,
            }
            out.append(entry)
            log_event(
                "repair_suggestion",
                run_id=run_id,
                agent="recovery",
                outcome=sugg.status,
                field=fname,
                template=tmpl,
                candidates=len(sugg.candidates),
                model_calls=b.model_calls,
            )
        if out:
            await self.repo.update_run(run_id, metrics_json=json.dumps({"repair_suggestions": out}))
        return out

    async def finalize(self, run_id: str, ctl: RunControl) -> str:
        counts = await self.repo.task_counts(run_id)
        open_n = sum(counts.get(s, 0) for s in ("PENDING", "RUNNING", "RETRY_WAIT"))
        bad = counts.get("FAILED", 0) + counts.get("QUARANTINED", 0)
        reason = ctl.stop_reason
        if reason == "access_restricted":
            status, term = RunStatus.PAUSED, "ACCESS_RESTRICTED"
        elif reason == "time_limit":
            status, term = RunStatus.PAUSED, "TIME_LIMIT"
        elif open_n > 0:
            status = RunStatus.BUDGET_EXHAUSTED
            term = {"pages": "MAX_PAGES", "products": "MAX_PRODUCTS_PER_CATEGORY"}.get(reason or "", "BUDGET")
        elif bad:
            status, term = RunStatus.COMPLETED_WITH_ERRORS, "FRONTIER_DRAINED"
        else:
            status, term = RunStatus.COMPLETED, "FRONTIER_DRAINED"
        fields: dict[str, Any] = {"status": status.value, "termination_reason": term}
        if status in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ERRORS):
            fields["finished_at"] = utcnow()
        await self.repo.update_run(run_id, **fields)
        log_event(
            "run_finished", run_id=run_id, outcome=status.value, termination_reason=term, task_counts=json.dumps(counts)
        )
        return status.value
