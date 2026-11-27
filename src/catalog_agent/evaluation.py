"""P1 (FR-13): offline deterministic-vs-hybrid evaluation on frozen, labelled fixtures.

Both modes run the same Extractor + Validator on the same saved snapshots; no page is fetched.
Replay results describe replay behaviour only (A-11) and are reported separately from live runs.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from bs4 import BeautifulSoup

from catalog_agent.agents.extractor import ExtractorAgent, ExtractorInput
from catalog_agent.agents.validator import ValidatorAgent
from catalog_agent.config import AppConfig
from catalog_agent.contracts import PageSnapshot
from catalog_agent.extraction.normalize import normalize_price
from catalog_agent.runtime.fetcher import make_snapshot

REDESIGNS = ("classes", "tags", "columns")
_TAG_SWAP = {"div": "section", "span": "em", "ul": "ol"}


def simulate_redesign(html: str, kind: str = "classes") -> str:
    """SYNTHETIC site redesign of a saved page; never written to `outputs/`. Every kind removes the JSON-LD and
    renames every class name (consistently, so repeated elements still look alike).
    - classes: only that; text, tags, ids and structure are unchanged.
    - tags: also changes the element types (div -> section, span -> em, ul -> ol), as a new front-end would.
    - columns: also reverses the order of the children of every repeated element (an element that has a sibling
      with the same tag, e.g. a variant row and its header row), so SKU, label, stock and price change places."""
    if kind not in REDESIGNS:
        raise ValueError(f"unknown redesign {kind!r}")
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select('script[type="application/ld+json"]'):
        tag.decompose()
    if kind == "columns":
        repeated = []
        for parent in soup.find_all(True):
            sigs: dict[str, list] = {}
            for child in parent.find_all(True, recursive=False):
                sigs.setdefault(child.name, []).append(child)
            repeated += [c for group in sigs.values() if len(group) > 1 for c in group]
        for el in repeated:
            kids = el.find_all(True, recursive=False)
            if len(kids) > 1:
                for k in kids:
                    k.extract()
                for k in reversed(kids):
                    el.append(k)
    for el in soup.find_all(class_=True):
        el["class"] = ["x" + hashlib.sha1(c.encode()).hexdigest()[:8] for c in el.get("class", [])]
    if kind == "tags":
        for el in soup.find_all(list(_TAG_SWAP)):
            el.name = _TAG_SWAP[el.name]
    return str(soup)


def _rate(n: float, d: float) -> float | None:
    return round(n / d, 4) if d else None


def _norm(v: str) -> str:
    return " ".join(v.lower().split())


async def evaluate_mode(
    cfg: AppConfig, adapter: ModuleType, fixtures: Path, mode: str, model: Any = None
) -> dict[str, Any]:
    manifest = json.loads((fixtures / "manifest.json").read_text())
    labels = json.loads((fixtures / "labels.json").read_text())["pages"]
    extractor = ExtractorAgent(adapter, cfg.agents.extractor, cfg.llm)
    validator = ValidatorAgent(adapter, adapter.SOURCE)
    pages = []
    for url, name in manifest.items():
        lab = labels[url]
        with gzip.open(fixtures / name, "rt", encoding="utf-8") as f:
            snap = make_snapshot(url, url, 200, f.read())
        t0 = time.perf_counter()
        res = await extractor.run(
            ExtractorInput(
                snapshot=snap,
                mode=mode,
                model_allowance=cfg.agents.extractor.max_model_calls_per_task,
                model=model,
            )
        )
        out, _ = validator.validate(res.output.candidate, snap, "eval", [])
        seconds = time.perf_counter() - t0
        got = {v.sku: v for v in (out.product.variants if out.product else [])}
        exp = lab["variants"]
        tp = len(set(got) & set(exp))
        price_ok = sum(1 for s, v in got.items() if s in exp and v.price == exp[s]["price"])
        price_wrong = [s for s, v in got.items() if s in exp and v.price is not None and v.price != exp[s]["price"]]
        a_req = a_hit = a_pred = a_bad = 0
        bad_examples = []
        for s, e in exp.items():
            v = got.get(s)
            req = {_norm(x) for x in e["attribute_values"]}
            ok = req | {_norm(x) for x in e["allowed_extra_values"]}
            pred = {_norm(x) for k, x in (v.attributes.items() if v else []) if k != "Group"}
            a_req += len(req)
            a_hit += len(req & pred)
            a_pred += len(pred)
            wrong = [x for x in pred if not any(x == o or x in o or o in x for o in ok)]
            a_bad += len(wrong)
            if wrong and len(bad_examples) < 5:
                bad_examples.append({"sku": s, "label": e["label"], "unexpected": wrong})
        b = res.budget_used
        pages.append(
            {
                "url": url,
                "split": lab["split"],
                "source": lab["source"],
                "decision": out.decision,
                "expected_skus": len(exp),
                "predicted_skus": len(got),
                "matched_skus": tp,
                "prices_correct": price_ok,
                "prices_wrong": price_wrong,
                "attr_required": a_req,
                "attr_found": a_hit,
                "attr_predicted": a_pred,
                "attr_unexpected": a_bad,
                "attr_unexpected_examples": bad_examples,
                "model_calls": b.model_calls,
                "tool_calls": b.tool_calls,
                "input_tokens": b.input_tokens,
                "output_tokens": b.output_tokens,
                "seconds": round(seconds, 3),
                "gaps_remaining": [g.kind for g in res.output.gaps_remaining],
            }
        )
    return {"mode": mode, "pages": pages}


def summarize(result: dict[str, Any], price_in: float | None, price_out: float | None) -> dict[str, Any]:
    def agg(ps: list[dict]) -> dict[str, Any]:
        s = {
            k: sum(p[k] for p in ps)
            for k in (
                "expected_skus",
                "predicted_skus",
                "matched_skus",
                "prices_correct",
                "attr_required",
                "attr_found",
                "attr_predicted",
                "attr_unexpected",
                "model_calls",
                "tool_calls",
                "input_tokens",
                "output_tokens",
            )
        }
        cost = None
        if price_in is not None and price_out is not None:
            cost = round(s["input_tokens"] / 1e6 * price_in + s["output_tokens"] / 1e6 * price_out, 5)
        return {
            "pages": len(ps),
            "variant_recall": _rate(s["matched_skus"], s["expected_skus"]),
            "variant_precision": _rate(s["matched_skus"], s["predicted_skus"]),
            "price_accuracy": _rate(s["prices_correct"], s["matched_skus"]),
            "attribute_recall": _rate(s["attr_found"], s["attr_required"]),
            "attribute_precision": _rate(s["attr_predicted"] - s["attr_unexpected"], s["attr_predicted"]),
            "accepted": sum(p["decision"] == "accepted" for p in ps),
            "partial": sum(p["decision"] == "partial" for p in ps),
            "quarantined": sum(p["decision"] == "quarantined" for p in ps),
            "model_calls": s["model_calls"],
            "tool_calls": s["tool_calls"],
            "input_tokens": s["input_tokens"],
            "output_tokens": s["output_tokens"],
            "cost_usd": cost,
            "seconds_total": round(sum(p["seconds"] for p in ps), 2),
            "counts": s,
        }

    pages = result["pages"]
    return {
        "mode": result["mode"],
        "all": agg(pages),
        "held_out_live": agg([p for p in pages if p["split"] == "held_out" and p["source"] == "live"]),
        "dev_live": agg([p for p in pages if p["split"] == "dev" and p["source"] == "live"]),
        "synthetic": agg([p for p in pages if p["source"] != "live"]),
        "failures": [
            {k: p[k] for k in ("url", "decision", "prices_wrong", "attr_unexpected_examples", "gaps_remaining")}
            for p in pages
            if p["decision"] != "accepted"
            or p["prices_wrong"]
            or p["attr_unexpected"]
            or p["matched_skus"] < p["expected_skus"]
        ],
    }


# ---------------------------------------------------------------------------
# Redesign experiment: original vs simulated-redesign pages, rules only vs rules + LLM (2 x 2)
# ---------------------------------------------------------------------------

GROUPS = {
    "A": ("original", "deterministic"),
    "B": ("original", "hybrid"),
    "C": ("redesign", "deterministic"),
    "D": ("redesign", "hybrid"),
}  # C and D run once per redesign kind and are reported as e.g. "C:classes", "D:columns"
_FIELDS = ("price", "pack_label", "availability_raw", "variant_name")


def reference_variants(adapter: ModuleType, snap: PageSnapshot) -> dict[str, str | None]:
    """SKU -> price from the ORIGINAL page's JSON-LD (the same reference as the eval labels)."""
    sd = adapter.section_by_id(snap, "structured-data")
    return {r.cells["sku"]: (normalize_price(r.cells.get("price")) or None) for r in (sd.rows if sd else [])}


