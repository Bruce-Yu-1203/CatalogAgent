# web-crawler Constitution

**Version:** 1.1.0 · **Ratified:** 2026-11-26 · **Last amended:** 2026-11-27

This document holds the non-negotiable rules for building web-crawler. It applies to every contributor, human or AI coding agent, and to every change in this repository.

**Precedence:** this constitution → `docs/` (PRD, Technical Stack, Architecture, Roadmap) → code. If code contradicts the docs, the code is wrong or the docs must be amended in the same change. If the docs contradict this constitution, the constitution wins until it is formally amended (see Governance).

Keywords **MUST**, **MUST NOT**, **SHOULD** and **MAY** are used in their RFC 2119 sense.

---

## Core principles

### I. Genuine data, honest reporting

- Every exported record MUST come from a real crawl or replay of a real snapshot. Synthetic fixtures MUST be labeled as synthetic and MUST NOT appear in `outputs/`.
- A SKU, price or other identifier MUST NOT be generated, guessed or inferred. If it cannot be extracted reliably, it stays null with a reason from PRD §5.4.
- The README and reports MUST state what was actually run and measured. Planned tests, targets and estimates are never presented as results. A negative finding (e.g. the LLM did not help) is reported as is.

*Rationale: the deliverable is judged on whether it really works; one fabricated value undermines all the others.*

### II. Vertical slice first, scope frozen

- Work follows the Roadmap milestone order: a genuine end-to-end slice across both categories comes before durability, agents, evaluation or polish.
- P0 is the scope. P1 starts only after all P0 gates pass. P2 is documentation only and MUST NOT be implemented.
- When scope must be cut, cut in this order: P1 items, test sample size, secondary parser strategies. Never cut: genuine two-category data, variant correctness, basic reliability, stated limitations.

### III. Deterministic first; the model only where it earns its place

- The Orchestrator, Navigator and Validator MUST NOT call a model. Navigation stays rule-bound so the model can never widen crawl scope; validation stays independent of the model that produced the data.
- The model MAY be used only in: the Extractor's bounded recovery loop (P0), the Classifier fallback for `unknown` pages (P1) and Recovery's selector-repair suggestions (P1).
- P1 model features MUST ship disabled in the default configuration and MAY be enabled only after their tests pass.
- The model is invoked only for a recoverable gap, never because a field is simply not displayed.
- All model budgets (calls, tools, tokens, seconds) MUST be enforced by runtime code outside the model.
- Deterministic mode MUST run without any model credentials.

### IV. Evidence over trust

- Every exported `sku` and `price` MUST carry a verified evidence reference (PRD §5.3) resolved against the original snapshot, not against text supplied by the model.
- A price MUST belong to the evidence row of its SKU; substring matches alone do not establish this.
- Any other field whose value came from the model MUST pass schema checks and MUST be listed in `llm_fields`.
- A known deterministic value MUST NOT be overwritten by an unsupported model value.
- Unresolved critical identity or row ambiguity is quarantined, never silently accepted.

### V. Agents behind one contract

- The five agents (Navigator, Page Classifier, Extractor, Validator & Deduplicator, Recovery) MUST implement the shared `Agent` protocol and return `AgentResult` (Tech Stack §4).
- Agents MUST NOT write to storage. Only the Orchestrator/Repository commits results.
- All cross-component data MUST be typed Pydantic models; no untyped dicts across module boundaries.
- Site-specific URL patterns, selectors and parsing live only in `adapters/safco.py`. Generic runtime code MUST NOT contain Safco-specific logic.
- No LLM supervisor, dynamic agent spawning or agent-to-agent LLM conversation.

### VI. Reliability by construction

- Task execution is at-least-once; every effect MUST be idempotent through unique keys and transactional upserts.
- Discovered children and the parent task's completion commit in one transaction; product/variant upserts and the product task's completion commit in one transaction.
- Database locks MUST NOT be held during network or model calls.
- Crawl and resume MUST hold the OS-level exclusive lock for the whole process; a second process fails fast.
- Budgets follow Architecture §6.1 exactly: page, product and model budgets are cumulative per run and reserved before use; time is per invocation; a `BUDGET_EXHAUSTED` run continues only with explicit extension flags; resume always uses the run's stored configuration.
- Retries are bounded, use exponential backoff with jitter and honor `Retry-After`.

### VII. Responsible crawling and security

- Respect robots.txt, site terms, configured scope and rate limits. Redirects are checked against allowed hosts.
- On 401, 403, CAPTCHA or any explicit restriction: stop or pause the affected scope. Never bypass, never sign in, never rotate identities to evade limits.
- Scraped content is untrusted data. Instruction-like page text MUST NOT influence control flow. Model tools are read-only over immutable snapshots, with no network, filesystem, shell, database or secret access.
- Secrets come only from environment variables. `.env` is git-ignored; `.env.example` lists names only. Secrets and full prompts MUST NOT be logged or committed.
- No live load or stress testing against the target site.

