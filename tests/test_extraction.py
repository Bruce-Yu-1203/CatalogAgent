"""Extraction: multiple SKU rows with row-correct price/pack, no visible price, JSON/CSV round trip."""

from __future__ import annotations

import json
import re

from conftest import fixture_html, snapshot

from catalog_agent.adapters import safco
from catalog_agent.agents.validator import ValidatorAgent
from catalog_agent.storage.export import CSV_COLUMNS, csv_rows, read_csv, write_csv, write_json

VALIDATOR = ValidatorAgent(safco, "safco")


def validate(snap, lineage=None):
    cand = safco.parse_product(snap)
    out, issues = VALIDATOR.validate(cand, snap, "run_test", lineage or [])
    return out, issues


def test_multiple_sku_rows_are_distinct_with_row_correct_price_and_pack():
    out, _ = validate(snapshot("product_surestitch_sutures"))
    p = out.product
    assert out.decision == "accepted" and p.variant_count == 15 == out.expected_skus
    skus = [v.sku for v in p.variants]
    assert len(set(skus)) == 15
    by = {v.sku: v for v in p.variants}
    # Values read from the live page's own rows (2026-11-26 snapshot).
    assert by["1025865"].price == "37.49" and by["1025865"].pack_label == "12/box"
    assert by["1025859"].price == "30.49" and by["1025859"].variant_name.startswith("C-31, silk black")
    assert by["1025865"].attributes["Group"].startswith("C-3 needle")
    for v in p.variants:
        ev = {e.field: e for e in v.evidence}
        assert ev["sku"].raw_value == v.sku and ev["sku"].row_id == ev["price"].row_id  # same evidence row
        assert ev["price"].normalized_value == v.price and v.currency == "USD"
        assert v.pack_quantity == 12 and v.pack_unit == "box"
    assert p.category_paths == [["Dental Supplies", "Sutures & surgical products", "Sutures"]]


def test_structured_data_fallback_when_variant_table_did_not_render():
    out, _ = validate(snapshot("product_trimmer_incomplete_render"))
    assert out.decision == "accepted"
    assert {v.sku: v.price for v in out.product.variants} == {"4605716": "144.99", "4605738": "144.99"}
    assert all(e.section_id == "structured-data" for v in out.product.variants for e in v.evidence)
    assert out.product.variants[0].availability.value == "in_stock"


def _strip_prices(html: str) -> str:
    """SYNTHETIC: the real Surpass page with every price removed (table cells and JSON-LD offers)."""
    html = re.sub(
        r'<div class="data-attr-mob pdp-col-3-xl-2 tier-price-wrapper.*?</div>\s*</div>\s*</div>', "", html, flags=re.S
    )
    return re.sub(r'"price":\s*"[0-9.]+"', '"price": ""', html)


def test_no_visible_price_is_null_with_not_visible():
    html = _strip_prices(fixture_html("product_surpass_gloves"))
    out, _ = validate(snapshot("product_surpass_gloves", html=html))
    for v in out.product.variants:
        assert v.price is None and v.currency is None and v.price_visibility.value == "not_shown"
        assert {"field": "price", "reason": "not_visible"} in [m.model_dump(mode="json") for m in v.missing]
        assert "price" not in [e.field for e in v.evidence] and v.evidence[0].field == "sku"


def test_login_required_price():
    html = _strip_prices(fixture_html("product_surpass_gloves")).replace(
        "</body>",
        "<p>Please sign in to see price</p></body>",  # SYNTHETIC notice
    )
    out, _ = validate(snapshot("product_surpass_gloves", html=html))
    assert all(v.price_visibility.value == "login_required" for v in out.product.variants)
    assert all(
        any(m.reason.value == "login_required" for m in v.missing if m.field == "price") for v in out.product.variants
    )


