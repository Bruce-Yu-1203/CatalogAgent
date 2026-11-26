"""Navigator Agent: which links to enqueue, pagination continuation and stop. Never uses a model."""

from __future__ import annotations

import time
from collections.abc import Callable
from types import ModuleType

from pydantic import Field

from catalog_agent.contracts import (
    AgentInput,
    AgentResult,
    ChildTask,
    CrawlTask,
    Event,
    NavigatorOutput,
    PageSnapshot,
    PageType,
    TaskType,
)
from catalog_agent.extraction.normalize import canonicalize_url

MAX_DEPTH = 5


class NavigatorInput(AgentInput):
    task: CrawlTask
    snapshot: PageSnapshot
    page_type: PageType
    scope: str  # selected | all
    configured_roots: list[str]
    allowed_hosts: list[str]
    url_check: Callable[[str], str | None] = Field(exclude=True)  # returns rejection reason or None


def select_roots(discovered: list[str], scope: str, configured: list[str]) -> tuple[list[str], bool]:
    """`selected` seeds the configured roots that discovery found; `all` seeds every permitted root.

    Returns (roots, degraded): degraded is True when a configured root was not discovered and had to
    be seeded explicitly.
    """
    uniq = list(dict.fromkeys(discovered))
    if scope == "all":
        return uniq, False
    wanted = [canonicalize_url(u) or u for u in configured]
    found = [u for u in wanted if u in uniq]
    missing = [u for u in wanted if u not in uniq]
    return found + missing, bool(missing)


class NavigatorAgent:
    name = "navigator"

    def __init__(self, adapter: ModuleType):
        self.adapter = adapter

    async def run(self, inp: NavigatorInput) -> AgentResult:
        t0 = time.perf_counter()
        a = self.adapter
        task, snap = inp.task, inp.snapshot
        links = a.extract_links(snap, inp.page_type)
        out = NavigatorOutput()
        seen: set[str] = set()

        def reject(url: str) -> str | None:
            return inp.url_check(url)

        if inp.page_type == PageType.CATEGORY and task.task_type == TaskType.DISCOVERY:
            discovered = []
            for link in links:
                if link.kind == "root" and reject(link.url) is None:
                    discovered.append(link.url)
            roots, degraded = select_roots(discovered, inp.scope, inp.configured_roots)
            out.degraded_discovery = degraded
            for r in roots:
                if (why := reject(r)) is not None:
                    out.rejected.append((r, why))
                    continue
                out.children.append(ChildTask(task_type=TaskType.LISTING, url=r, root=r, lineage=[], depth=1))
            return self._result(out, t0, discovered=len(set(discovered)))

        lineage_path = a.breadcrumb(snap)
        root = task.root or task.url
        root_path = a.path_of(root)
        for link in links:
            url = link.url
            if url in seen:
                continue
            seen.add(url)
            if (why := reject(url)) is not None:
                if link.kind == "next_page":
                    out.pagination_stop = f"next_page_{why}"
                out.rejected.append((url, why))
                continue
            if link.kind == "subcategory":
                p = a.path_of(url)
                if not (p == root_path or p.startswith(root_path + "/")):
                    out.rejected.append((url, "off_scope"))
                    continue
                if task.depth + 1 > MAX_DEPTH:
                    out.rejected.append((url, "max_depth"))
                    continue
                out.children.append(
                    ChildTask(task_type=TaskType.LISTING, url=url, root=root, lineage=[], depth=task.depth + 1)
                )
            elif link.kind == "product":
                out.children.append(
                    ChildTask(
                        task_type=TaskType.PRODUCT,
                        url=url,
                        root=root,
                        lineage=[lineage_path] if lineage_path else [],
                        depth=task.depth + 1,
                    )
                )
            elif link.kind == "next_page":
                out.children.append(
                    ChildTask(task_type=TaskType.LISTING, url=url, root=root, lineage=[], depth=task.depth)
                )
                out.pagination_stop = None
        if inp.page_type == PageType.LISTING and out.pagination_stop is None:
            has_next = any(c.task_type == TaskType.LISTING and "page=" in c.url for c in out.children)
            out.pagination_stop = "next_page_enqueued" if has_next else "last_page"
        return self._result(out, t0, stats=a.listing_stats(snap), lineage=lineage_path)

    def _result(self, out: NavigatorOutput, t0: float, **data: object) -> AgentResult:
        counts = {
            "products": sum(c.task_type == TaskType.PRODUCT for c in out.children),
            "listings": sum(c.task_type == TaskType.LISTING for c in out.children),
            "rejected": len(out.rejected),
        }
        return AgentResult(
            outcome="ok",
            output=out,
            events=[Event(event="navigated", outcome=out.pagination_stop or "ok", data={**counts, **data})],
            budget_used={"seconds": time.perf_counter() - t0},
        )
