# Live fixtures (real snapshots)

Rendered DOM captured with Playwright (Chromium, images/fonts/analytics blocked) from
https://www.safcodental.com on 2026-11-26 during the M0 site spike. They are real pages, gzipped,
and are used only for offline tests; they are not exported data.

| File | URL |
|---|---|
| `catalog.html.gz` | /catalog (category roots) |
| `listing_gloves.html.gz` | /catalog/gloves |
| `listing_sutures.html.gz` | /catalog/sutures-surgical-products |
| `listing_sutures_sutures.html.gz` | /catalog/sutures-surgical-products/sutures |
| `product_surpass_gloves.html.gz` | /product/safco-nitrilex-surpass-trade |
| `product_surestitch_sutures.html.gz` | /product/safco-surestitch-trade-sutures |
| `product_trimmer_incomplete_render.html.gz` | /product/soft-tissue-trimmer (variant table not yet rendered; JSON-LD only) |
| `product_orasoothe_tooltips.html.gz` | /product/orasoothe-reg-sockit-gel (description cells with SDS links and "Details" tooltips; captured in run `run_20261126_live`) |
| `products_sitemap_excerpt.xml` | /products.xml filtered to the product URLs that have fixtures here (real entries, captured 2026-11-27; the full file lists 3,980 products) |
