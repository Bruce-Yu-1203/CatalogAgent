# Implementation log

What was built per milestone, the decisions taken, the PRD §9 assumptions checked, the tests actually
run with their real results, and the gaps. Entries are factual records of this implementation; nothing
here is a plan or an estimate unless it says so.

Implementation date: 2026-11-26. Environment: macOS (arm64), Python 3.12 via uv 0.12, Playwright Chromium.

## M0 — Site spike

**Done**

- Read `robots.txt` and the terms of sale. robots.txt allows `/catalog/*` and `/product/*`, disallows
  `/*?page=` and `/*?p=` (pagination), `/catalog/product/view/id/*`, account and checkout paths, and
  lists `sitemap.xml`. The terms of sale have no clause on scraping or automated access.
- Fetched the gloves listing with plain HTTP and with Playwright, recorded network calls, then
  inspected the rendered DOM of `/catalog`, both category roots, one subcategory listing and three
  product pages. Every live request was paced at ≥ 2–3 s; about 12 page loads in total during the spike.
- Saved seven real rendered snapshots as gzipped fixtures in `tests/fixtures/live/` (see the README there).

**Findings**

| Topic | Observation |
|---|---|
| Rendering | Listing products come from Algolia InstantSearch on `*.algolia.net` (client-side). Plain HTTP gets navigation only. |
| Navigation | `/catalog` lists 20 category roots (`/catalog/<slug>`). Each listing page links to its subcategories (`/catalog/<root>/<child>`). |
| Listings | 12 hits per page (`.ais-Hits-item a.result`), result count in `.ais-Stats` (gloves: 92, sutures & surgical: 56). |
| Pagination | `?page=N` links, disallowed by robots.txt. No load-more control. |
| Product page | schema.org `ProductGroup` JSON-LD with `hasVariant` (sku, name, price, availability), `BreadcrumbList`, and a rendered variant table `#pdp-grouped .table-grid-pdp` with `Item #`, `Product Name`, `Mfr #`, `Description`, `Stock Availability`, and quantity-tier prices. Some tables are grouped under headings (e.g. suture needle type). |
| Prices | Public, no sign-in. Tier prices (qty 1, qty 10, …); the qty-1 tier is exported as the unit price. |
| Rendering race | On one page (`soft-tissue-trimmer`) the variant table had not rendered after scrolling; JSON-LD was present. Kept as fixture `product_trimmer_incomplete_render`. |
| Alternatives / specifications | No "alternatives" or specification table on the sampled pages; recommendation carousels are third-party recs, not page-listed alternatives. |

**Crawl strategy chosen**

1. Discovery task on `/catalog` → category roots (`selected` keeps the two configured roots; `all` keeps all).
2. Each root listing → subcategory listings + product links on page 1. The next-page link is detected,
   checked against robots.txt and recorded as `next_page_robots_disallowed`; it is not fetched.
3. Product pages → variant table rows as primary evidence; JSON-LD rows as the fallback source when
   the table did not render on the final attempt.

**Field mapping (PRD §5)**

| Field | Source |
|---|---|
| `product_key` | JSON-LD `productGroupID` (source product ID), else URL path |
| `name`, `description`, `brand`, `image_urls` | JSON-LD `ProductGroup` (`description` from the rendered `#description` section when present) |
| `manufacturer` | null, reason `not_applicable`: the page shows one manufacturer label identical to the brand |
| `category_paths` | Listing breadcrumbs under which the product was discovered ∪ the product breadcrumb (minus Home and the product name) |
| `attributes` | `Key: value` bullet items in the description |
| `alternatives` | empty (none listed on the sampled pages) |
| variant `sku` | `Item #` cell (table) or JSON-LD `sku`; verbatim |
| `variant_name` | `Description` cell (e.g. `X-small, 300/box`) |
| `attributes` (variant) | table group heading as `Group`; other attributes only via the model (see M3) |
| `pack_label/quantity/unit` | last `N/unit` token in the variant description |
| `price`, `currency` | qty-1 tier price cell, `$` → USD (JSON-LD `price` + `priceCurrency` as fallback) |
| `availability`, `availability_raw` | `Stock Availability` cell (JSON-LD availability as fallback) |

