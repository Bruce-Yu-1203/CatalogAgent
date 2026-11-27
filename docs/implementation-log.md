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

**Not done**: baseline-vs-hybrid evaluation (needs a model; optional in M4 and P1 FR-13).
Instead, an independent consistency check was run on the live output: for every exported price taken
from the rendered table, the page's own JSON-LD offer price for the same SKU was compared.

## M5 — Release artifacts

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

## Live model verification (2026-11-27)

Credentials were added by Amy in `.env` (`LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-haiku-5-5`; the key was
not read or logged).

**Changes before the run**

- `max_output_tokens_per_call` 1200 → 4000 and `output_config.effort = "low"`: current Claude models think
  adaptively and thinking counts toward `max_tokens`.
- The variant rows are sent in the first user message (wrapped as untrusted page data), and the system
  prompt says to submit directly. On the first try the model spent all 3 allowed calls on read tools for
  the 15-row SureStitch page without submitting (rows were truncated at 4000 chars);
  `max_tool_result_chars` was raised to 12000 and `max_input_tokens_per_call` to 8000.
- Prompt rule: don't repeat values that have their own field (pack, stock, item/manufacturer number); use
  consistent Title Case keys. Added after the first hybrid run produced keys like `Pack`, `Stock`, `MFR No`.

**Runs**

| Run | Mode | Result |
|---|---|---|
| Saved-snapshot smoke test | hybrid, 2 pages | Synthetic gap page: missing SKU recovered with verified JSON-LD evidence, accepted (3 calls). SureStitch: 15/15 variants got Needle/Material/Size/Length in 1 call after the prompt change. A rerun of the synthetic gap page returned the SKU without its price, so the Validator marked the record `partial`. |
| `run_20261127_hybrid` | hybrid, live | 20 products, 20 calls, 91 patch entries applied, 1 rejected; attribute keys inconsistent (`Pack`, `Count`, `MFR No`). Not exported. |
| `run_20261127_hybrid2` | hybrid, live, final | 20 accepted products, 91 variants, 37 fetches, 0 retries, 139 s; 20 model calls, 0 tool calls, 41,684 input + 17,012 output tokens (≈ $0.013 at $0.10/$0.50 per M); 90 patch entries applied, 0 rejected; 19 products `hybrid`, 1 `deterministic` (HemoStat, nothing to split). **Now the source of `outputs/`.** |

