#!/usr/bin/env bash
# Two-minute tour, fully offline (saved real pages, no network, no model credentials):
# run -> budget stop -> resume -> output -> evidence. Everything goes to a temp directory.
set -euo pipefail
cd "$(dirname "$0")/.."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DB="$WORK/catalog.db"
CFG="$WORK/tour.yaml"
# The default config with a deliberately small page budget, so the first run stops part-way.
sed -e 's/max_pages: 150/max_pages: 8/' configs/safco.yaml > "$CFG"
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
q() { uv run python - "$DB" "$1" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1]); cur = con.execute(sys.argv[2])
cols = [d[0] for d in cur.description]; rows = cur.fetchall()
w = [max(len(str(x)) for x in [c, *[r[i] for r in rows]]) for i, c in enumerate(cols)]
print("  ".join(c.ljust(w[i]) for i, c in enumerate(cols)))
for r in rows: print("  ".join(str(x).ljust(w[i]) for i, x in enumerate(r)))
PY
}

step "1. Run: crawl both categories from saved pages, page budget 8"
uv run catalog-agent crawl --config "$CFG" --mode deterministic --replay tests/fixtures/live --db "$DB" \
  --run-id tour > "$WORK/crawl.json" 2> "$WORK/crawl.log"
q "SELECT run_id, status, termination_reason, pages_used, max_pages FROM crawl_runs"
q "SELECT task_type, status, count(*) AS tasks FROM crawl_tasks GROUP BY 1, 2 ORDER BY 1, 2"
echo "(PENDING tasks are kept for a resume. FAILED = links with no saved page in this offline set: only 3 listings"
echo " and 4 product pages are saved, the rest are recorded as NOT_FOUND, hence COMPLETED_WITH_ERRORS below.)"

step "2. Recover: resume the same run with 60 more pages (completed tasks are not fetched again)"
uv run catalog-agent resume --db "$DB" --run-id tour --add-pages 60 > "$WORK/resume.json" 2> "$WORK/resume.log"
q "SELECT run_id, status, termination_reason, pages_used, max_pages FROM crawl_runs"
q "SELECT count(*) AS tasks, count(DISTINCT task_key) AS distinct_tasks FROM crawl_tasks"

step "3. Output: products and variants (also exported as JSON and CSV)"
uv run catalog-agent export --db "$DB" --run-id tour --out-dir "$WORK/out" > /dev/null
q "SELECT name, brand, variant_count, record_status FROM products ORDER BY name"
q "SELECT sku, variant_name, price, currency, availability, pack_quantity, pack_unit FROM variants
   WHERE sku IN ('1027990', '1027993') ORDER BY sku"

step "4. Evidence: where SKU 1027990's SKU and price came from in the saved page"
q "SELECT json_extract(e.value,'$.field') AS field, json_extract(e.value,'$.section_id') AS section,
          json_extract(e.value,'$.row_id') AS row_id, json_extract(e.value,'$.raw_value') AS raw_value,
          json_extract(e.value,'$.method') AS method
   FROM variants v, json_each(v.evidence_json) e
   WHERE v.sku = '1027990' AND json_extract(e.value,'$.field') IN ('sku', 'price', 'availability_raw')"

printf '\nThe real sample is in outputs/ (89 products, 556 SKUs); why an LLM: README section 9.\n'
