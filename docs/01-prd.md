# CatalogAgent — Product Requirements Document

**Version:** 3.0 — Production-minded POC  
**Scope:** Proof of concept · 24-hour implementation timebox  
**Source:** *Take-Home Test: Build an AI Agent for Product Scraping and Structured Catalog Extraction (POC)*, pp. 1–6.  
**Assumptions:** All assumptions and items to verify are collected in [§9](#9-assumptions-and-open-questions). The rest of this document states decisions.

## 1. Executive summary

CatalogAgent is an agent-based catalog ingestion system that discovers, crawls, extracts, normalizes and exports public product information from Safco Dental Supply. Five specialized agents — Navigator, Page Classifier, Extractor, Validator & Deduplicator, and Recovery — run under a deterministic orchestrator. Each agent is rule-first; LLM reasoning is used where page semantics are uncertain: product extraction for irregular layouts, classification of unrecognized pages and selector-repair suggestions. A persistent single-process crawl frontier, evidence-based quality checks and idempotent writes make the POC reliable and give it a direct path to production.

The goal is a reproducible working slice across both target categories, including product variants, with measurable evidence of where AI adds value.

## 2. Problem and users

Supplier product data is spread across categories, listings, detail pages and variant tables. Manual collection is slow and hard to repeat; fixed parsers break on inconsistent layouts. Downstream software needs the data structured, not as page text.

| User (persona, A-01) | Need |
|---|---|
| Catalog operations analyst | Structured product and SKU data with source references. |
| Automation engineer | Run a bounded crawl, resume interrupted work and inspect failures. |
| Data consumer | Query or export a normalized catalog without parsing HTML. |
| Engineering reviewer | Reproduce the demo and evaluate correctness, AI value and production trade-offs. |

## 3. Objectives and scope

### 3.1 Source scope

- Dental Exam Gloves: https://www.safcodental.com/catalog/gloves
- Sutures & Surgical Products: https://www.safcodental.com/catalog/sutures-surgical-products

The system discovers categories from site navigation (or sitemap, where permitted), selects the two configured roots and follows their in-scope descendants and product links. It supports the site's pagination or dynamic-loading mechanism. A configurable run budget limits pages and products; the sample need not exhaust the site. `selected` and `all` scope settings use the same recursive discovery algorithm and differ only in which discovered roots are seeded. `all` is implemented as a configuration switch and explained in the README; it is not exercised by a full live crawl or a dedicated multi-root fixture.

### 3.2 Priorities

| Priority | Requirement |
|---|---|
| P0 | Real end-to-end crawl of both categories: discovery, listing traversal, product and variant extraction, normalized storage and export. |
| P0 | Separate Navigator, Classifier, Extractor and Validator agents behind a common contract; a working LLM-assisted extraction path. |
| P0 | Rate limiting, bounded retries, persistent task state, idempotency, logging, configuration and safe secret handling. |
| P0 | Focused tests for recursive traversal, crash recovery, deduplication and runtime budget enforcement. |
| P0 | Runnable repository, setup and usage instructions, genuine sample output, field-level schema, known limitations and production roadmap. |
| P1 | LLM fallback for pages the Classifier rules cannot identify. |
| P1 | Recovery Agent selector-repair suggestions (report only, never auto-applied). |
| P1 | Evidence references for fields beyond SKU and price (pack, availability, attributes); small replayable baseline-versus-hybrid comparison; quality metrics. |
| P1 | Additional tests for invalid model output and recovery quality. |
| P2 | Design only: multi-root fixture graph for `all`-scope verification, distributed workers, leases, large-scale load testing, automatic selector rollout, incremental crawling. Docker optional after acceptance; no dashboard. |

### 3.3 Non-goals

Full-site completion in 24 hours; authenticated or restricted pricing; CAPTCHA bypass or access-control circumvention; cross-supplier product matching; automatic permanent selector changes; distributed infrastructure, RAG, fine-tuning, an LLM supervisor, dynamic agent spawning, multi-process task leases/heartbeats, history tables, large-scale stress testing and a dashboard.

## 4. Functional requirements

| ID | Pri. | Requirement | Acceptance criterion |
|---|---|---|---|
| FR-01 | P0 | Category discovery and scope | Navigator discovers category links and identifies both configured roots; URLs are resolved, normalized, scoped and deduplicated; source and hierarchy are recorded. |
| FR-02 | P0 | Traverse listings | Navigator discovers product links and follows the site's pagination or dynamic loading; it stops at an end condition or an explicit budget and reports which. |
| FR-03 | P0 | Extract product details | Both categories produce real product records with the §5 fields whenever visible, and every observed sellable variant rather than only the first SKU. |
| FR-04 | P0 | AI-assisted extraction | When deterministic extraction leaves a recoverable gap, the Extractor Agent's bounded LLM loop uses read-only snapshot tools and returns typed candidates; SKU and price candidates carry evidence references. At least one reproducible fallback case is demonstrated. |
| FR-05 | P0 | Normalize and validate | Validator checks schema, identity, source and completeness. SKU and price are verified against their evidence row; other model-supplied fields pass schema checks and are listed in `llm_fields`. Unsupported values are rejected or kept null with a reason; unresolved critical records are quarantined. |
| FR-06 | P0 | Store and export | SQLite supports querying products and variants. JSON and CSV exports follow the §5 schema, preserve SKU identity and category paths, and contain genuine data from both categories. |
| FR-07 | P0 | Resume and retry | An interrupted run resumes without losing queued work or duplicating accepted records, after acquiring exclusive process ownership of the database. Page, product and model budgets are cumulative per run; a `BUDGET_EXHAUSTED` run continues only when resumed with explicit budget extensions (Architecture §6.1). Transient failures retry with bounded backoff; access denial is never bypassed. |
| FR-08 | P0 | Report outcomes | Each run records discovery, completion, partial/failure outcomes, retries, variant counts, fallback usage and the POC data-quality metrics in Architecture §10.1. |
| FR-09 | P0 | Configuration | Roots, limits, concurrency, retry policy and model settings are externally configurable. Credentials come from environment variables and are never committed. |
| FR-10 | P0 | `all` scope setting | `scope: all` seeds every permitted discovered root through the same discovery loop. A unit test on the saved navigation snapshot asserts which roots each setting seeds. The README explains how `all` works, which budgets bound it and that it was not run against the live site. |
| FR-11 | P1 | Classifier LLM fallback | A page the rules classify as `unknown` gets one bounded model call that returns a type from a closed enum with evidence section IDs; the type is accepted only if the required DOM signals for it are present. Disabled by default until its P1 test passes. |
| FR-12 | P1 | Selector-repair suggestions | After repeated parse failures on the same field and page template, the Recovery Agent proposes replacement selectors, tests each on the frozen failing snapshots, and writes ranked suggestions to `outputs/repair_suggestions.json`. No parser change is applied automatically. Disabled by default until its P1 test passes. |
| FR-13 | P1 | Reproducible evaluation | Deterministic-only and hybrid modes run against the same frozen, manually checked fixtures; the report includes failures, costs and limitations. |

## 5. Catalog data contract

A **product** is a logical catalog entry; a **variant** is a sellable item (SKU) belonging to it. One page is not assumed to equal one SKU. Source identifiers are strings to preserve leading zeros and punctuation. This section is the single definition of the export schema; JSON, CSV and evidence use the same field names, enums and path convention.

**Path convention.** Wherever a field is referenced (`missing[].field`, `llm_fields[]`, `evidence[].field`), the path is relative to the object that holds the array: a top-level field name (`price`, `brand`) or `attributes.<key>` (`attributes.Material`). Variant-level references live in the variant's own arrays, never in the product's.

### 5.1 Product fields

| Field | Type | Nullable | Description | Assignment field |
|---|---|---|---|---|
| `product_id` | string | no | Stable catalog ID: hash of `source` + `product_key`. Identical across runs; stored rows are keyed by `(run_id, product_id)`. | — |
| `run_id` | string | no | Run that observed this record. | — |
| `source` | string | no | Source adapter, e.g. `safco`. | — |
| `product_key` | string | no | Source product ID if exposed, else canonical URL path. | — |
| `url` | string (URL) | no | Canonical product page URL. | product URL |
| `name` | string | no | Product name as displayed. | product name |
| `brand` | string | yes | Brand as displayed. | brand |
| `manufacturer` | string | yes | Manufacturer, only when distinguishable from brand. | manufacturer |
| `description` | string | yes | Plain-text description. | description |
| `category_paths` | array of array of string | no | Every category path the product was discovered under, root first. | category hierarchy |
| `image_urls` | array of string | no (may be empty) | Absolute image URLs, primary first. | image URL(s) |
| `attributes` | object (string → string) | no (may be empty) | Product-level specifications. | specifications / attributes |
| `alternatives` | array of `{name, url, sku}` | no (may be empty) | Alternative/related products the page lists; `sku` nullable. Not crawled automatically. | alternative products |
| `variant_count` | integer | no | Number of exported variants. | — |
| `record_status` | enum | no | `accepted` or `partial`. Quarantined records are not exported. | — |
| `extraction_method` | enum | no | `deterministic`, or `hybrid` when the model supplied at least one accepted value in the product or its variants. | — |
| `llm_fields` | array of path | no (may be empty) | Product-level fields whose accepted value came from the model. These passed schema checks; only SKU and price are evidence-verified. | — |
| `missing` | array of `{field, reason}` | no (may be empty) | Product-level fields left null and why (§5.4). | — |
| `observed_at` | string (ISO 8601 UTC) | no | Snapshot time. | — |

### 5.2 Variant fields

| Field | Type | Nullable | Description | Assignment field |
|---|---|---|---|---|
| `variant_id` | string | no | Stable ID: hash of `product_id` + `sku`. Identical across runs; stored rows are keyed by `(run_id, variant_id)`. | — |
| `product_id` | string | no | Parent product. Omitted inside nested JSON variants (implied by the parent). | — |
| `sku` | string | no | Source item number / product code, verbatim. Never generated. | SKU / item number |
| `variant_name` | string | yes | Variant label as displayed (e.g. "Medium"). | — |
| `attributes` | object (string → string) | no (may be empty) | Variant attributes such as size, color, material, needle type. | specifications |
| `pack_label` | string | yes | Raw pack text, e.g. `100/Box`. | unit / pack size |
| `pack_quantity` | integer | yes | Parsed count from `pack_label`. | unit / pack size |
| `pack_unit` | string | yes | Parsed unit, e.g. `box`, `case`, `each`. | unit / pack size |
| `price` | string (decimal) | yes | Public price, decimal string to avoid float error. | price |
| `currency` | string (ISO 4217) | yes | Set if and only if `price` is set. | price |
| `price_visibility` | enum | no | `public`, `login_required` or `not_shown`. | price |
| `availability` | enum | no | `in_stock`, `out_of_stock`, `backorder`, `limited` or `unknown`. | availability |
| `availability_raw` | string | yes | Stock text as displayed. | availability |
| `llm_fields` | array of path | no (may be empty) | Variant-level fields whose accepted value came from the model. | — |
| `evidence` | array of evidence refs (§5.3) | no | Exactly one ref for `sku`, plus one for `price` when `price` is set. | — |
| `missing` | array of `{field, reason}` | no (may be empty) | Variant-level fields left null and why. | — |

Variants inherit `run_id` and `observed_at` from their product.

### 5.3 Evidence reference

The same object is stored in SQLite, nested in JSON and JSON-encoded in the CSV `evidence` column. Only verified references are exported; a value whose evidence fails verification is rejected, so there is no status field.

| Field | Type | Nullable | Description |
|---|---|---|---|
| `field` | enum | no | `sku` or `price` in the POC (P1 adds more paths). |
| `snapshot_id` | string | no | Immutable snapshot the value was read from. |
| `section_id` | string | no | Section of that snapshot, as returned by the snapshot tools. |
| `row_id` | string | yes | Variant row, when the value comes from a variant table or list. |
| `raw_value` | string | no | Text exactly as found in the section/row. |
| `normalized_value` | string | no | Value as exported (equal to `raw_value` for SKUs). |
| `method` | enum | no | `deterministic` (parser) or `llm` (model-proposed, then verified). |

### 5.4 Missing-data reasons

| Reason | Meaning |
|---|---|
| `not_visible` | The page does not display the field. |
| `login_required` | The page indicates the value requires sign-in. |
| `not_applicable` | The field does not apply to this product. |
| `parse_failed` | The value is displayed but could not be extracted reliably. |
| `not_observed` | The section containing the value was not loaded or captured. |

Missing stock is `unknown`, never `out_of_stock`. Only public prices are captured. Raw values are kept beside normalized values whenever conversion is ambiguous. Brand and manufacturer are not conflated, and listed alternatives are not treated as verified substitutes.

### 5.5 JSON export (`outputs/products.json`)

A JSON array of products with nested `variants`. Values below are invented to show the shape; real exports contain only scraped data.

```json
{
  "product_id": "p_3f9a1c",
  "run_id": "run_20261127_0900",
  "source": "safco",
  "product_key": "/product/example-nitrile-exam-gloves",
  "url": "https://www.safcodental.com/product/example-nitrile-exam-gloves",
  "name": "Example Nitrile Exam Gloves",
  "brand": "ExampleBrand",
  "manufacturer": null,
  "description": "Powder-free nitrile examination gloves.",
  "category_paths": [["Dental Supplies", "Dental Exam Gloves"]],
  "image_urls": ["https://www.safcodental.com/media/example.jpg"],
  "attributes": {"Material": "Nitrile", "Powder": "Powder-free"},
  "alternatives": [{"name": "Example Latex Gloves", "url": "https://www.safcodental.com/product/example-latex-gloves", "sku": null}],
  "variant_count": 1,
  "record_status": "accepted",
  "extraction_method": "hybrid",
  "llm_fields": ["attributes.Powder"],
  "missing": [{"field": "manufacturer", "reason": "not_visible"}],
  "observed_at": "2026-11-27T14:05:12Z",
  "variants": [
    {
      "variant_id": "v_81d2e0",
      "sku": "EX-1001-S",
      "variant_name": "Small",
      "attributes": {"Size": "Small"},
      "pack_label": "100/Box",
      "pack_quantity": 100,
      "pack_unit": "box",
      "price": "12.49",
      "currency": "USD",
      "price_visibility": "public",
      "availability": "unknown",
      "availability_raw": null,
      "llm_fields": [],
      "evidence": [
        {"field": "sku", "snapshot_id": "s_77a1", "section_id": "variant-table", "row_id": "row-1", "raw_value": "EX-1001-S", "normalized_value": "EX-1001-S", "method": "deterministic"},
        {"field": "price", "snapshot_id": "s_77a1", "section_id": "variant-table", "row_id": "row-1", "raw_value": "$12.49", "normalized_value": "12.49", "method": "deterministic"}
      ],
      "missing": [{"field": "availability_raw", "reason": "not_visible"}]
    }
  ]
}
```

### 5.6 CSV export (`outputs/variants.csv`)

One row per variant, with product fields repeated. A product with zero exported variants gets one row with empty variant columns. Every column maps to a §5.1/§5.2 field:

- Column names equal field names, except where a product and variant field share a name: those get a `product_` or `variant_` prefix (`product_attributes`, `variant_attributes`, `product_llm_fields`, `variant_llm_fields`, `product_missing`, `variant_missing`), and `url`/`name` become `product_url`/`product_name`.
- Encoding: arrays of strings joined with ` | `; each category path joined with ` > `, paths joined with ` | `; objects and arrays of objects (`attributes`, `alternatives`, `missing`, `evidence`) JSON-encoded; nulls as empty cells. The file is UTF-8 with a header row and opens directly in Excel.

Columns, in order:

`run_id, product_id, product_key, product_url, product_name, brand, manufacturer, description, category_paths, image_urls, product_attributes, alternatives, variant_count, record_status, extraction_method, product_llm_fields, product_missing, observed_at, variant_id, sku, variant_name, variant_attributes, pack_label, pack_quantity, pack_unit, price, currency, price_visibility, availability, availability_raw, variant_llm_fields, evidence, variant_missing`

`test_extraction.py` round-trips one product through both exports and asserts identical values (Roadmap §3.1).

### 5.7 Identity policy

Product identity is `source` + `product_key`; variant identity is `product_id` + `sku`, valid only where the site uses item numbers as unique sellable codes (A-05). Both IDs are deterministic hashes, so the same product has the same `product_id` in every run; storage and exports separate runs through `run_id`, never by changing the ID. A missing or ambiguous SKU is never replaced with a generated code: the record is marked partial or quarantined instead of merging unrelated variants.

## 6. Quality and operational requirements

- **Correctness:** Product identity and source URL are always present; SKU and price are verified against the correct variant row. An accepted record does not imply every optional field is populated.
- **Reliability:** At-least-once task execution with atomic result commits and idempotent effects. One active process per database, enforced by an OS-backed exclusive lock; interrupted tasks are recovered only after acquiring it. No leases or heartbeats in the POC.
- **Responsible crawling:** Respect scope, site terms, robots directives and server throttling. Validate redirects; navigation is never LLM-controlled. Stop on access restrictions.
- **Safety:** Scraped content is untrusted data, not instructions. LLM tools are read-only snapshot inspection plus typed output; no shell, database or network access.
- **Observability:** Structured, correlated events carry run, task, agent, page type, extraction method, outcome, duration and error reason. Credentials and full prompts are not logged by default.
- **Maintainability:** Site-specific parsing sits behind an adapter; typed contracts, tests and documented configuration.
- **Cost:** The model is called only for a recoverable gap, an unknown page or a repeated failure pattern, under per-task and per-run budgets.

## 7. Success measurement

Internal acceptance gates for the POC (A-09):

| Measure | Target |
|---|---|
| Category coverage | Genuine accepted sample from both categories; 10 products per category. |
| Variant coverage | SKU sets checked on a manually inspected sample; report recall and precision. |
| Critical field correctness | Zero unsupported SKU or public price in accepted evaluation records; report the denominator and any failures. |
| Duplicate prevention | Replaying identical tasks does not change unique product or SKU counts. |
| Recovery | Interruption and resume completes the remaining work or marks it failed. |
| AI value | Baseline vs hybrid correctness/coverage, latency and model cost on identical fixtures. |
| Reproducibility | A reviewer installs, runs, replays fixtures and exports data with the documented commands. |

Field accuracy and completeness are reported separately, with exact or documented normalized comparisons, missed and extra variants, and the number of pages and fields assessed. A small held-out set is used where practical.

The run report shows discovered and accepted counts, pending/failed tasks and the stopping reason. A budget-limited run is reported as incomplete. Full-scope coverage is not measured in the POC; the README states this explicitly.

## 8. Release acceptance and deliverables

The submission is ready when P0 requirements are implemented and exercised, the repository has setup and execution instructions, outputs contain genuine data from both categories and limitations are stated. Deliverables: sample JSON/CSV (optional SQLite dump), run report and a README structured per [Roadmap §7](04-roadmap.md#7-final-readme-outline). A short demo or demo script is included if time permits.

## 9. Assumptions and open questions

These are the only unverified premises in the design. Each is checked at the listed point and the docs are updated with the outcome.

| ID | Assumption | Basis / current evidence | Verified in | If wrong |
|---|---|---|---|---|
| A-01 | The §2 users are representative. | Proposed personas; not confirmed Frontier Dental workflows. | Review | Adjust exports, not architecture. |
| A-02 | Public catalog pages may be crawled at low rate. | Public catalog. | M0: robots.txt and terms | Stop live crawling; document; use only clearly labeled permitted fixtures. |
| A-03 | Listing content renders client-side, so a browser is required. | A non-browser fetch of the gloves page returned a "JavaScript seems to be disabled" notice and no product cards. | M0: inspect network calls | If a permitted JSON or server-rendered endpoint exists, add HTTPX for it. |
| A-04 | Prices may require sign-in. | The same fetch showed a Sign in link and no prices. | M0 | `price_visibility` and the `login_required` reason already cover both cases. |
| A-05 | Item numbers uniquely identify sellable variants site-wide. | Typical for distributor catalogs. | M0: compare item numbers across pages | Scope SKU identity by product; quarantine ambiguous rows. |
| A-06 | Pagination is URL-based or load-more. | Not observed yet. | M0 | Navigator supports both; implement the one observed. |
| A-07 | The chosen model supports tool calling and structured output reliably. | Provider documentation. | M3 smoke test | Pin a different model; deterministic mode is unaffected. |
| A-08 | The dependency set in Tech Stack §2 installs together. | Not installed yet. | M1 | Pin working versions in `uv.lock`. |
| A-09 | §7 targets are internal engineering gates. | Not company scoring criteria. | — | — |
| A-10 | Sizing inputs in Architecture §8 (page counts, retry and fallback rates). | Placeholders. | M0/M4 measurements | Recompute the estimate with measured values. |
| A-11 | Fixture replay results reflect replay behavior only. | By construction. | — | Live and replay results are reported separately. |

### 9.1 Verification outcomes

Recorded as each assumption was checked. Details and evidence: [implementation log](implementation-log.md).

| ID | Outcome | Checked | Design consequence |
|---|---|---|---|
| A-02 | **Holds, with limits.** robots.txt (`User-agent: *`) allows `/catalog/*` and `/product/*` but disallows `/*?page=`, `/*?p=`, `/catalog/product/view/id/*`, `/customer/`, `/checkout/` and others; it advertises `sitemap.xml`. The terms of sale contain no clause on automated access or scraping. | M0, 2026-11-26 | Crawl at the configured low rate; the fetcher enforces robots.txt (with `*`/`$` wildcards) on every URL, including redirects. |
| A-03 | **Confirmed.** A plain HTTP fetch of a listing returns navigation but no product cards; products are rendered client-side by Algolia InstantSearch (12 hits per page). Product pages embed schema.org `ProductGroup` JSON-LD and render the variant table client-side. | M0 | Playwright stays the only fetch path. No HTTPX path: the only listing API is Algolia on a third-party host, outside `allowed_hosts`. |
| A-04 | **Wrong: prices are public.** Variant prices (with quantity tiers) are shown without sign-in on every product page sampled. | M0 | No change: `price_visibility = public`; `login_required` detection is kept for pages that show a sign-in notice. |
| A-05 | **Holds on the sample.** Each variant row has a distinct `Item #` (e.g. `1027990`), equal to the JSON-LD `sku` and the `Mfr #` column; no SKU appeared under two products in the live run `run_20261126_live` (duplicate identity count 0, 93 SKUs). | M0, M5 run | Variant identity stays `product_id` + `sku`. |
| A-06 | **Partly wrong.** Pagination is URL-based (`?page=N`), but robots.txt disallows those URLs. There is no load-more. | M0 | The Navigator detects the next page, checks it against robots.txt and records `next_page_robots_disallowed` as the stop reason instead of fetching it. Coverage beyond page 1 comes from subcategory listings (each also page 1 only). |
| A-07 | **Not verified.** No model credentials were available in the implementation environment. | M3 | The LLM path is exercised only with scripted fake models in tests; hybrid live runs report `MODEL_UNAVAILABLE` degraded recovery. |
| A-08 | **Holds.** The Tech Stack §2 set installs together on Python 3.12 with uv; versions pinned in `uv.lock`. | M1 | — |
| A-10 | **Partly measured.** Live run: 48 fetch attempts in 206 s (≈4.3 s each, 2 workers, 2 s pacing); 10 render retries over 31 product-page attempts; 12 products per listing page. | M0/M5 | README §10 recomputes the sizing estimate with these inputs. |

If live access is restricted, the blocker is documented and any fixture demonstration is labeled as such; fixtures alone do not meet the live-data requirement.
