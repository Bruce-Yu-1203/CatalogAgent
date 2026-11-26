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
