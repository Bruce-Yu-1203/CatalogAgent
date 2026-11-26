"""JSON and CSV exports following PRD §5.5/§5.6, scoped to one run."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from catalog_agent.contracts import Product, Variant

CSV_COLUMNS = [
    "run_id",
    "product_id",
    "product_key",
    "product_url",
    "product_name",
    "brand",
    "manufacturer",
    "description",
    "category_paths",
    "image_urls",
    "product_attributes",
    "alternatives",
    "variant_count",
    "record_status",
    "extraction_method",
    "product_llm_fields",
    "product_missing",
    "observed_at",
    "variant_id",
    "sku",
    "variant_name",
    "variant_attributes",
    "pack_label",
    "pack_quantity",
    "pack_unit",
    "price",
    "currency",
    "price_visibility",
    "availability",
    "availability_raw",
    "variant_llm_fields",
    "evidence",
    "variant_missing",
]


def rows_to_products(rows: list[dict[str, Any]]) -> list[Product]:
    out = []
    for r in rows:
        variants = [
            Variant(
                variant_id=v["variant_id"],
                product_id=v["product_id"],
                sku=v["sku"],
                variant_name=v["variant_name"],
                attributes=json.loads(v["attributes_json"]),
                pack_label=v["pack_label"],
                pack_quantity=v["pack_quantity"],
                pack_unit=v["pack_unit"],
                price=v["price"],
                currency=v["currency"],
                price_visibility=v["price_visibility"],
                availability=v["availability"],
                availability_raw=v["availability_raw"],
                llm_fields=json.loads(v["llm_fields_json"]),
                evidence=json.loads(v["evidence_json"]),
                missing=json.loads(v["missing_json"]),
            )
            for v in r["variants"]
        ]
        out.append(
            Product(
                product_id=r["product_id"],
                run_id=r["run_id"],
                source=r["source"],
                product_key=r["product_key"],
                url=r["url"],
                name=r["name"],
                brand=r["brand"],
                manufacturer=r["manufacturer"],
                description=r["description"],
                category_paths=json.loads(r["category_paths_json"]),
                image_urls=json.loads(r["image_urls_json"]),
                attributes=json.loads(r["attributes_json"]),
                alternatives=json.loads(r["alternatives_json"]),
                variant_count=r["variant_count"],
                record_status=r["record_status"],
                extraction_method=r["extraction_method"],
                llm_fields=json.loads(r["llm_fields_json"]),
                missing=json.loads(r["missing_json"]),
                observed_at=r["observed_at"],
                variants=variants,
            )
        )
    return out


def product_to_json(p: Product) -> dict[str, Any]:
    d = p.model_dump(mode="json")
    for v in d["variants"]:
        v.pop("product_id", None)  # implied by the parent in nested JSON
    return d


def write_json(products: list[Product], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([product_to_json(p) for p in products], indent=2, ensure_ascii=False) + "\n")
    return path


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _cell(v: Any) -> Any:
    return "" if v is None else v


def csv_rows(products: list[Product]) -> list[dict[str, Any]]:
    rows = []
    for p in products:
        base = {
            "run_id": p.run_id,
            "product_id": p.product_id,
            "product_key": p.product_key,
            "product_url": p.url,
            "product_name": p.name,
            "brand": _cell(p.brand),
            "manufacturer": _cell(p.manufacturer),
            "description": _cell(p.description),
            "category_paths": " | ".join(" > ".join(path) for path in p.category_paths),
            "image_urls": " | ".join(p.image_urls),
            "product_attributes": _j(p.attributes),
            "alternatives": _j([a.model_dump() for a in p.alternatives]),
            "variant_count": p.variant_count,
            "record_status": p.record_status,
            "extraction_method": p.extraction_method,
            "product_llm_fields": " | ".join(p.llm_fields),
            "product_missing": _j([m.model_dump(mode="json") for m in p.missing]),
            "observed_at": p.observed_at,
        }
        if not p.variants:
            rows.append({**base, **{c: "" for c in CSV_COLUMNS if c not in base}})
            continue
        for v in p.variants:
            rows.append(
                {
                    **base,
                    "variant_id": v.variant_id,
                    "sku": v.sku,
                    "variant_name": _cell(v.variant_name),
                    "variant_attributes": _j(v.attributes),
                    "pack_label": _cell(v.pack_label),
                    "pack_quantity": _cell(v.pack_quantity),
                    "pack_unit": _cell(v.pack_unit),
                    "price": _cell(v.price),
                    "currency": _cell(v.currency),
                    "price_visibility": v.price_visibility.value,
                    "availability": v.availability.value,
                    "availability_raw": _cell(v.availability_raw),
                    "variant_llm_fields": " | ".join(v.llm_fields),
                    "evidence": _j([e.model_dump() for e in v.evidence]),
                    "variant_missing": _j([m.model_dump(mode="json") for m in v.missing]),
                }
            )
    return rows


def write_csv(products: list[Product], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel opens UTF-8 directly
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(csv_rows(products))
    return path


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))
