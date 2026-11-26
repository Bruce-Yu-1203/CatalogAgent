"""Run lifecycle for `crawl` and `resume`: ownership, run state, budgets and extensions (Architecture §6)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from catalog_agent.adapters import safco
from catalog_agent.config import AppConfig
from catalog_agent.contracts import RunStatus
from catalog_agent.extraction.llm import ModelClient, ModelUnavailable, model_from_env
from catalog_agent.runtime.fetcher import Fetcher, PlaywrightFetcher, SnapshotStore
from catalog_agent.runtime.lock import DatabaseLock
from catalog_agent.runtime.orchestrator import Orchestrator
from catalog_agent.storage.repository import Repository
from catalog_agent.telemetry import log_event, setup_logging

ADAPTERS = {"safco": safco}


@dataclass
class Extensions:
    add_pages: int = 0
    add_model_calls: int = 0
    add_products: int = 0

    def any(self) -> bool:
        return bool(self.add_pages or self.add_model_calls or self.add_products)


def new_run_id() -> str:
    return datetime.now(UTC).strftime("run_%Y%m%d_%H%M%S")


async def playwright_fetcher(cfg: AppConfig) -> Fetcher:
    adapter = ADAPTERS[cfg.site.adapter]
    base = cfg.site.discovery_url.split("/", 3)
    f = PlaywrightFetcher(
        allowed_hosts=cfg.site.allowed_hosts,
        user_agent=cfg.site.user_agent,
        interval=cfg.crawler.request_interval_seconds,
        timeout_s=cfg.crawler.timeout_seconds,
        readiness_timeout_s=cfg.crawler.readiness_timeout_seconds,
        ready_selectors=adapter.READY_SELECTORS,
        blocked_resource_types=adapter.BLOCKED_RESOURCE_TYPES,
        blocked_url_parts=adapter.BLOCKED_URL_PARTS,
        robots_url=f"{base[0]}//{base[2]}/robots.txt",
    )
    await f.start()
    return f


FetcherFactory = Callable[[AppConfig], Awaitable[Fetcher]]
ModelFactory = Callable[[AppConfig], ModelClient]


def _env_model(cfg: AppConfig) -> ModelClient:
    return model_from_env(cfg.llm.max_output_tokens_per_call)


async def execute(
    *,
    database: str,
    cfg: AppConfig | None = None,
    mode: str | None = None,
    run_id: str | None = None,
    resume: bool = False,
    extensions: Extensions | None = None,
    fetcher_factory: FetcherFactory = playwright_fetcher,
    model_factory: ModelFactory = _env_model,
    quiet_logs: bool = False,
) -> dict[str, Any]:
    """Run or resume a crawl while holding exclusive ownership of the database."""
    extensions = extensions or Extensions()
    lock = DatabaseLock(database).acquire()  # fails fast if another process owns the database
    repo = await Repository.open(database)
    fetcher: Fetcher | None = None
    started = time.monotonic()
    try:
        if not resume:
            assert cfg is not None and mode is not None
            run_id = run_id or new_run_id()
            await repo.create_run(
                run_id,
                mode,
                cfg.model_dump(mode="json"),
                cfg.crawler.max_pages,
                cfg.crawler.max_products_per_category,
                cfg.llm.max_model_calls_per_run,
            )
        else:
            assert run_id is not None
            run = await repo.get_run(run_id)
            if run is None:
                raise SystemExit(f"unknown run {run_id}")
            # A resume always uses the run's stored configuration, never the current YAML.
            cfg = AppConfig.model_validate_json(run["config_json"])
            mode = run["mode"]
            if run["status"] in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ERRORS):
                return {"run_id": run_id, "status": run["status"], "note": "run already finished"}
            if run["status"] == RunStatus.BUDGET_EXHAUSTED and not extensions.any():
                return {
                    "run_id": run_id,
                    "status": run["status"],
                    "note": "budget exhausted; resume with --add-pages/--add-model-calls/--add-products",
                }
        setup_logging(cfg.storage.log_dir, run_id, quiet=quiet_logs)
        requeued, failed = await repo.reconcile_running(run_id, cfg.crawler.max_attempts)
        if resume:
            log_event("resume_reconciled", run_id=run_id, outcome="ok", requeued=requeued, failed=failed)
            if extensions.any():
                await repo.extend_run(run_id, extensions.add_pages, extensions.add_model_calls, extensions.add_products)
            await repo.update_run(run_id, status=RunStatus.RUNNING.value, termination_reason=None)
        adapter = ADAPTERS[cfg.site.adapter]
        model = None
        unavailable = None
        if mode == "hybrid" and cfg.agents.extractor.llm_enabled:
            try:
                model = model_factory(cfg)
            except ModelUnavailable as e:
                unavailable = f"MODEL_UNAVAILABLE: {e}"
                log_event("model_unavailable", run_id=run_id, agent="extractor", outcome="degraded", reason=str(e))
        fetcher = await fetcher_factory(cfg)
        store = SnapshotStore(cfg.storage.snapshot_dir)
        orch = Orchestrator(cfg, repo, fetcher, store, adapter, mode, model=model, model_unavailable_reason=unavailable)
        if not resume:
            await orch.seed(run_id)
        log_event(
            "run_started" if not resume else "run_resumed",
            run_id=run_id,
            outcome=mode,
            model_id=getattr(model, "model_id", None),
            scope=cfg.site.scope,
        )
        status = await orch.run(run_id, cfg.crawler.max_invocation_seconds)
        await repo.record_invocation(
            run_id,
            {
                "kind": "resume" if resume else "crawl",
                "seconds": round(time.monotonic() - started, 1),
                "status": status,
                "requeued": requeued,
                "model_id": getattr(model, "model_id", None),
                "model_unavailable": unavailable,
            },
        )
        return {"run_id": run_id, "status": status}
    finally:
        if fetcher is not None:
            await fetcher.close()
        await repo.close()
        lock.release()
