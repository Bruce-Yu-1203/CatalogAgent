# CatalogAgent — Technical Stack and Engineering Decisions

**Version:** 3.0 — Production-minded POC  
**Companion documents:** [PRD](01-prd.md) · [Architecture](03-overall-architecture.md) · [Roadmap](04-roadmap.md)  
**Assumptions:** see [PRD §9](01-prd.md#9-assumptions-and-open-questions).

## 1. Design principles

Use the simplest reliable mechanism for each decision. Every agent is rule-first and calls a model only where page semantics are genuinely uncertain. Site adapters are separate from generic runtime infrastructure. The system is one runnable Python application, not a set of distributed services.

## 2. Stack selection

| Layer | Technology | Rationale |
|---|---|---|
| Language/runtime | Python 3.11+ (tested version pinned) | Mature web, async and AI libraries. |
| Package management | `pyproject.toml`, `uv`, committed lockfile | Reproducible installs, including the browser version. |
| CLI | Typer | Typed commands for crawl, resume, export, report and evaluate. |
| Configuration | YAML + Pydantic Settings | Validated configuration; credentials from the environment. |
| Fetching | Playwright (async Chromium) | Safco listing content renders client-side (A-03). HTTPX is added only if M0 finds a permitted JSON or server-rendered endpoint. |
| Parsing | Beautiful Soup with lxml | DOM, structured metadata, tables and navigation links. |
| Orchestration | Python `asyncio` scheduler; LangGraph for the Extractor Agent loop | Deterministic outer loop; explicit state machine for the validate → recover → revalidate cycle (ADR-09). |
| LLM | LangChain chat-model integration; provider/model configurable | Tool calling and schema-constrained output; one tested model pinned for the demo. |
| Contracts | Pydantic v2 | Typed agent inputs/outputs, errors and the §5 export schema. |
| Persistence | SQLite via `aiosqlite`, WAL mode | Run-scoped catalog, crawl tasks and JSON provenance in one file; atomic commits. |
| Export | Python `csv` and `json` | Flat variant CSV and nested product JSON, no extra dependency. |
| Logging | `logging` with a JSON formatter | Correlated run/task/agent events without an observability stack. |
| Testing | pytest, pytest-asyncio, saved fixtures | Parser, agent contract, state transition, integration and recovery tests. |
| Quality tooling | Ruff | Linting and formatting. |
| Deployment | Local CLI; Docker image (official Playwright Python base) after P0/P1 | Reproducible setup now; the same image runs as a scheduled cloud job later (Architecture §9). |

SQLite is chosen for the single-process POC, not as a multi-host queue. PostgreSQL replaces it when independent workers are needed.

## 3. Architecture decisions (ADRs)

| Decision | Alternatives considered | Trade-off |
|---|---|---|
| ADR-01: Deterministic orchestrator | LLM supervisor; dynamic agent spawning | Predictable scope, lower cost, testable. The workflow is known in advance, so planning is not where a model adds value. |
| ADR-02: Rule-first agents with selective LLM reasoning | LLM on every page; pure scraper | Fast, cheap path for regular pages; model reasoning for irregular layouts, unknown pages and failure diagnosis. Requires reliable gap detection and validation. |
| ADR-03: Five agents with one contract | One monolithic script; one general-purpose agent | Clear responsibilities, independent tests and replacement. An agent is defined by owning a decision with typed input/output, whether or not it calls a model. |
| ADR-04: One fetch path first | Mandatory browser + HTTP stack | Playwright because content is client-rendered; a second path only if M0 justifies it. |
| ADR-05: Local persistent frontier | In-memory queue; Kafka; Redis | Crash recovery and atomic task transitions with minimal infrastructure. An OS-backed exclusive lock gives simple restart recovery. PostgreSQL and fenced claims come with independent workers. |
| ADR-06: Snapshot-grounded evidence for SKU and price | Trust LLM output; second LLM as judge; evidence for every field | Independent checks for identity, values and row association on the two highest-risk fields; other fields are schema-checked and flagged in `llm_fields` when model-supplied, keeping the POC small. The Validator never uses a model, so it cannot share the Extractor's errors. Ambiguous fields stay unresolved. |
| ADR-07: Offline replay evaluation | Repeated live crawls | Identical inputs across modes, less load on the source. Snapshots go stale and cannot test every live behavior. |
| ADR-08: Product + variant schema | One row per page; untyped JSON | Preserves sellable SKU identity and category-specific attributes, at the cost of joins. |
| ADR-09: LangGraph for the Extractor loop | Provider SDK tool-calling loop (~100 lines); LangChain AgentExecutor; CrewAI/AutoGen | LangGraph state and conditional edges map directly onto validate → tool loop → revalidate, with a built-in recursion limit and inspectable state per step, and LangChain lets the provider change via config. Cost: two dependencies and a layer of abstraction for a small loop. Multi-agent frameworks were rejected because they add LLM-to-LLM coordination the workflow does not need. Exit: the graph sits behind the `ExtractorAgent` interface, so a plain async loop can replace it without touching callers if the framework slows M3. |
| ADR-10: Suggest, never apply, selector repairs | Auto-patch selectors; no repair support | Repair suggestions shorten the fix when layouts change, without letting a model silently change parsing for every later page. A human applies and regression-tests the change. |
| ADR-11: Do not fetch robots-disallowed pagination (added after M0) | Fetch `?page=N`; click "Next" in the browser (client-side routing); query Algolia directly | robots.txt disallows `/*?page=`. Clicking through client-side pagination would avoid the literal URL but not the intent, and Algolia is a third-party host outside `allowed_hosts`. The Navigator records `next_page_robots_disallowed`; coverage comes from page 1 of each root and subcategory listing. Cost: listings with more than 12 products are sampled, not exhausted. |
| ADR-12: Model provider via `langchain-anthropic` only (added at M3) | `init_chat_model` with several provider packages | One provider package keeps the dependency set small; other providers raise `MODEL_UNAVAILABLE` until their package is added. |

## 4. Agent roster

A deterministic **Orchestrator** (scheduler) dispatches tasks to five agents. It is not itself an agent and uses no model.

Every agent implements the same contract:

```python
class AgentResult(BaseModel):
    outcome: Literal["ok", "partial", "retry", "quarantine", "failed"]
    output: BaseModel | None  # agent-specific typed payload
    issues: list[Issue]  # typed validation or processing issues
    events: list[Event]  # structured telemetry
    budget_used: Budget  # model calls, tool calls, tokens, seconds


class Agent(Protocol):
    name: str

    async def run(self, task: AgentInput) -> AgentResult: ...
```

| Agent | Decision it owns | Deterministic core | LLM role | Priority |
|---|---|---|---|---|
| **Navigator** | Which links and pages to enqueue; pagination continuation and stop | URL patterns, DOM links, pagination detection, scope filter | None. Navigation stays rule-bound so the model can never widen crawl scope. | P0 |
| **Page Classifier** | Page type: `category`, `listing`, `product`, `other`, `blocked` | URL, DOM and metadata signals | Fallback when rules return `unknown`: one call choosing from the closed enum with evidence section IDs. Accepted only if the DOM signals required for that type are present. Off by default. | Rules P0, fallback P1 |
| **Extractor** | Product and variant candidate | Structured data and DOM parsers | Bounded tool loop that resolves recoverable gaps with evidence references. | P0 |
| **Validator & Deduplicator** | Accept, partial or quarantine; identity merge | Schema, evidence, row-alignment and identity rules | None, so the check stays independent of the model that produced the candidate. | P0 |
| **Recovery** | Retry, back off, pause or quarantine; diagnose repeated failures | Retry policy by error class | After repeated parse failures on one field and template, proposes replacement selectors. Each is tested on the frozen failing snapshots; ranked results go to `outputs/repair_suggestions.json`. Never auto-applied. Off by default. | Retry P0, suggestions P1 |

### Tools

All tools operate on immutable snapshots and are read-only.

| Tool | Used by | Contract |
|---|---|---|
| `inspect_sections()` | Extractor, Classifier | Bounded list of section IDs and metadata for the current snapshot. |
| `get_section(section_id)` | Extractor, Classifier, Recovery | One permitted section, including captured structured data. |
| `get_variant_rows()` | Extractor | Variant evidence with stable row IDs. |
| `test_selector(selector)` | Recovery | Match count and short sample text for a CSS selector across the frozen failing snapshots. |

Agents return typed results through structured output, not through a write tool. The runtime enforces schema validation, argument bounds, maximum tool-result size and call/time budgets outside the model. Models have no access to live URLs, shell, secrets or storage.

## 5. Configuration

```yaml
site:
  adapter: safco
  allowed_hosts: [www.safcodental.com, safcodental.com]
  scope: selected  # selected | all (permitted discovered roots)
  discovery_url: https://www.safcodental.com/catalog   # added at M1: page that lists the roots
  category_roots:
    - https://www.safcodental.com/catalog/gloves
    - https://www.safcodental.com/catalog/sutures-surgical-products
crawler:
  concurrency: 2
  request_interval_seconds: 2
  timeout_seconds: 30
  max_attempts: 3
  max_pages: 150
  max_products_per_category: 10
  max_invocation_seconds: 3600   # per crawl/resume process; page/product/model budgets are cumulative per run (Architecture §6.1)
  readiness_timeout_seconds: 15  # added at M1: wait for listing/product DOM signals
  backoff_base_seconds: 2        # added at M2
  backoff_max_seconds: 60
agents:
  extractor:
    llm_enabled: true              # P0; ignored in --mode deterministic
    max_model_calls_per_task: 3
    max_tool_calls_per_task: 6
    max_seconds_per_task: 60
  classifier:
    llm_fallback_enabled: false    # P1: enable only once implemented and tested
    max_model_calls_per_task: 1
  recovery:
    repair_suggestions_enabled: false   # P1: enable only once implemented and tested
    trigger_after_failures: 3      # same field, same page template
    max_suggestions_per_run: 5
llm:
  max_model_calls_per_run: 60
  max_input_tokens_per_call: 6000
  max_output_tokens_per_call: 1200
storage:
  database: data/catalog.db
  snapshot_dir: data/snapshots
  output_dir: outputs
```

The two P1 LLM features ship disabled, so the default configuration only exercises tested behavior; they are switched on once their P1 tests pass. Defaults are deliberately conservative; they are lowered if robots.txt or site terms require it (A-02). Browser subrequests also load the host, so the per-host limiter paces both navigations and controlled HTTP calls, and unnecessary resources (fonts, analytics, large media) are blocked where safe.

Exclusive ownership uses a nonblocking `fcntl.flock` on a database-specific lock file (Linux/macOS; Windows is out of scope for the POC). The lock is held for the whole crawl/resume and the lock file is never deleted while a process may own it.

Credentials (`LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`) come from `.env` or the shell. `.env.example` is committed; `.env` is git-ignored. Deterministic mode runs without credentials. Hybrid mode with an unavailable provider reports degraded recovery explicitly.

## 6. Repository layout

```text
catalog-agent/               # repository root (GitHub: Bruce-Yu-1203/catalog-agent)
├── pyproject.toml           # package `catalog-agent`, CLI `catalog-agent`
├── uv.lock
├── README.md
├── AGENTS.md
├── constitution.md
├── .env.example
├── .gitignore
├── configs/safco.yaml
├── docs/                    # PRD, tech stack, architecture, roadmap, implementation log
├── src/catalog_agent/
│   ├── cli.py
│   ├── config.py
│   ├── contracts.py          # AgentResult, export schema models
│   ├── adapters/safco.py
│   ├── runtime/
│   │   ├── orchestrator.py
│   │   ├── runner.py         # crawl/resume lifecycle, budgets, extensions
│   │   ├── fetcher.py        # Playwright + offline replay fetcher
│   │   ├── robots.py
│   │   └── lock.py
│   ├── agents/
│   │   ├── navigator.py
│   │   ├── classifier.py
│   │   ├── extractor.py      # LangGraph subgraph
│   │   ├── validator.py
│   │   └── recovery.py
│   ├── extraction/
│   │   ├── llm.py            # ModelClient boundary
│   │   ├── tools.py
│   │   └── normalize.py
│   ├── storage/
│   │   ├── schema.sql
│   │   ├── repository.py     # frontier tasks, run state and catalog
│   │   └── export.py
│   ├── quality.py            # run-level data-quality metrics
│   └── telemetry.py
├── tests/
│   ├── fixtures/live/        # real snapshots + manifest for replay
│   ├── test_discovery.py     # P0 suite: Roadmap §3.1
│   ├── test_extraction.py
│   ├── test_agent_loop.py
│   ├── test_persistence.py
│   └── test_runtime.py
├── scripts/smoke.sh
└── outputs/                  # genuine sample exports and measured reports
```

The frontier lives in `storage/repository.py` (one module owns every SQL transaction) rather than a separate
`runtime/frontier.py`; Safco parsing lives in `adapters/safco.py` (no separate `extraction/parsers.py`).
`evaluation.py` (P1, FR-13) and the optional `Dockerfile` are not built.

## 7. Developer interface

```bash
uv sync
uv run playwright install chromium
uv run catalog-agent crawl --config configs/safco.yaml --mode hybrid
uv run catalog-agent resume --run-id RUN_ID
uv run catalog-agent resume --run-id RUN_ID --add-pages 50 --add-model-calls 20   # after BUDGET_EXHAUSTED
uv run catalog-agent resume --run-id RUN_ID --add-products 5   # added at M2: raise the per-category product budget
uv run catalog-agent export --run-id RUN_ID --format json
uv run catalog-agent export --run-id RUN_ID --format csv
uv run catalog-agent report --run-id RUN_ID
uv run catalog-agent evaluate --fixtures tests/fixtures --mode compare   # P1 (FR-13): not built
uv run catalog-agent crawl --replay tests/fixtures/live --db /tmp/replay.db   # added at M2: offline replay
uv run pytest
```

Exports correspond to one run. Each run records its configuration, model version, parser version and snapshot IDs so results stay interpretable.

## 8. Production evolution

Production changes are triggered by need: PostgreSQL and fenced leases when several processes need independent ownership; shared host pacing and budgets when scaling across hosts; monitoring and retention once runs are recurring. The concrete deployment path is in [Architecture §9](03-overall-architecture.md#9-deployment-path) and data-quality monitoring in [§10](03-overall-architecture.md#10-production-data-quality-monitoring).
