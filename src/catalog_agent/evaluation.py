"""P1 (FR-13): offline deterministic-vs-hybrid evaluation on frozen, labelled fixtures.

Both modes run the same Extractor + Validator on the same saved snapshots; no page is fetched.
Replay results describe replay behaviour only (A-11) and are reported separately from live runs.
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from catalog_agent.agents.extractor import ExtractorAgent, ExtractorInput
from catalog_agent.agents.validator import ValidatorAgent
from catalog_agent.config import AppConfig
from catalog_agent.runtime.fetcher import make_snapshot


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