## M1 — Vertical slice

**Done**: `pyproject.toml` + `uv.lock` (Python 3.12 pinned), Typer CLI, YAML + Pydantic config, Safco
adapter, Classifier and Navigator rules, deterministic Extractor, Validator, SQLite storage and JSON/CSV
export.

**Live slice run** `run_m1_slice` (deterministic mode, `max_products_per_category: 2`, `max_pages: 20`,
scratch database, not exported to `outputs/`): 20 page fetches in ≈48 s, 3 products (2 gloves,
1 sutures & surgical) with 12 variants reached SQLite and `products.json`. The run stopped with
`BUDGET_EXHAUSTED` / `MAX_PAGES` because the 14 subcategory listings are fetched before products
(navigation tasks have higher priority). This met the M1 exit criterion (≥ 1 real product per category with
variant data in typed storage and JSON).

Note on commits: the M1 commit already contained the runtime and agent code that M2/M3 exercise (lock,
recovery, LangGraph loop). Those parts were not validated until the M2/M3 tests below were written; the
M2 and M3 commits add the tests and the fixes they found.

## M2 — Durable runtime

**Done**: SQLite frontier and catalog (WAL, busy timeout), `fcntl.flock` ownership, transactional
enqueue/commit, lineage merge for products found under several categories, restart reconciliation,
Recovery Agent retry policy (exponential backoff with jitter, Retry-After, pause on 401/403/CAPTCHA),
host pacing, page/product/model budget reservation, resume with `--add-pages`, `--add-model-calls` and
`--add-products`, offline `ReplayFetcher` and the `--replay` / `--db` CLI options.

**Decisions**

- Task priority is discovery → listings → products, so category paths are known before products run.
  A listing or product whose category already has its product quota is not dispatched (saves fetches).
- `--add-products` was added beside the documented `--add-pages` / `--add-model-calls`: a run stopped by
  the per-category product budget could otherwise never be extended (Tech Stack §7 updated).
- 401/403/CAPTCHA pause the whole run (single host = single scope) and leave the task resumable.

**Bug found by the tests**: `claim_next` ended a run as budget-exhausted while an eligible task was still
waiting to retry (flaky 429 test). Fixed: the run waits for retries whose category quota is not full and
only then reports the product budget. A related infinite wait (retry under a full quota) was prevented
in the same change.

## M3 — LLM agents and quality gate

**Done**: Normalizer (decimal-string prices, `N/unit` packs, availability enum), SKU/price evidence with
exact same-row cell checks, `llm_fields` tagging, Validator issues and accept/partial/quarantine,
LangGraph Extractor loop (`plan → call_model → tools | apply → plan`) with read-only snapshot tools
(`inspect_sections`, `get_section`, `get_variant_rows`) and `submit_patch` as typed structured output,
model/tool/token/time budgets enforced in code, run-level model budget reserved per task and settled
after it, `LangChainModelClient` (Anthropic via `langchain-anthropic`).

**Recoverable gaps** that trigger the model in hybrid mode: (1) a SKU observed on the page (table or
JSON-LD) but missing from the candidate, (2) a displayed price that does not normalize, (3) variant
label text with no structured attributes. A field that is not displayed never triggers the model.

**Not demonstrated live**: no model credentials were available (A-07 unverified). The M3 exit criterion
"one reproducible fallback case passes through the model and independent validation" is met only with a
scripted fake model on a labeled synthetic gap (a real page with one table row removed); see
`tests/test_agent_loop.py`. Fix found: a price shown in the row but not extracted is now reported as
`parse_failed` (it was `not_visible`), which makes the record `partial`.

## M4 — Robustness and evaluation

**Done**: root-selection unit test for `all`/`selected` on the saved `/catalog` snapshot, crash/resume,
limits and access policy tests, §10.1 metrics in `run_report.json`, `scripts/smoke.sh`.

