Run `run_20261127_full` (hybrid, COMPLETED, FRONTIER_DRAINED)

| Pages fetched | Product pages | Accepted / partial / quarantined | Products / SKUs exported | Model calls | Tokens in / out | List cost | Time |
|---|---|---|---|---|---|---|---|
| 106 | 89 | 89 / 0 / 0 | 89 / 556 | 114 | 400,364 / 95,445 | $0.088 | 434.7 s |

| Field completeness | gloves | sutures-surgical-products |
|---|---|---|
| product.name | 100% | 100% |
| product.brand | 100% | 100% |
| product.manufacturer | 0% | 0% |
| product.description | 100% | 100% |
| product.category_paths | 100% | 100% |
| product.image_urls | 100% | 100% |
| product.attributes | 62% | 2% |
| product.alternatives | 100% | 100% |
| variant.sku | 100% | 100% |
| variant.variant_name | 100% | 100% |
| variant.attributes | 100% | 74% |
| variant.pack_label | 98% | 81% |
| variant.pack_quantity | 98% | 81% |
| variant.pack_unit | 98% | 81% |
| variant.price | 100% | 100% |
| variant.currency | 100% | 100% |
| variant.availability_raw | 100% | 100% |

| Missing-value and rejection reasons | gloves | sutures-surgical-products |
|---|---|---|
| missing manufacturer: not_applicable | 39 | 50 |
| missing variants.pack_label: not_visible | 3 | 69 |
| missing variants.pack_quantity: parse_failed | 0 | 1 |
| rejected values (evidence checks) | 0 | 0 |
| evidence rejection rate (model patches) | 0% | 2% |

Duplicates: SKU identity duplicate rate 0.2% (1 SKUs under more than one product); 0 repeated rows merged with identical values.
