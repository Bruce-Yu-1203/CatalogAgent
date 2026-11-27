# Evaluation fixtures (FR-13)

Frozen product pages for the deterministic-vs-hybrid comparison (`catalog-agent evaluate`).

- 20 real pages captured in live run `run_20261127_hybrid2` (2026-11-27) and the real SureStitch page from
  the M0 spike (2026-11-26).
- 1 **SYNTHETIC** page: `SYNTHETIC-surpass-row-removed.html.gz`, the real Surpass page with one variant row
  removed from the rendered table (its SKU is still in the page's JSON-LD). It is a controlled irregular
  case and is reported separately; it never appears in `outputs/` catalog data. Since the JSON-LD merge
  (2026-11-27) the deterministic parser fills the removed SKU from JSON-LD, so this page no longer needs the model.

`labels.json`: SKU sets and prices come from each page's JSON-LD offers (independent of the rendered-table
parser, but from the same page). Attribute values were labelled by Claude from each variant's visible
label using per-page rules, not reviewed by a human; attribute keys are not scored. `split` marks pages
inspected during development (`dev`) versus `held_out`.
