#!/usr/bin/env bash
# Smoke test: fresh environment, lint, offline test suite, then the README commands end to end
# against saved fixtures (replay). Never touches the live site; outputs go to a temp dir, not outputs/.
set -euo pipefail
cd "$(dirname "$0")/.."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

uv sync --frozen
uv run ruff check
uv run ruff format --check
uv run pytest -q

DB="$WORK/catalog.db"
uv run catalog-agent crawl --config configs/safco.yaml --mode deterministic --replay tests/fixtures/live \
  --db "$DB" --run-id smoke_replay > "$WORK/crawl.json"
uv run catalog-agent export --db "$DB" --run-id smoke_replay --format all --out-dir "$WORK/out" > /dev/null
uv run catalog-agent report --db "$DB" --run-id smoke_replay --out-dir "$WORK/out" > /dev/null
uv run catalog-agent resume --db "$DB" --run-id smoke_replay > "$WORK/resume.json"

uv run python - "$WORK" <<'PY'
import csv, json, sys
w = sys.argv[1]
crawl = json.load(open(f"{w}/crawl.json")); resume = json.load(open(f"{w}/resume.json"))
products = json.load(open(f"{w}/out/products.json"))
rows = list(csv.DictReader(open(f"{w}/out/variants.csv", encoding="utf-8-sig")))
report = json.load(open(f"{w}/out/run_report.json"))
assert products and rows, "no replay output"
assert {p["run_id"] for p in products} == {r["run_id"] for r in rows} == {"smoke_replay"}
assert report["run_id"] == "smoke_replay" and report["per_category"]
assert resume["status"] == crawl["status"]
print(f"smoke ok: status={crawl['status']} products={len(products)} variant_rows={len(rows)} "
      f"categories={len(report['per_category'])}")
PY
