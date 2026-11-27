"""P1: malformed model output, oversized tool results and cursor cycles are rejected or bounded."""

from __future__ import annotations

import json

from conftest import BASE, URLS, FakeModel, ScriptedFetcher, make_config, snapshot, tool
from test_agent_loop import MISSING, gap_snapshot, run_loop

from catalog_agent.adapters import safco
from catalog_agent.agents.navigator import NavigatorAgent, NavigatorInput
from catalog_agent.contracts import CrawlTask, PageType, TaskType
from catalog_agent.extraction.llm import ModelReply
from catalog_agent.extraction.tools import SnapshotTools


async def test_malformed_patches_never_crash_the_loop_or_reach_the_record():
    snap = gap_snapshot()
    junk = [
        ModelReply(tool_calls=[tool("submit_patch", 0, variants="not a list")]),
        ModelReply(tool_calls=[tool("submit_patch", 1, variants=[None, 7, "x", {"sku": None}, {"sku": ["a"]}])]),
        ModelReply(
            tool_calls=[
                tool(
                    "submit_patch",
                    2,
                    variants=[
                        {
                            "sku": MISSING,
                            "section_id": "page-rows",
                            "row_id": "generic-3",
                            "sku_raw": MISSING,
                            "price_raw": {"amount": 1},
                            "attributes": {"Size": 5, "": "x", "Color": "Z" * 5000},
                        }
                    ],
                )
            ]
        ),
    ]
    res, out, _ = await run_loop(FakeModel(junk), snap, allowance=3, attribute_gaps=False)
    assert res.budget_used.model_calls <= 3
    added = next((v for v in out.product.variants if v.sku == MISSING), None)
    assert added is not None and added.price is None and added.attributes == {}  # verified SKU only
    assert all(v.sku.isdigit() for v in out.product.variants)


async def test_unknown_tools_and_bad_arguments_are_answered_not_executed():
    snap = gap_snapshot()
    model = FakeModel(
        [
            ModelReply(
                tool_calls=[
                    tool("run_shell", 0, cmd="rm -rf /"),
                    tool("get_section", 1, section_id=123),
                    tool("get_section", 2, section_id="x" * 10_000),
                ]
            ),
            ModelReply(tool_calls=[tool("submit_patch", 3, variants=[])]),
        ]
    )
    res, out, _ = await run_loop(model, snap, allowance=2, attribute_gaps=False)
    results = [m["content"] for m in model.calls[1]["messages"] if m["role"] == "tool"]
    assert any("unknown tool" in r for r in results) and all("unknown section_id" in r for r in results[1:3])
    assert out.decision == "partial"


def test_tool_results_are_size_bounded():
    tools = SnapshotTools(safco, snapshot("product_surestitch_sutures"), max_chars=500)
    for name, args in [
        ("get_variant_rows", {}),
        ("inspect_sections", {}),
        ("get_section", {"section_id": "page-text"}),
    ]:
        assert len(tools.call(name, args)) <= 500 + len('..."[truncated]"')


async def test_pagination_cycle_back_to_the_same_page_is_detected():
    html = snapshot("listing_gloves").html.replace(f"{BASE}/catalog/gloves?page=2", f"{BASE}/catalog/gloves")
    url = URLS["listing_gloves"]
    snap = snapshot("listing_gloves", html=html)  # SYNTHETIC: "next" points back at the same page
    task = CrawlTask(
        task_id=1, run_id="r", task_key=f"listing:{url}", task_type=TaskType.LISTING, url=url, root=url, depth=1
    )
    res = await NavigatorAgent(safco).run(
        NavigatorInput(
            task=task,
            snapshot=snap,
            page_type=PageType.LISTING,
            scope="selected",
            configured_roots=[url],
            allowed_hosts=["www.safcodental.com"],
            url_check=lambda u: None,
        )
    )
    assert res.output.pagination_stop == "cycle_detected"


async def test_two_page_cursor_cycle_terminates_through_task_dedup(tmp_path):
    from catalog_agent.runtime.fetcher import make_snapshot
    from catalog_agent.runtime.runner import execute
    from catalog_agent.storage.repository import Repository

    cfg = make_config(tmp_path, max_products_per_category=1, concurrency=1, max_pages=60)
    p1, p2 = f"{BASE}/catalog/gloves?page=1", f"{BASE}/catalog/gloves?page=2"
    base_html = snapshot("listing_gloves").html
    fetcher = ScriptedFetcher()
    real = fetcher.fetch

    async def fetch(u, kind, require_ready=True):
        if u in (p1, p2):  # SYNTHETIC A -> B -> A pagination cycle
            nxt = p1 if u == p2 else p2
            return make_snapshot(u, u, 200, base_html.replace(f"{BASE}/catalog/gloves?page=2", nxt))
        return await real(u, kind, require_ready)

    fetcher.fetch = fetch
    fetcher.check_url = lambda u: None

    async def factory(_c):
        return fetcher

    await execute(
        database=cfg.storage.database,
        cfg=cfg,
        mode="deterministic",
        run_id="run_cyc",
        fetcher_factory=factory,
        quiet_logs=True,
    )
    repo = await Repository.open(cfg.storage.database)
    try:
        tasks = await repo.tasks("run_cyc")
    finally:
        await repo.close()
    page_tasks = [t for t in tasks if "gloves?page=" in t["url"]]
    assert sorted(t["url"] for t in page_tasks) == [p1, p2]  # each cursor scheduled once
    assert all(t["attempts"] == 1 for t in page_tasks)
    assert json.loads(next(t for t in page_tasks if t["url"] == p2)["outcome_json"])["children"] > 0