**Comparison on the same snapshots** (deterministic parser re-run on `run_20261127_hybrid2`'s pages): SKU sets
identical on 20/20 products; 0 differences in price, pack or availability across 91 variants; variants with
attributes beyond the group heading 0/91 → 90/91. Table price vs JSON-LD: 60 agree, 0 disagree, 31 JSON-LD only.

**Manual check of 20 random hybrid variants**: 17 correct; 1 wrong (`Scent: Hygiene` for "Oral Coating Rinse -
Hygiene, 3.4 oz Bottle"); 2 debatable keys (`Length: long` for a trimmer model, `Form: Gel` taken from the
product name). All 20 values appear in their own row's text.

**Tests**: 41 passed; `./scripts/smoke.sh` passed.

**P0 status**: every P0 checklist item is now done and exercised, including LLM-assisted extraction against a
real model. Remaining non-P0 items: commits are not pushed; P1 not started.

## Doc cleanup and rename (2026-11-27)

At Amy's request the docs were cleaned of references to the original project brief and its deadline
(constitution amended to 1.1.0, then retitled "CatalogAgent Constitution" in 1.1.1). The Tech Stack repository layout now matches the files that exist. The project
name is CatalogAgent; Amy renamed the GitHub repo to `CatalogAgent`
(https://github.com/Bruce-Yu-1203/CatalogAgent) and the local remote was updated to match. `main` was first pushed to GitHub at `d3ae07c`.

## P1 (2026-11-27)

P1 started after every P0 gate passed, including the live model check. Items in roadmap order:

| Item | What was built | Tests |
|---|---|---|
| FR-11 classifier fallback | One reserved model call for `unknown` pages; closed enum + cited section IDs; accepted only if `adapter.type_signals` confirms the type's DOM signals; otherwise recorded for review. New `page-text` and `listing-hits` sections. Off by default. | `test_classifier_fallback.py` (6) |
| FR-12 repair suggestions | Parsers record field-selector failures per template (`FIELD_SELECTORS`, `field_failures`). After `trigger_after_failures`, the Recovery agent gives the model a tag/class outline, lets it call `test_selector` on frozen failing + passing snapshots, and ranks up to 3 candidates by regressions, then coverage. Written to `run_report.json` / `repair_suggestions.json`; never applied. Off by default. | `test_repair_suggestions.py` (5) |
| Evidence beyond SKU/price | `pack_label`, `availability_raw` and each `attributes.<key>` carry evidence refs on the SKU's row (exact match for availability and the group heading, containment for pack/attributes); unverifiable values are dropped with `parse_failed`. PRD §5.2/5.3, `EvidenceRef`, round-trip test updated together. | `test_extraction.py` (+2) |
| Second recovery case | Rejected patch entries return typed issues (`sku_not_in_cited_row`, `price_not_in_own_row`, `attribute_not_in_row`, `schema`); a rejected patch earns another turn, identical rejections twice stop. Run report adds recovery attempted/resolved/success rate. | `test_agent_loop.py` (+2) |
| FR-13 evaluation | `catalog-agent evaluate` over `tests/fixtures/eval/` (21 real pages, 17 held out, + 1 labelled synthetic). Labels: SKU/price from JSON-LD; attribute values written by Claude from the variant labels (not human-reviewed); keys not scored. | `test_agent_loop.py` (+1, offline) |
| Adversarial tests | Malformed patches, unknown tools/bad args, bounded tool results, next-page self-cycle (`cycle_detected`, new in the Navigator), A→B→A cursor cycle ending through task dedup. | `test_adversarial.py` (5) |

Not done: the optional short demo.

**Evaluation results** (held-out real pages; full detail in `outputs/evaluation_report.json`, which holds run 3):

| | Deterministic | Hybrid run 1 | Hybrid run 2 | Hybrid run 3 |
|---|---|---|---|---|
| Variant recall / price accuracy | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 / 1.0 |
| Attribute recall / precision | 0.0 / n/a | 1.0 / 0.99 | 0.54 / 0.99 | 0.96 / 0.99 |
| Model calls / cost | 0 / $0 | 18 / $0.012 | 20 / $0.011 | 22 / $0.014 |

Between runs: run 2 added a prompt line asking for the row's price cell on missing variants; it fixed the
synthetic page (now `accepted` with a verified price) but one 18-variant page used all 3 calls without submitting.
Run 3 sends attribute gaps 8 SKUs per turn; recall went back to 0.96. The variance between runs is itself a
finding: attribute extraction with the model is useful but not stable.

**Live run with all P1 features on** `run_20261127_p1` (scratch config with `llm_fallback_enabled` and
`repair_suggestions_enabled` set to true; `configs/safco.yaml` keeps them off), now the source of `outputs/`:
20 accepted products, 91 variants, 37 fetches, 0 retries, 154 s; 27 model calls (20 extraction, 3 repair, 0
classifier fallback because no page was `unknown`), 70,115 + 20,481 tokens ≈ $0.017; recovery 19/20 pages; 0
differences in SKU/price/pack/availability versus the deterministic parser on the same snapshots; evidence refs:
91 SKU, 91 price, 91 availability, 66 pack, 139 attribute. Repair suggestion: `variant_rows` failed on 6 pages;
the model tested selectors and concluded, correctly, that none can match because the table never renders
("Loading..." only), so the ranked list holds just the unchanged current selector. Nothing was applied.

**Tests**: 62 passed; `./scripts/smoke.sh` passed.

Found while testing: an unclosed SQLite connection after a failing test keeps the pytest process alive (aiosqlite's
worker thread), which looked like a hang. The P1 tests close the repository in `finally`; older P0 tests do not yet.

## Alternatives from the Trending Products carousel (2026-11-27)

Checked against the requirements document Amy shared: every listed field was covered except "alternative
products" (`alternatives` was always empty because the crawler blocked the Adobe recommendations service
that renders Safco's carousels). Amy chose to collect the "Trending Products" carousel only.

**Built** (commit "Collect alternatives from the Trending Products carousel"):
- `commerce.adobe.io` removed from the blocked hosts; every other third party stays blocked.
- After the product readiness signal, a best-effort wait (≤ 6 s, no error) for the carousel. A live probe
  showed it renders without scrolling in ≈ 0.8 s.
- `parse_alternatives`: items of the rendered "Trending Products" slider only, taken from the DOM as rendered
  (no arrow clicks, no scrolling), canonical product URLs, the current product excluded, `sku` null (the
  rendered card has no SKU), `source: "trending_carousel"`. "Best Value Options" is skipped: on the sampled
  pages it lists Safco private-label items of other types (prophy angles, curettes, saliva ejectors on a
  glove page), i.e. cross-sell.
- Empty `alternatives` now records `missing`: `not_observed` (carousel did not render) or `not_visible`.
- Schema change in one commit (constitution VIII): PRD §5.1/§5.5, `Alternative` model (`source`), exporters
  (serialize the new key unchanged), JSON/CSV round-trip test; plus 2 new tests (Trending only, self excluded,
  `not_observed`). 64 tests pass. Fill rate for `alternatives` added to the run report.

**Live re-crawl** `run_20261127_alts` (same scratch config as `run_20261127_p1`, hybrid, P1 features on),
now the source of `outputs/`: 20 accepted products, 91 variants, 37 fetches, 0 retries, 146 s; 160 alternatives
(8 per product, all `trending_carousel`, none pointing at the product itself); 26 model calls, 64,607 + 17,323
tokens ≈ $0.015.

**Stability checks** on SKU, price, pack and availability:
1. New code, deterministic re-parse of this run's snapshots vs the export: 0 differences in 91 variants.
2. New code on the previous run's (`run_20261127_p1`) snapshots vs that run's export: 0 differences.
3. This run vs the previous run: same 20 products, same 91 variants, 0 field differences (the site did not
   change between the two runs).

The model results moved with the usual run-to-run variance: attributes on 80/91 variants (previous run 90/91),
recovery 18/20 pages, and the repair agent returned no candidate this time (previous run: a correct "no
selector can fix this" diagnosis).

## Sampling and reporting fixes (2026-11-27)

Amy reviewed `run_20261127_alts` and found four problems; all four were confirmed in the database first.

1. **No sutures in the sutures sample.** The 10 sutures & surgical products were bone grafts, haemostatics and a
   trimmer; the 5 products of the Sutures subcategory (and every latex, chloroprene and surgical glove) stayed
   PENDING. Cause: product tasks were claimed in discovery order, so the 12 products of each root's first
   listing page used the whole 10-per-category budget. **Fix:** `claim_next` now picks the product whose
   subcategory (deepest discovered category path) has been sampled least, ties by discovery order. Test:
   `test_product_claims_rotate_across_subcategories`.
2. **README model-call split was wrong.** It said 20 extraction + 6 repair; the database shows 23 extraction (18
   pages × 1, Alasta × 2, OSSIF-i × 3, because attribute gaps go 8 SKUs per call) + 3 repair. Total 26 was right.
   The README now states the split per page from the current run.
3. **Repair suggestions triggered by an expected fallback.** Pages whose variant table never renders were
   counted as `variant_rows` selector failures, so every run spent 3 model calls to conclude "no selector can
   fix this". **Fix:** a table container that only shows its loading placeholder is recorded as the render note
   `table_not_rendered` (reported in `run_report.json`) and no longer counts as a field failure; a rendered table
   whose SKU selector matches nothing still does. Test: `test_unrendered_table_is_a_render_note_not_a_selector_failure`.
4. **README §9 used an older run.** §9 now leads with the current run and the offline evaluation; earlier runs
   appear only as an example of run-to-run variance.

**Live re-crawl** `run_20261127_balanced` (same scratch config, hybrid, P1 features on), now the source of
`outputs/`: 20 accepted products covering every subcategory (gloves: chloroprene 2, latex 2, nitrile 2,
surgical 2, vinyl 1, holder 1; sutures & surgical: all 8 subcategories, including Safco SureStitch Sutures with
15 variants), 87 variants, 37 fetches, 0 retries, 131 s; 24 model calls (all extraction), 57,129 + 18,048 tokens
≈ $0.015; 0 field failures, 3 `table_not_rendered` notes, no repair suggestions; attributes on 87/87 variants;
160 alternatives. Checks: SKU sets equal JSON-LD on 20/20 pages; 78 table prices agree with JSON-LD, 0 disagree,
8 from JSON-LD; deterministic re-parse of the same snapshots gives 0 differences in price, pack and
availability. New real edge case: Aurelia Refresh x-large (`4870048`) shows "Non-purchasable" and no price in
the table while JSON-LD lists $10.49 in stock; the export keeps price null (`not_visible`) and availability
`unknown` with the raw text, because only displayed values are exported.

Tests: 66 passed; smoke script passed. The project folder was renamed to `CatalogAgent` on Amy's machine during
this work; nothing in the code depends on the folder name.

## Review fixes: price conflicts and full-site evidence (2026-11-27)

1. **Conflicting duplicate prices were accepted silently.** Reproduced: a copy of the real Surpass page with
   its first row repeated at `$999.99` came out `accepted` with no issue, because `parse_product` dropped repeated
   SKUs before the Validator saw them. **Fix:** the parser keeps every row; the Validator's existing ambiguity
   check now sees both rows and quarantines the product (`sku_ambiguous`, critical); consistent duplicates are
   merged with an info issue `duplicate_row_merged`. Two regression tests go from HTML to the validation
   decision. Re-validating the 20 current live snapshots and all eval fixtures changed nothing (the real data
   has no duplicate rows). 68 tests pass.
2. **Full-site coverage has no evidence yet.** Measured from the current run's listing pages: gloves report 92
   products and 39 unique were discovered (page 1 of the root and subcategories); sutures & surgical report 56
   and 50 were discovered. One request to `products.xml` (the product sitemap listed in robots.txt) returned
   3,980 product URLs with `lastmod`; all 89 discovered products are in it. The sitemap has no categories, so a
   full crawl would fetch every product page and read its category. README §10 and §12 now say full coverage
   is not proven and give these numbers. Seeding from the sitemap is not built.

## Sitemap sample and README §7 (2026-11-27)

**Sitemap sample.** New option `crawl --sitemap-sample N --sample-seed S`: one robots-checked, paced request to
`products.xml`, then N randomly chosen products (reproducible by seed) are seeded instead of category discovery.
Two live runs on the same 30 products (seed 20261127), spanning 18 top-level categories:
- deterministic `run_20261127_sitemap30`: 27 accepted, 3 partial; 209 of 224 observed SKUs; 30 fetches, 0 retries, 97 s.
- hybrid `run_20261127_sitemap30_hybrid` (source of `outputs/sitemap_sample/`): 29 accepted, 1 partial; 218 of 224;
  40 model calls, 137,772 + 26,843 tokens ≈ $0.027; 147 s.
The partial pages render fewer table rows than their JSON-LD (Filtek flowable 20/27, Kerr glide-path files 4/10,
Premier bur blocks 2/4). Live hybrid recovered 9 SKUs; Kerr kept 6 missing. An offline re-run of the extractor on
the 3 saved pages recovered all 15 (9 calls, ≈ $0.007). The sample also found two templates not seen before:
products without a `#pdp-grouped` table (personalized items) and tables showing "No options of this product are
available." Selector failures are now counted only when the table rendered rows but the SKU selector missed;
the other cases are render notes (`no_variant_table`, `table_not_rendered`, `no_options_listed`).

**README §7** now has the ten production items as subsections in the order the requirements list them, each
with how it is done and the code and tests that cover it. Two tests were added so every item has one: JSON log
fields with secret redaction, and a hybrid run without credentials completing in degraded mode. 72 tests pass.

## Rules for normal pages, LLM for redesigns (2026-11-27)

Goal agreed with Amy: rules handle normal pages; the LLM is insurance for redesigns and unseen layouts.

**JSON-LD merge (rules).** `parse_product` now takes the rendered table rows and then adds every JSON-LD
variant whose SKU the table did not render (a table row always wins, so "Non-purchasable" rows keep a null
price). The 15 SKUs the model had recovered on the sitemap sample were all in JSON-LD; rules now fill them.
Live re-runs on the same 30 sitemap products (seed 20261127): `run_20261127_sitemap30_merge` (deterministic)
30 accepted, 224/224 SKUs, 107 s; `run_20261127_sitemap30_merge_hybrid` (source of `outputs/sitemap_sample/`)
30 accepted, 224/224, 48 model calls, 152k + 35k tokens ≈ $0.033, 163 s. Every model call was attribute
splitting: 0 SKUs and 0 prices came from the model; SKU, price, pack and availability identical in both runs.

**Generic evidence path (Amy's option B).** Amy pointed out that parser, snapshot tools and EvidenceChecker
all read `build_sections()`, so a redesign that breaks the Safco selectors would leave the model with no rows
to cite and `find_gaps` with nothing to report. Changes, no new agent:
- `build_sections` adds a `page-rows` section only when the table and JSON-LD give no row: repeated sibling
  elements with the same tag and class signature (any class names) that each show a `$` price and an SKU-like
  code; one cell per leaf text plus a `price` cell = the row's first displayed price. Groups split across
  containers are joined; at least two rows are required.
- New gap `no_variants` (no variant parsed, no expected SKU, generic rows present) starts the model loop.
- Verification is not downgraded: SKU must equal a whole non-price cell of the cited row and contain a digit;
  price must equal that row's `price` cell. A generic-row record is `partial` (`generic_rows_uncovered`) unless
  every generic row got a verified variant.
- Prompt: one line for the `no_variants` gap.

**Tests.** `simulate_redesign` (in `evaluation.py`, labelled SYNTHETIC) removes JSON-LD and renames every class.
The agent-loop tests that used a removed table row (now filled by rules) run on the redesigned Surpass page
instead: rules find no variant; a valid patch is accepted with evidence on the cited generic row; SKU on the
wrong row, the row's second price, an invented SKU and a price cell used as SKU are rejected; a redesigned page
without prices builds no generic rows and never calls the model. 73 tests pass.

**2×2 experiment** (`catalog-agent experiment --run-id run_20261127_sitemap30_merge_hybrid`,
`outputs/redesign_experiment.json`), same 30 saved pages, reference = original JSON-LD (224 SKUs; 82 of them
rendered as table rows):

| | A original, rules | B original, rules + LLM | C redesign, rules | D redesign, rules + LLM |
|---|---|---|---|---|
| Accepted / partial | 30 / 0 | 30 / 0 | 0 / 30 | 16 / 14 |
| SKUs / correct prices / wrong prices | 224 / 224 / 0 | 224 / 224 / 0 | 0 / 0 / 0 | 56 / 56 / 0 |
| Model calls (pages) | 0 | 47 (30) | 0 | 35 (17) |
| List cost | $0 | $0.032 | $0 | $0.026 |

D recovered every rendered row on 16 of 17 multi-row pages; Filtek (20 rows) returned no usable patch in this
run (18 of 20 in the first run of the experiment, before a fix). Not recoverable by design: 142 SKUs that exist
only in JSON-LD and 6 single-variant pages. First run of the experiment found that single-row sub-groups (e.g.
one product in its own group container) were dropped from generic rows (5 pages lost one row each); groups with
one row are now joined to a matching signature elsewhere on the page.

**Docs.** README §9 "Why an LLM" with the 2×2, §10 sitemap table before/after the merge and selector
self-repair as design only, §12 limitation of the generic path; Architecture §4 extraction paragraph.
Offline evaluation re-run (hybrid run 4): held-out SKU recall and price accuracy 1.0, attribute recall 0.75.

## One-command demo and data-quality summary (2026-11-27)

Amy asked for three things in order: merge the redesign work into `main` (done, fast-forward), a one-command
reproducible run, and a data-quality report.

- `Makefile`: `make demo` (setup, live bounded crawl of both categories with the default config, export, run
  report, summary into `outputs/demo/`), `make demo-offline` (same pipeline on `tests/fixtures/live`, no network,
  no credentials, into `data/demo_offline/`), `make test` (smoke script). Docker was not added: the roadmap
  lists Docker packaging under P2.
- `run_report.json` gains a `summary` block (pages fetched, accepted/partial/quarantined, products and SKUs,
  model calls, tokens, list cost from the run's stored config, run time, duplicate SKU identity rate, repeated
  rows merged) and, per category, `issue_distribution` (severity:code) and `rejected_values`.
- New command `catalog-agent summary` renders the summary and data-quality tables (field completeness per
  category, missing-value reasons, issue codes, evidence rejections, duplicates) as Markdown. The run-level
  report test now checks the summary and the tables.
- Live `make demo` (`demo_20261127`, hybrid): 37 pages, 20 products all accepted, 87 SKUs, 27 model calls,
  69,246 + 19,730 tokens ≈ $0.017, 151 s; 0 duplicate identities, 0 rejected values. README §5 and §11 show it.

## Stronger redesign recovery: row mapping, single-variant pages, harder redesigns (2026-11-27)

Amy chose items 1, 2 and 6 of the recovery plan (the first redesign run recovered 56 of 82 rendered SKUs).

- **Row mapping (1).** Generic cells are keyed by their tag/class path inside the row (`k012402`), so the same
  column has the same key in every row. On a `no_variants` gap the first message holds 3 sample rows plus the
  row count; the model returns `row_mapping` (`sku_cell`, `name_cell`) and code applies it to all rows. Each
  produced entry goes through the same checks as a hand-written one.
- **Single-variant pages (2).** If no repeated row exists, the single smallest element that shows both a `$` price
  and an SKU-like code becomes one generic row (none or several such elements: no row). All 6 single-variant pages
  in the sample now get their row; the 7 pages with no rendered rows still get none.
- **Harder synthetic redesigns (6).** `simulate_redesign(html, kind)`: `classes` (as before), `tags` (div → section,
  span → em, ul → ol), `columns` (children of every repeated element reversed). A row's leaf limit went from 24 to
  48 because reversed rows put the SKU last.
- **Found on the way:** with columns reversed and no labels, the model mapped `Mfr #` as the SKU on 6 pages; those
  values are whole SKU-like cells of the right row, so the evidence gate accepted them (9–10 wrong SKUs). Fix: the
  header row's labels are aligned to the row's columns by code (a column repeated for mobile/desktop counts once)
  and become part of the keys (`Item # k012402`). The Validator also requires a generic-row SKU to match the
  adapter's SKU-like pattern, so a label or quantity can never be an SKU.
- Tests: row mapping applied to all rows in one call; wrong mappings (label column, price cell, other section)
  rejected; single-variant redesigned page recovered. 76 tests pass.

Experiment (same 30 pages, final run): rendered SKUs recovered 82/82 (classes), 62/82 (tags; the 20-row Filtek
page returned no usable mapping in this run, 20/20 when re-run alone), 82/82 (columns); 0 wrong prices and 0 wrong
SKUs in all three; ≈ $0.035 per redesign pass of 30 pages. README §9 rewritten with these numbers.

## Inline page data and stock on redesigned pages (2026-11-27)

Amy asked for the stock field on redesigned pages (item 3) and the inline-data evidence source (item 4).

- **Inline page data (rules).** `embedded_rows` decodes JSON inside inline scripts (also JSON serialized inside a
  string literal) and keeps objects with an `sku` string and a numeric price, found by shape, not by variable name;
  the container with the most such objects wins. Cells: sku, name, price (only if exact to the cent), currency
  (USD only if the object itself shows the amount with `$`), availability (label or code). `parse_product` uses it
  after the table and JSON-LD to fill SKUs they did not show. On 50 normal pages it found 290 SKUs, all equal to the
  JSON-LD SKUs with equal prices; the parent product object (no price) is ignored.
- **Stock on generic rows.** `row_mapping` gains `stock_cell`; mapped rows fall back to the same labelled column
  when a row's markup differs (status-dependent styling gave "In stock" and "Backorder" different keys). The
  Validator accepts a generic-row stock value only if it is a whole cell of the row that is a known status or a
  short text in a stock/availability column ("Special order").
- **Redesign kinds.** New `data` kind keeps the inline data; `classes`, `tags` and `columns` now also remove it.
- Tests: rules read inline data after a `data` redesign with no model call; mapping with stock; a non-status stock
  column rejected. 78 tests pass.

Final experiment run (`outputs/redesign_experiment.json`): rules alone get 224/224 on the original pages and 203/224
after the `data` redesign; with all structured data removed rules get 0 and rules + LLM recover 82, 81 and 82 of the
82 rendered SKUs (classes, tags, columns), each with price, stock and name; 0 wrong prices and 0 wrong SKUs in every
group. README §9 rewritten with this run.

## Full two-category sample, query examples, LLM-use table (2026-11-27)

Checked against the requirements document again; three gaps were closed before submission.

- **Sample = everything reachable.** `configs/safco_full_sample.yaml` (default config with 60 products per category,
  250 pages, 300 model calls, P1 model features on). Live run `run_20261127_full`: 106 pages, 0 retries, 435 s,
  `COMPLETED`; 89 products (39 gloves, 50 sutures & surgical, all 14 subcategories), all accepted; 556 SKUs. Checks:
  every page's SKU set equals its JSON-LD SKU set (89/89); all 556 prices equal the JSON-LD offer price; a
  deterministic re-parse of the same snapshots gives 0 differences in SKU, price, pack and availability. 386 SKUs
  from the rendered table, 170 from JSON-LD (16 pages whose table never rendered). 114 model calls, all attribute
  splitting, ≈ $0.088; 0 classifier fallback, 0 repair suggestion. One SKU (`4180087`) is listed by the site on two
  suture products. `outputs/` now comes from this run; README §1, §6, §9, §10, §11, §12 updated.
- **Queryable.** `outputs/catalog_sample.sql` (SQLite dump of the run's products and variants; no database file is
  committed) and three SQL examples in README §6, checked against the dump.
- **LLM uses.** README §4 maps the four suggested LLM uses to what was built, their defaults and how to see them,
  and why navigation never uses a model.
- `summary` prints small rates with one decimal (the duplicate rate showed 0% for 1 of 555).
