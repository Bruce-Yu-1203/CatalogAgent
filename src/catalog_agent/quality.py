"""Run report with the Architecture §10.1 POC quality metrics, computed from one run's records."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from catalog_agent.storage.repository import Repository

PRODUCT_FIELDS = ["name", "brand", "manufacturer", "description", "category_paths", "image_urls", "attributes"]
VARIANT_FIELDS = [
    "sku",
    "variant_name",
    "attributes",
    "pack_label",
    "pack_quantity",
    "pack_unit",
    "price",
    "currency",
    "availability_raw",
]


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None  # zero denominators are reported as not applicable


def _filled(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        try:
            parsed = json.loads(v) if v[:1] in "[{" else v
        except json.JSONDecodeError:
            parsed = v
        return bool(parsed)
    return bool(v) or v == 0


async def build_report(repo: Repository, run_id: str) -> dict[str, Any]:
    run = await repo.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run {run_id}")
    tasks = await repo.tasks(run_id)
    products = await repo.products(run_id)
    counts = Counter(t["status"] for t in tasks)
    by_type = Counter(t["task_type"] for t in tasks)
    product_tasks = [t for t in tasks if t["task_type"] == "product"]
    done_product_tasks = [
        t for t in product_tasks if t["outcome_json"] and t["status"] in ("COMPLETED", "PARTIAL", "QUARANTINED")
    ]
    outcomes = {t["task_id"]: json.loads(t["outcome_json"]) for t in done_product_tasks}
    root_name = {}
    for t in tasks:
        if t["task_type"] == "listing" and t["url"] == t["root"]:
            root_name[t["root"]] = t["url"].rstrip("/").rsplit("/", 1)[-1]

    per_cat: dict[str, dict[str, Any]] = {}
    sku_owners: dict[str, set[str]] = defaultdict(set)
    for p in products:
        for v in p["variants"]:
            sku_owners[v["sku"]].add(p["product_id"])
    roots = sorted({t["root"] for t in product_tasks if t["root"]})
    for root in roots:
        cat_tasks = [t for t in done_product_tasks if t["root"] == root]
        cat_products = [p for p in products if p["root"] == root]
        cat_variants = [v for p in cat_products for v in p["variants"]]
        outs = [outcomes[t["task_id"]] for t in cat_tasks]
        n_pages = len(cat_tasks)
        missing_dist: dict[str, Counter] = defaultdict(Counter)
        for p in cat_products:
            for m in json.loads(p["missing_json"]):
                missing_dist[m["field"]][m["reason"]] += 1
            for v in p["variants"]:
                for m in json.loads(v["missing_json"]):
                    missing_dist["variants." + m["field"]][m["reason"]] += 1
        patches = sum(o.get("patches_applied", 0) + o.get("patches_rejected", 0) for o in outs)
        per_cat[root_name.get(root, root)] = {
            "root": root,
            "product_pages": n_pages,
            "accepted": sum(o["decision"] == "accepted" for o in outs),
            "partial": sum(o["decision"] == "partial" for o in outs),
            "quarantined": sum(o["decision"] == "quarantined" for o in outs),
            "products_exported": len(cat_products),
            "variants_exported": len(cat_variants),
            "field_fill_rate": {
                "product": {
                    f: _rate(
                        sum(
                            _filled(
                                p.get(f)
                                if f in ("name", "brand", "manufacturer", "description")
                                else p.get(f + "_json")
                            )
                            for p in cat_products
                        ),
                        len(cat_products),
                    )
                    for f in PRODUCT_FIELDS
                },
                "variant": {
                    f: _rate(
                        sum(_filled(v.get(f) if f != "attributes" else v.get("attributes_json")) for v in cat_variants),
                        len(cat_variants),
                    )
                    for f in VARIANT_FIELDS
                },
            },
            "validation_failure_rate": _rate(sum(bool(o.get("issues")) for o in outs), n_pages),
            "quarantine_rate": _rate(sum(o["decision"] == "quarantined" for o in outs), n_pages),
            "llm_fallback_rate": _rate(sum(bool(o.get("model_used")) for o in outs), n_pages),
            "evidence_rejection_rate": _rate(sum(o.get("patches_rejected", 0) for o in outs), patches),
            "variant_coverage": {
                "expected_skus": sum(o.get("expected_skus", 0) for o in outs),
                "exported_skus": sum(o.get("variants", 0) for o in outs),
            },
            "missing_reason_distribution": {k: dict(v) for k, v in sorted(missing_dist.items())},
        }

    listing_stops = Counter()
    pages_detail = []
    degraded_discovery = False
    for t in tasks:
        if t["task_type"] in ("listing", "discovery") and t["outcome_json"]:
            o = json.loads(t["outcome_json"])
            if t["task_type"] == "listing":
                listing_stops[o.get("pagination_stop") or "none"] += 1
            degraded_discovery |= bool(o.get("degraded_discovery"))
            pages_detail.append(
                {
                    "url": t["url"],
                    "type": t["task_type"],
                    "pagination_stop": o.get("pagination_stop"),
                    "children": o.get("children"),
                }
            )
    model_outs = [o for o in outcomes.values() if o.get("model_used") or o.get("degraded")]
    fallback_reasons = Counter(g for o in outcomes.values() if o.get("model_used") for g in o.get("gaps_initial", []))
    failed = [
        {
            "task_id": t["task_id"],
            "type": t["task_type"],
            "url": t["url"],
            "status": t["status"],
            "error_code": t["error_code"],
            "error": t["last_error"],
            "attempts": t["attempts"],
        }
        for t in tasks
        if t["status"] in ("FAILED", "QUARANTINED", "RETRY_WAIT")
    ]
    return {
        "run_id": run_id,
        "mode": run["mode"],
        "status": run["status"],
        "termination_reason": run["termination_reason"],
        "coverage_status": "partial" if run["status"] != "COMPLETED" else "unknown",
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "invocations": json.loads(run["invocations_json"]),
        "budgets": {
            "max_pages": run["max_pages"],
            "pages_used": run["pages_used"],
            "max_products_per_category": run["max_products_per_category"],
            "max_model_calls": run["max_model_calls"],
            "model_calls_used": run["model_calls_used"],
            "input_tokens_used": run["input_tokens_used"],
            "output_tokens_used": run["output_tokens_used"],
            "extensions": json.loads(run["extensions_json"]),
        },
        "tasks": {
            "by_status": dict(counts),
            "by_type": dict(by_type),
            "outstanding": sum(counts.get(s, 0) for s in ("PENDING", "RUNNING", "RETRY_WAIT")),
            "retries": sum(max(0, t["attempts"] - 1) for t in tasks),
        },
        "discovery": {
            "degraded_discovery": degraded_discovery,
            "listing_pagination_stops": dict(listing_stops),
            "navigation_pages": pages_detail,
        },
        "catalog": {
            "unique_products": len(products),
            "observed_skus": sum(len(p["variants"]) for p in products),
            "duplicate_identity_count": sum(1 for owners in sku_owners.values() if len(owners) > 1),
        },
        "model_usage": {
            "product_pages_with_model": sum(bool(o.get("model_used")) for o in outcomes.values()),
            "degraded": Counter(o.get("degraded") for o in model_outs if o.get("degraded")),
            "fallback_reasons": dict(fallback_reasons),
            "patch_entries_applied": sum(o.get("patches_applied", 0) for o in outcomes.values()),
            "patch_entries_rejected": sum(o.get("patches_rejected", 0) for o in outcomes.values()),
            "tool_calls": sum(o.get("tool_calls", 0) for o in outcomes.values()),
        },
        "extraction_seconds_total": round(sum(o.get("extract_seconds", 0) for o in outcomes.values()), 2),
        "per_category": per_cat,
        "failed_tasks": failed,
    }
