"""P1 (FR-12): Recovery repair suggestions for a selector that no longer matches.

Failing snapshots are SYNTHETIC: real product pages where the `product-item-sku` class was renamed
(a simulated layout change). Control snapshots are the unmodified real pages.
"""

from __future__ import annotations

import copy

from conftest import FakeModel, fixture_html, snapshot, tool

from catalog_agent.adapters import safco
from catalog_agent.agents.recovery import rank_candidates, suggest_repair
from catalog_agent.extraction.llm import ModelReply
from catalog_agent.extraction.tools import SelectorTester

NAMES = ["product_surpass_gloves", "product_surestitch_sutures", "product_orasoothe_tooltips"]
OLD = safco.FIELD_SELECTORS["product"]["variant_rows"]


def renamed(name: str):
    html = fixture_html(name).replace('class="product-item-sku text-xs"', 'class="item-sku-code text-xs"')
    return snapshot(name, html=html, url=f"https://www.safcodental.com/product/{name}")


def control(name: str):
    return snapshot(name, url=f"https://www.safcodental.com/product/{name}")


def test_field_failure_is_detected_on_the_changed_layout_only():
    assert safco.field_failures(renamed(NAMES[0]), "product") == ["variant_rows"]
    assert safco.field_failures(control(NAMES[0]), "product") == []


def test_selector_tester_is_bounded_and_rejects_invalid_css():
    t = SelectorTester([renamed(NAMES[0])], [control(NAMES[0])])
    assert "invalid selector" in t.run("div[[[")["error"]
    assert "error" in t.run("x" * 201)
    r = t.run(".item-sku-code")
    assert r["failing"][0]["matches"] > 0 and r["control"][0]["matches"] == 0
    assert "unknown tool" in t.call("fetch_url", {"url": "https://example.com"})


def test_ranking_prefers_candidates_without_regressions():
    failing = [renamed(n) for n in NAMES]
    ctrl = [control(n) for n in NAMES]
    ranked = rank_candidates(
        SelectorTester(failing, ctrl), OLD, [".item-sku-code", '#pdp-grouped [x-text="item.sku"]', "div.nope"]
    )
    best = ranked[0]
    assert best.selector == '#pdp-grouped [x-text="item.sku"]'  # matches the new and the old layout
    assert best.failing_match_rate == 1.0 and best.control_match_rate == 1.0 and best.regressions == 0
    narrow = next(r for r in ranked if r.selector == ".item-sku-code")
    assert narrow.failing_match_rate == 1.0 and narrow.regressions == 3
    assert ranked[-1].selector == "div.nope" or ranked[-1].failing_match_rate == 0


async def test_suggestion_is_produced_tested_and_never_applied():
    before = copy.deepcopy(safco.FIELD_SELECTORS)
    failing = [renamed(n) for n in NAMES]
    ctrl = [control(n) for n in NAMES]
    model = FakeModel(
        [
            ModelReply(tool_calls=[tool("test_selector", selector=".item-sku-code")]),
            ModelReply(
                tool_calls=[
                    tool(
                        "submit_selectors",
                        1,
                        candidates=['#pdp-grouped [x-text="item.sku"]', ".item-sku-code"],
                        rationale="x-text is stable",
                    )
                ]
            ),
        ]
    )
    sugg, budget = await suggest_repair(
        model, "variant_rows", "product", OLD, failing, ctrl, safco.dom_outline(failing[0])
    )
    assert sugg.status == "suggested" and sugg.applied is False
    assert sugg.candidates[0].selector == '#pdp-grouped [x-text="item.sku"]' and sugg.candidates[0].regressions == 0
    assert budget.model_calls == 2 and budget.tool_calls == 1
    tool_result = next(m for m in model.calls[1]["messages"] if m["role"] == "tool")
    assert '"failing"' in tool_result["content"]  # the model saw real test results
    assert safco.FIELD_SELECTORS == before  # parser configuration unchanged
    assert "<untrusted_page_data>" in model.calls[0]["messages"][1]["content"]


async def test_run_level_trigger_and_report(tmp_path):
    from conftest import URLS, ScriptedFetcher, make_config

    from catalog_agent.quality import build_report
    from catalog_agent.runtime.fetcher import make_snapshot
    from catalog_agent.runtime.runner import execute
    from catalog_agent.storage.repository import Repository

    cfg = make_config(tmp_path, max_products_per_category=3, concurrency=1)
    cfg.agents.recovery.repair_suggestions_enabled = True
    cfg.agents.recovery.trigger_after_failures = 2
    cfg.agents.extractor.llm_enabled = False
    fetcher = ScriptedFetcher()
    real = fetcher.fetch
    broken = {URLS["product_surpass_gloves"], URLS["product_surestitch_sutures"]}

    async def fetch(url, kind, require_ready=True):
        snap = await real(url, kind, False)
        if url in broken:  # SYNTHETIC layout change on two real pages
            html = snap.html.replace('class="product-item-sku text-xs"', 'class="item-sku-code text-xs"')
            return make_snapshot(url, url, 200, html)
        return snap

    fetcher.fetch = fetch
    model = FakeModel([ModelReply(tool_calls=[tool("submit_selectors", candidates=['[x-text="item.sku"]'])])])

    async def factory(_c):
        return fetcher

    await execute(
        database=cfg.storage.database,
        cfg=cfg,
        mode="hybrid",
        run_id="run_rs",
        fetcher_factory=factory,
        model_factory=lambda _c: model,
        quiet_logs=True,
    )
    repo = await Repository.open(cfg.storage.database)
    try:
        rep = await build_report(repo, "run_rs")
        run = await repo.get_run("run_rs")
    finally:
        await repo.close()
    assert rep["field_failures"]["product.variant_rows"] >= 2
    [s] = rep["repair_suggestions"]
    assert s["field"] == "variant_rows" and s["applied"] is False and s["candidates"][0]["regressions"] == 0
    assert run["model_calls_used"] == 1 == len(model.calls)


def test_unrendered_table_is_a_render_note_not_a_selector_failure():
    snap = snapshot("product_trimmer_incomplete_render")
    assert safco.field_failures(snap, "product") == []
    assert safco.render_notes(snap, "product") == ["table_not_rendered"]
    assert safco.render_notes(control(NAMES[0]), "product") == []
