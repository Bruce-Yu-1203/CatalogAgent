# CatalogAgent — Design Documents V3

**Production-minded POC.**

V3 supersedes V2. These documents define the design; the repository README reports what was built and measured, following [Roadmap §7](docs/04-roadmap.md#7-final-readme-outline).

- [Product Requirements Document](docs/01-prd.md) — scope, requirements, field-level output schema, assumptions
- [Technical Stack and Engineering Decisions](docs/02-technical-stack.md) — stack, ADRs, agent roster, configuration
- [Overall System Architecture](docs/03-overall-architecture.md) — workflow, reliability, scaling, deployment, data-quality monitoring
- [Implementation Roadmap](docs/04-roadmap.md) — milestones, tests, evaluation, README outline

## Changes in V3

- **Agent roster:** five agents (Navigator, Page Classifier, Extractor, Validator & Deduplicator, Recovery) behind one typed contract under a deterministic orchestrator. The model is used for extraction (P0), unknown-page classification and selector-repair suggestions (both P1, report-only for repairs).
- **Schema:** field-level product and variant tables, missing-reason enum, JSON example and CSV column list (PRD §5).
- **Assumptions:** consolidated in PRD §9 with evidence and verification points; the other documents state decisions.
- **Production:** sizing model for a full-site crawl, a staged deployment path, and production data-quality metrics with alert thresholds (Architecture §8–10).
- **Scope cuts:** evidence limited to SKU and price; `all` scope documented in the README instead of verified with a dedicated fixture (moved to P2).
- **Consistency fixes:** stable `product_id`/`variant_id` with run-keyed storage (no cross-run collisions); one schema for JSON, CSV and evidence (PRD §5); POC run metrics separated from production monitoring (Architecture §10.1 vs §10.2); P1 LLM features off by default; explicit resume and budget semantics (Architecture §6.1); P0 tests consolidated into a five-file suite plus smoke script (Roadmap §3.1).
- **Fixes:** priority table ordering, initial site observation (client-side rendering, prices possibly behind sign-in), LangGraph rationale and exit plan (ADR-09), README outline.

## Scope decisions

- One active Python process per SQLite database, with an OS-backed exclusive lock and restart recovery; no leases, heartbeats or distributed workers in the POC.
- Playwright as the single fetch path; HTTPX only if site inspection finds a permitted lighter endpoint.
- Four core tables, run-scoped records; the Validator never uses a model.
- Evidence references cover SKU and price only; other model-supplied fields are schema-checked and listed in `llm_fields`.
- `selected`/`all` root scope through the same discovery loop; `all` is a config switch with a root-selection unit test, explained in the README, with no dedicated multi-root fixture and no live full-site crawl.
- Genuine output from both required categories and a small baseline/hybrid evaluation where possible.
- Distributed workers, automatic selector rollout, incremental crawling and centralized observability stay design-only.

## Start here

Read the PRD and roadmap first. Build the real vertical slice before agent recovery or evaluation, freeze scope at P0/P1, and report actual results. Mermaid diagrams render in GitHub Markdown.