### VIII. The schema is a contract

- PRD §5 is the single definition of the export schema. JSON, CSV, SQLite and evidence use the same field names, enums and path convention.
- `product_id` and `variant_id` are deterministic and stable across runs; runs are separated by `run_id`, never by changing an ID.
- Source identifiers are strings. Prices are decimal strings; floats MUST NOT be used for money.
- Any schema change MUST update PRD §5, the Pydantic models, both exporters and the round-trip test in the same commit.

### IX. Tests that matter, kept light

- The P0 suite (Roadmap §3.1) MUST pass before every commit to the main branch.
- Tests run offline against saved fixtures with fake fetch and fake model implementations. CI MUST NOT hit the live site or a paid model API.
- Every fixed bug gets a fixture or test that reproduces it.
- New test files outside the planned suite need a reason; prefer extending an existing file.

### X. Observable by default

- Logs are structured JSON. Every event carries `run_id`, `task_id` (if any), `agent`, `event`, outcome and duration; model events add model ID, tool names and token usage.
- Every run writes `run_report.json` including the §10.1 POC quality metrics.
- Failures are recorded with a typed error code, never swallowed.

### XI. Simplicity

- Use the stack in Tech Stack §2. Adding a dependency, service or infrastructure component requires an ADR entry stating the alternative considered and the trade-off.
- Prefer the standard library when it is adequate.
- One process, one SQLite database. No dashboard, no distributed workers, no queue, no history tables in the POC.
- No speculative abstraction: build for the Safco adapter that exists, keep the adapter boundary clean, and stop there.

---

## Technical constraints

| Area | Constraint |
|---|---|
| Language | Python 3.11+, tested version pinned |
| Packaging | `uv`, `pyproject.toml`, committed `uv.lock` |
| Fetching | Playwright (Chromium) primary; HTTPX only via ADR after M0 |
| Parsing | Beautiful Soup + lxml |
| Contracts | Pydantic v2 |
| Orchestration | `asyncio`; LangGraph only inside the Extractor, behind the `ExtractorAgent` interface |
| Storage | SQLite (WAL) via `aiosqlite` |
| Quality | Ruff (lint + format), pytest, pytest-asyncio |
| Platform | Linux/macOS (`fcntl.flock`) |

---

## Development workflow

1. **Before coding a milestone:** re-read its Roadmap exit criteria and the relevant doc sections.
2. **After verifying an assumption** (PRD §9): record the outcome there in the same change, and adjust the design if it was wrong.
3. **Significant decisions:** add or update an ADR in Tech Stack §3.
4. **Definition of done for every change:**
   - `uv run ruff check` and `uv run ruff format --check` pass;
   - the P0 suite passes;
   - docs are updated if behavior, schema, config or commands changed;
   - no secrets, `.env`, database files or unlabeled synthetic data are staged;
   - configuration defaults still run in deterministic mode without credentials.
5. **Commits:** small and focused, imperative subject line, one logical change each.
6. **Before release:** every P0 checklist item is done or disclosed as a gap in the README; README follows Roadmap §7; sample outputs come from one identified run.

---

## Hard prohibitions (quick reference)

- Fabricating or guessing SKUs, prices or any exported value.
- Presenting planned, estimated or fixture results as live measured results.
- Model calls in the Orchestrator, Navigator or Validator; model-controlled navigation.
- Agents writing directly to storage.
- Bypassing access controls, CAPTCHA, sign-in or rate limits.
- Committing or logging secrets.
- Floats for money; generated identifiers; changing IDs to separate runs.
- Implementing P2 items, or P1 items before P0 gates pass.
- Live-site or paid-API calls in automated tests.
- Holding a database lock across network or model I/O.

---

## Governance

- This constitution supersedes conflicting guidance anywhere in the repository.
- **Amendments** are made by editing this file in a dedicated commit that states the reason and updates any affected docs.
- **Versioning** (semantic):
  - MAJOR: a principle is removed or redefined incompatibly;
  - MINOR: a principle or section is added or materially expanded;
  - PATCH: clarifications and wording.
- Every review, human or AI, checks the change against this document. A violation is either fixed or justified through an amendment, never left in place.

---

## References

- [PRD](docs/01-prd.md) — scope, requirements, schema (§5), assumptions (§9)
- [Technical Stack](docs/02-technical-stack.md) — stack, ADRs, agent contract, configuration
- [Architecture](docs/03-overall-architecture.md) — workflow, evidence gate, reliability and budgets (§6), metrics (§10)
- [Roadmap](docs/04-roadmap.md) — milestones, P0 test suite (§3.1), README outline (§7)
