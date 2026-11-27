# One-command entry points. Everything runs through `uv`; see README §5.
.PHONY: setup demo demo-offline tour test

RUN_ID ?= demo_$(shell date +%Y%m%d_%H%M%S)
MODE ?= hybrid
DEMO_DB ?= data/demo.db
DEMO_OUT ?= outputs/demo
OFFLINE_DIR ?= data/demo_offline

setup:
	uv sync --frozen
	uv run playwright install chromium

# Live, bounded crawl of both configured categories (configs/safco.yaml budgets: 10 products per category,
# 150 pages), then JSON/CSV export, run report and a Markdown summary with the data-quality tables.
# MODE=hybrid uses the model from .env; without credentials it still completes and reports MODEL_UNAVAILABLE.
demo: setup
	uv run catalog-agent crawl --mode $(MODE) --db $(DEMO_DB) --run-id $(RUN_ID) > /dev/null
	uv run catalog-agent export --db $(DEMO_DB) --run-id $(RUN_ID) --out-dir $(DEMO_OUT) > /dev/null
	uv run catalog-agent report --db $(DEMO_DB) --run-id $(RUN_ID) --out-dir $(DEMO_OUT) > /dev/null
	uv run catalog-agent summary --db $(DEMO_DB) --run-id $(RUN_ID) --out $(DEMO_OUT)/summary.md

# The same pipeline offline on the saved fixtures (no network, no model credentials needed).
demo-offline:
	uv sync --frozen
	uv run catalog-agent crawl --mode deterministic --replay tests/fixtures/live --db $(OFFLINE_DIR)/catalog.db \
	  --run-id $(RUN_ID) > /dev/null
	uv run catalog-agent export --db $(OFFLINE_DIR)/catalog.db --run-id $(RUN_ID) --out-dir $(OFFLINE_DIR)/out > /dev/null
	uv run catalog-agent report --db $(OFFLINE_DIR)/catalog.db --run-id $(RUN_ID) --out-dir $(OFFLINE_DIR)/out > /dev/null
	uv run catalog-agent summary --db $(OFFLINE_DIR)/catalog.db --run-id $(RUN_ID) --out $(OFFLINE_DIR)/out/summary.md

# One-minute offline tour: run -> budget stop -> resume -> output -> evidence (README "Two-minute path").
tour:
	uv sync --frozen
	./scripts/tour.sh

test:
	./scripts/smoke.sh
