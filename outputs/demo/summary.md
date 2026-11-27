Run `demo_20261127` (hybrid, BUDGET_EXHAUSTED, MAX_PRODUCTS_PER_CATEGORY)

| Pages fetched | Product pages | Accepted / partial / quarantined | Products / SKUs exported | Model calls | Tokens in / out | List cost | Time |
|---|---|---|---|---|---|---|---|
| 37 | 20 | 20 / 0 / 0 | 20 / 87 | 27 | 69,246 / 19,730 | $0.017 | 151.2 s |

| Field completeness | gloves | sutures-surgical-products |
|---|---|---|
| product.name | 100% | 100% |
| product.brand | 100% | 100% |
| product.manufacturer | 0% | 0% |
| product.description | 100% | 100% |
| product.category_paths | 100% | 100% |
| product.image_urls | 100% | 100% |
| product.attributes | 70% | 10% |
| product.alternatives | 100% | 100% |
| variant.sku | 100% | 100% |
| variant.variant_name | 100% | 100% |
| variant.attributes | 100% | 100% |
| variant.pack_label | 94% | 75% |
| variant.pack_quantity | 94% | 75% |
| variant.pack_unit | 94% | 75% |
| variant.price | 100% | 100% |
| variant.currency | 100% | 100% |
| variant.availability_raw | 100% | 100% |

| Missing-value and rejection reasons | gloves | sutures-surgical-products |
|---|---|---|
| missing manufacturer: not_applicable | 10 | 10 |
| missing variants.pack_label: not_visible | 3 | 9 |
| rejected values (evidence checks) | 0 | 0 |
| evidence rejection rate (model patches) | 0% | 0% |

Duplicates: SKU identity duplicate rate 0% (0 SKUs under more than one product); 0 repeated rows merged with identical values.