async def redesign_experiment(
    cfg: AppConfig,
    adapter: ModuleType,
    snaps: list[PageSnapshot],
    groups: list[str],
    model: Any = None,
    kinds: tuple[str, ...] = ("classes",),
) -> dict[str, Any]:
    extractor = ExtractorAgent(adapter, cfg.agents.extractor, cfg.llm)
    validator = ValidatorAgent(adapter, adapter.SOURCE)
    refs = {s.url: reference_variants(adapter, s) for s in snaps}
    # SKUs the original page actually rendered as rows: the most a redesigned page (no JSON-LD) can still show.
    shown = {}
    for s in snaps:
        table = adapter.section_by_id(s, "variant-table")
        shown[s.url] = {r.cells.get("item_no") for r in (table.rows if table else [])} - {"", None}
    redesigned = {
        k: {s.url: make_snapshot(s.url, s.final_url, s.status, simulate_redesign(s.html, k)) for s in snaps}
        for k in kinds
    }
    runs = [(g, None) for g in groups if GROUPS[g][0] == "original"]
    runs += [(g, k) for k in kinds for g in groups if GROUPS[g][0] == "redesign"]
    out: dict[str, Any] = {}
    for g, kind in runs:
        pages_kind, mode = GROUPS[g]
        pages = []
        for s in snaps:
            snap = s if kind is None else redesigned[kind][s.url]
            res = await extractor.run(
                ExtractorInput(
                    snapshot=snap,
                    mode=mode,
                    model_allowance=cfg.agents.extractor.max_model_calls_per_task,
                    model=model if mode == "hybrid" else None,
                )
            )
            val, _ = validator.validate(res.output.candidate, snap, "experiment", [])
            got = {v.sku: v for v in (val.product.variants if val.product else [])}
            ref = refs[s.url]
            b = res.budget_used
            pages.append(
                {
                    "url": s.url,
                    "decision": val.decision,
                    "reference_skus": len(ref),
                    "exported_skus": len(got),
                    "matched_skus": len(set(got) & set(ref)),
                    "extra_skus": sorted(set(got) - set(ref)) if ref else [],
                    "shown_skus": len(shown[s.url]),
                    "matched_shown_skus": len(set(got) & shown[s.url]),
                    "prices_correct": sum(1 for k, v in got.items() if k in ref and v.price and v.price == ref[k]),
                    "prices_wrong": [
                        k for k, v in got.items() if k in ref and v.price and ref[k] and v.price != ref[k]
                    ],
                    "filled": {f: sum(1 for k, v in got.items() if k in ref and getattr(v, f)) for f in _FIELDS},
                    "llm_skus": sum(1 for v in got.values() if "sku" in v.llm_fields),
                    "model_calls": b.model_calls,
                    "input_tokens": b.input_tokens,
                    "output_tokens": b.output_tokens,
                    "gaps_before_model": [
                        e.data.get("gaps") for e in res.events if e.event == "deterministic_extraction"
                    ][0],
                }
            )
        out[g if kind is None else f"{g}:{kind}"] = {
            "pages": pages_kind if kind is None else f"redesign:{kind}",
            "mode": mode,
            "per_page": pages,
        }
    return out