**Not done**: baseline-vs-hybrid evaluation (needs a model; listed as "if time permits" and P1 FR-13).
Instead, an independent consistency check was run on the live output: for every exported price taken
from the rendered table, the page's own JSON-LD offer price for the same SKU was compared.

## M5 — Submission artifacts

**Live run** `run_20261126_live`, deterministic mode, default `configs/safco.yaml`, 2026-11-26 23:07–23:10 UTC:

| Measure | Value |
|---|---|
| Wall time | 206 s, one invocation |
| Fetch attempts | 48 / 150 (1 discovery, 16 listings, 31 product-page attempts) |
| Products | 20 accepted (10 per category), 0 partial, 0 quarantined, 0 failed |
| Variants | 93 (gloves 53, sutures & surgical 40); expected SKUs observed on the pages: 93 |
| Retries | 10, all `INCOMPLETE_RENDER` on 5 bone-graft pages whose variant table never rendered; on the final attempt their 29 variants came from JSON-LD |
| Pending | 69 tasks (68 products + 1 retry) left by the 10-per-category budget; status `BUDGET_EXHAUSTED` / `MAX_PRODUCTS_PER_CATEGORY` |
| Pagination | 6 listings stopped at `next_page_robots_disallowed`, 10 at `last_page` |
| Duplicate SKU identities | 0 |
| Table price vs JSON-LD price | 64 agree, 0 disagree (29 prices were JSON-LD only) |
| SKU set vs JSON-LD SKU set | equal on 20/20 products |
| Variant attribute fill rate | gloves 19% (group heading only), sutures 0% |
| Pack fill rate | gloves 100%, sutures & surgical 30% (`Box of 20`, `1cc jar`, `5-pack` not parsed) |

Outputs written from this run: `outputs/products.json`, `outputs/variants.csv`, `outputs/run_report.json`,
`outputs/failed_tasks.json`. README written per Roadmap §7.

## Tests actually run

`uv run pytest` (2026-11-26): **37 passed in 13.5 s**, offline.

| File | Tests | Covers |
|---|---|---|
| `test_discovery.py` | 10 | `selected` → exactly 2 roots, `all` → 20 roots, degraded discovery, in-scope children once with lineage, off-scope rejection, robots pagination stop, permitted pagination, last page, robots wildcards, Playwright readiness on a delayed container (local route, no network) and incomplete render |
| `test_extraction.py` | 5 | 15 distinct sutures SKUs with row-correct price/pack, JSON-LD fallback, no visible price (`not_visible`), `login_required`, JSON/CSV round trip with evidence |
| `test_agent_loop.py` | 9 | deterministic mode never calls the model, valid patch accepted with `llm` evidence, SKU/price outside the cited row rejected, known value not overwritten, no-progress stop, tool/call budgets, model error, missing key, instruction-like page text, run-level budget reservation/settlement |
| `test_persistence.py` | 6 | same task twice, injected failure before commit (product and navigation), crash + resume only after the lock, second process fails fast (real subprocess), same product in two runs |
| `test_runtime.py` | 7 | backoff bounds, 429 + Retry-After, 403 pause without bypass, CAPTCHA page, page budget + plain/extended resume + stored config, time limit → `PAUSED` → resume, bounded transient retries |

`./scripts/smoke.sh`: passed (`uv sync --frozen`, ruff check, ruff format --check, pytest, replay crawl →
export → report → resume; replay result `COMPLETED_WITH_ERRORS`, 3 products, 22 variant rows, because
fixtures exist only for 7 URLs and the rest replay as 404).

Earlier in development the full suite was run three times in a row to check for flakiness after the
retry-wait fix: 23/23 passed each time (before the extraction and agent-loop files existed).

## P0 checklist status (Roadmap §2)