def test_one_product_round_trips_through_json_and_csv(tmp_path):
    out, _ = validate(snapshot("product_surpass_gloves"), lineage=[["Dental Supplies", "Dental Exam Gloves"]])
    p = out.product
    jpath = write_json([p], tmp_path / "products.json")
    cpath = write_csv([p], tmp_path / "variants.csv")
    data = json.loads(jpath.read_text())
    rows = read_csv(cpath)
    assert len(data) == 1 and len(rows) == p.variant_count == 5
    assert list(rows[0].keys()) == CSV_COLUMNS
    j = data[0]
    assert "product_id" not in j["variants"][0]  # implied by the parent in nested JSON
    for jv, row in zip(j["variants"], rows, strict=True):
        assert row["product_id"] == j["product_id"] and row["product_name"] == j["name"]
        assert row["product_url"] == j["url"] and row["run_id"] == j["run_id"]
        assert row["category_paths"] == " | ".join(" > ".join(x) for x in j["category_paths"])
        assert json.loads(row["product_attributes"]) == j["attributes"]
        assert json.loads(row["product_missing"]) == j["missing"]
        assert row["sku"] == jv["sku"] and row["price"] == jv["price"] and row["currency"] == jv["currency"]
        assert row["pack_quantity"] == str(jv["pack_quantity"]) and row["pack_unit"] == jv["pack_unit"]
        assert row["availability"] == jv["availability"] and row["variant_name"] == jv["variant_name"]
        assert json.loads(row["evidence"]) == jv["evidence"]
        assert {e["field"] for e in jv["evidence"]} == {"sku", "price", "pack_label", "availability_raw"}
        assert json.loads(row["variant_missing"]) == jv["missing"]
    # In-memory CSV rows equal what was written (no float or encoding drift).
    assert [dict((k, str(v)) for k, v in r.items()) for r in csv_rows([p])] == rows


def test_pack_parsing_forms_seen_on_the_live_site():
    from catalog_agent.extraction.normalize import parse_pack

    assert parse_pack("X-small, 300/box") == ("300/box", 300, "box")
    assert parse_pack("Box of 20") == ("Box of 20", 20, "box")
    assert parse_pack("5-pack") == ("5-pack", 5, "pack")
    assert parse_pack("Soft Tissue Trimmer regular 1/pk") == ("1/pk", 1, "pack")
    assert parse_pack("Mineralized cortical bone .25-1.0mm 1.0cc jar") == (None, None, None)  # volume, not a pack


def test_variant_label_excludes_sds_links_and_tooltips():
    from conftest import BASE

    snap = snapshot("product_orasoothe_tooltips", url=f"{BASE}/product/orasoothe-reg-sockit-gel")
    out, _ = validate(snap)
    names = {v.sku: v.variant_name for v in out.product.variants}
    assert names["5690102"] == "5-pack" and names["5690106"] == "25-pack"
    assert not any("SDS" in (n or "") or "Details" in (n or "") for n in names.values())
    by = {v.sku: v for v in out.product.variants}
    assert (by["5690102"].pack_quantity, by["5690102"].pack_unit) == (5, "pack")


async def test_product_without_rendered_table_is_usable_on_first_attempt_via_structured_data():
    from conftest import URLS, ScriptedFetcher

    snap = await ScriptedFetcher().fetch(URLS["product_trimmer_incomplete_render"], "product", require_ready=True)
    assert safco.section_by_id(snap, "variant-table") is None or not safco.section_by_id(snap, "variant-table").rows
    out, _ = validate(snap)
    assert out.decision == "accepted" and out.product.variant_count == 2


def test_p1_evidence_for_pack_availability_and_attributes_resolves_to_the_same_row():
    out, _ = validate(snapshot("product_surestitch_sutures"))
    for v in out.product.variants:
        ev = {e.field: e for e in v.evidence}
        assert {"sku", "price", "pack_label", "availability_raw", "attributes.Group"} <= set(ev)
        assert len({e.row_id for e in v.evidence}) == 1  # one row backs every value
        assert ev["pack_label"].raw_value == v.pack_label and ev["pack_label"].normalized_value == "12 box"
        assert ev["availability_raw"].normalized_value == v.availability.value != "unknown"
        assert ev["attributes.Group"].raw_value == v.attributes["Group"]


def test_unsupported_evidence_field_is_rejected_by_the_schema():
    import pytest
    from pydantic import ValidationError

    from catalog_agent.contracts import EvidenceRef

    with pytest.raises(ValidationError):
        EvidenceRef(
            field="description", snapshot_id="s", section_id="x", raw_value="a", normalized_value="a", method="llm"
        )
