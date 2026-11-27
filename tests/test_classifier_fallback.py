"""P1 (FR-11): classifier model fallback for pages the rules cannot identify.

Fixtures are real snapshots served at an unrecognised URL (SYNTHETIC URL, real HTML), which is how a
redirect to an unknown URL scheme would look.
"""

from __future__ import annotations

from conftest import BASE, ExplodingModel, FakeModel, fixture_html, tool

from catalog_agent.adapters import safco
from catalog_agent.agents.classifier import ClassifierAgent
from catalog_agent.contracts import PageType
from catalog_agent.extraction.llm import ModelReply
from catalog_agent.runtime.fetcher import make_snapshot

ODD_URL = f"{BASE}/item/safco-nitrilex-surpass"


def odd(name: str):
    return make_snapshot(ODD_URL, ODD_URL, 200, fixture_html(name))


def answer(page_type: str, cited: list[str]) -> FakeModel:
    return FakeModel(
        [ModelReply(tool_calls=[tool("submit_classification", page_type=page_type, evidence_section_ids=cited)])]
    )


async def test_unrecognised_layout_is_routed_when_dom_signals_confirm_the_model():
    snap = odd("product_surpass_gloves")
    assert safco.classify_signals(snap)[0] == PageType.UNKNOWN
    model = answer("product", ["structured-data", "variant-table"])
    res = await ClassifierAgent(safco, fallback_enabled=True).run(snap, model=model, model_allowed=True)
    assert res.output.page_type == PageType.PRODUCT
    assert res.budget_used.model_calls == 1 and len(model.calls) == 1
    assert next(e for e in res.events if e.event == "classifier_fallback").outcome == "accepted"
    sent = model.calls[0]["messages"][1]["content"]
    assert sent.startswith("Sections: <untrusted_page_data>")


async def test_model_answer_without_required_dom_signals_is_recorded_for_review():
    snap = odd("listing_gloves")  # a listing (hits) at an odd URL; the model wrongly says product
    model = answer("product", ["listing-hits"])
    res = await ClassifierAgent(safco, fallback_enabled=True).run(snap, model=model, model_allowed=True)
    assert res.output.page_type == PageType.UNKNOWN and "DOM signals are absent" in res.output.reason
    assert next(e for e in res.events if e.event == "classifier_fallback").outcome == "review"


async def test_invalid_type_or_citation_is_not_routed():
    snap = odd("product_surpass_gloves")
    for model in (
        answer("checkout", ["structured-data"]),
        answer("product", ["no-such-section"]),
        answer("product", []),
    ):
        res = await ClassifierAgent(safco, fallback_enabled=True).run(snap, model=model, model_allowed=True)
        assert res.output.page_type == PageType.UNKNOWN


async def test_fallback_is_off_by_default_and_needs_a_reserved_call():
    snap = odd("product_surpass_gloves")
    res = await ClassifierAgent(safco).run(snap, model=ExplodingModel(), model_allowed=True)
    assert res.output.page_type == PageType.UNKNOWN
    res = await ClassifierAgent(safco, fallback_enabled=True).run(snap, model=ExplodingModel(), model_allowed=False)
    assert res.output.page_type == PageType.UNKNOWN  # no budget reserved -> no call


async def test_rule_classified_pages_never_call_the_model():
    snap = make_snapshot(f"{BASE}/product/x", f"{BASE}/product/x", 200, fixture_html("product_surpass_gloves"))
    res = await ClassifierAgent(safco, fallback_enabled=True).run(snap, model=ExplodingModel(), model_allowed=True)
    assert res.output.page_type == PageType.PRODUCT


async def test_orchestrator_reserves_the_fallback_call_in_the_run_budget(tmp_path):
    from conftest import URLS, ScriptedFetcher, make_config

    from catalog_agent.runtime.runner import execute
    from catalog_agent.storage.repository import Repository

    cfg = make_config(tmp_path, max_products_per_category=1, concurrency=1)
    cfg.agents.classifier.llm_fallback_enabled = True
    cfg.agents.extractor.llm_enabled = False
    fetcher = ScriptedFetcher()
    real = fetcher.fetch

    async def redirected(url, kind, require_ready=True):
        snap = await real(url, kind, require_ready)
        if url == URLS["product_surpass_gloves"]:  # SYNTHETIC: pretend the page redirected to an odd URL
            return make_snapshot(url, ODD_URL, 200, snap.html)
        return snap

    fetcher.fetch = redirected
    fetcher.check_url = lambda u: None
    model = answer("product", ["structured-data"])

    async def factory(_c):
        return fetcher

    await execute(
        database=cfg.storage.database,
        cfg=cfg,
        mode="hybrid",
        run_id="run_cf",
        fetcher_factory=factory,
        model_factory=lambda _c: model,
        quiet_logs=True,
    )
    repo = await Repository.open(cfg.storage.database)
    run = await repo.get_run("run_cf")
    task = next(t for t in await repo.tasks("run_cf") if t["url"] == URLS["product_surpass_gloves"])
    assert task["status"] == "COMPLETED" and task["page_type"] == "product"
    assert run["model_calls_used"] == 1 == len(model.calls)
    assert '"classifier_fallback_accepted": 1' in run["counters_json"]
    await repo.close()