def summarize_experiment(result: dict[str, Any], price_in: float, price_out: float) -> dict[str, Any]:
    rows = {}
    for g, r in result.items():
        ps = r["per_page"]
        with_ref = [p for p in ps if p["reference_skus"]]
        ref = sum(p["reference_skus"] for p in with_ref)
        tin, tout = sum(p["input_tokens"] for p in ps), sum(p["output_tokens"] for p in ps)
        rows[g] = {
            "pages": r["pages"],
            "mode": r["mode"],
            "products": len(ps),
            "accepted": sum(p["decision"] == "accepted" for p in ps),
            "partial": sum(p["decision"] == "partial" for p in ps),
            "quarantined": sum(p["decision"] == "quarantined" for p in ps),
            "reference_skus": ref,
            "sku_completeness": _rate(sum(p["matched_skus"] for p in with_ref), ref),
            "shown_skus": sum(p["shown_skus"] for p in ps),
            "sku_completeness_of_shown_rows": _rate(
                sum(p["matched_shown_skus"] for p in ps), sum(p["shown_skus"] for p in ps)
            ),
            "price_completeness": _rate(sum(p["prices_correct"] for p in with_ref), ref),
            **{f"{f}_completeness": _rate(sum(p["filled"][f] for p in with_ref), ref) for f in _FIELDS if f != "price"},
            "wrong_prices": sum(len(p["prices_wrong"]) for p in ps),
            "extra_skus": sum(len(p["extra_skus"]) for p in ps),
            "skus_from_llm": sum(p["llm_skus"] for p in ps),
            "pages_with_model_call": sum(p["model_calls"] > 0 for p in ps),
            "model_calls": sum(p["model_calls"] for p in ps),
            "input_tokens": tin,
            "output_tokens": tout,
            "cost_usd": round(tin / 1e6 * price_in + tout / 1e6 * price_out, 5),
        }
    return rows