| Item | Status |
|---|---|
| Permitted access, scoped config | Done (robots.txt + terms checked; robots enforced in code) |
| Discover both roots, traverse descendants/listings | Done (page 1 of each listing only; see ADR-11) |
| Dynamic content / pagination, stop reason | Done (Playwright readiness; stop reasons recorded) |
| Product fields and every observed SKU, both categories | Done (93/93 observed SKUs) |
| Normalize/validate to PRD §5, missing reasons | Done |
| Separate agents behind one contract; LLM-assisted extraction | Done in code; **LLM path verified only with fake models** |
| Persistence, lock, resume, retries, rate limit, atomic, idempotent | Done and tested |
| `selected`/`all` switch, root-selection test, README explanation | Done |
| P0 test suite passing | Done (37/37) |
| Genuine JSON/CSV + run report with §10.1 metrics | Done (`run_20261126_live`) |
| Clean Git repo, secret-free config, README per §7 | Done locally; **not pushed** from this environment |

## Gaps and follow-ups

1. Run a hybrid crawl with real credentials (`LLM_PROVIDER=anthropic`, `LLM_MODEL`, `LLM_API_KEY`) to verify
   A-07 and measure what the attribute gap repair adds; then build the P1 baseline-vs-hybrid evaluation.
2. ~~Accept JSON-LD after the first missing-table attempt~~ (done in M6).
3. ~~Extend pack parsing and strip tooltip text from variant names~~ (done in M6).
4. Find a robots-compliant route past page 1 (e.g. `products.xml` sitemap filtered by category) before any
   full-site crawl.
5. The live run used `deterministic` mode while the CLI default is `hybrid`; without credentials a hybrid
   run degrades and reports `MODEL_UNAVAILABLE`.

## M6 — Verification and P0 gap fixes (second session, 2026-11-26)

**Fixes** (commit "M6: accept structured data…", each with a regression test):

1. A product page whose variant table never renders is accepted on the first attempt when its
   ProductGroup JSON-LD is present (`READY_FALLBACK_SELECTORS` in the adapter; both fetchers). The M5 run
   spent 10 retries on such pages.
2. Pack parsing also reads `Box of N` and `N-pack`. Volume labels (`1cc jar`) still stay null.
3. Variant labels use only the description label element, not the SDS link and "Details" tooltip that
   share the cell. New real fixture: `product_orasoothe_tooltips.html.gz` (captured in the M5 run).

**Final live run** `run_20261126_live2` (deterministic, default config, started 23:31 UTC), which
replaced the M5 run's files in `outputs/`:

| Measure | M5 run `run_20261126_live` | Final run `run_20261126_live2` |
|---|---|---|
| Wall time / fetches | 206 s / 48 | 122 s / 37 |
| Retries | 10 | 0 |
| Products | 20 accepted | 20 accepted (Feather blades replaced by Soft Tissue Trimmer, which had been stuck in retry) |
| Variants | 93 | 91 (53 gloves, 38 sutures & surgical); 91/91 observed SKUs exported |
| Pages using JSON-LD as evidence | 5 | 6 |
| Table price vs JSON-LD price | 64 agree / 0 disagree | 60 agree / 0 disagree (31 JSON-LD only) |
| SKU set equals JSON-LD SKU set | 20/20 | 20/20 |
| Sutures & surgical pack fill rate | 30% | 34% |
| Duplicate SKU identities | 0 | 0 |

**Spot check**: sample records compared with their saved snapshots (Safco Nitrilex Surpass: 5 SKUs, $15.99
each, 300/box or 250/box; HemoStat: 1 SKU, "Box of 20", $125.99; Ossify bone graft: 3 SKUs from JSON-LD,
pack null). The values matched what the pages showed at capture time. This was checked against saved
snapshots, not by reloading the live pages.

**Tests**: `uv run pytest` 41 passed in 16.1 s (discovery 11, extraction 8, agent loop 9, persistence 6,
runtime 7). `./scripts/smoke.sh` passed (replay: 4 products, 25 variant rows).

**P0 status after M6**: every P0 checklist item is done and tested, with two open items:
the LLM path has been verified only with fake models (needs `LLM_API_KEY` for a live hybrid run), and the
commits are local (not pushed). P1 is not started: AGENTS.md and the constitution allow P1 only after all
P0 gates pass, and the live-model gate is still open.
