-- Four core tables (Architecture §5). Field definitions follow PRD §5.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id              TEXT PRIMARY KEY,
    mode                TEXT NOT NULL,              -- deterministic | hybrid
    config_json         TEXT NOT NULL,              -- the run's stored configuration (used on resume)
    status              TEXT NOT NULL,
    termination_reason  TEXT,
    started_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    finished_at         TEXT,
    max_pages           INTEGER NOT NULL,
    max_products_per_category INTEGER NOT NULL,
    max_model_calls     INTEGER NOT NULL,
    pages_used          INTEGER NOT NULL DEFAULT 0,  -- fetch attempts reserved (retries included)
    model_calls_used    INTEGER NOT NULL DEFAULT 0,  -- reserved model calls
    input_tokens_used   INTEGER NOT NULL DEFAULT 0,
    output_tokens_used  INTEGER NOT NULL DEFAULT 0,
    extensions_json     TEXT NOT NULL DEFAULT '[]',
    invocations_json    TEXT NOT NULL DEFAULT '[]',
    counters_json       TEXT NOT NULL DEFAULT '{}',
    metrics_json        TEXT
);

CREATE TABLE IF NOT EXISTS crawl_tasks (
    task_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES crawl_runs(run_id),
    task_key        TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    url             TEXT NOT NULL,
    root            TEXT,
    lineage_json    TEXT NOT NULL DEFAULT '[]',
    depth           INTEGER NOT NULL DEFAULT 0,
    priority        INTEGER NOT NULL DEFAULT 2,
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    reserved_model_calls INTEGER NOT NULL DEFAULT 0,
    next_retry_at   REAL,
    snapshot_id     TEXT,
    snapshot_path   TEXT,
    content_hash    TEXT,
    page_type       TEXT,
    outcome_json    TEXT,
    last_error      TEXT,
    error_code      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (run_id, task_key)
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON crawl_tasks(run_id, status, priority, task_id);

CREATE TABLE IF NOT EXISTS products (
    run_id              TEXT NOT NULL REFERENCES crawl_runs(run_id),
    product_id          TEXT NOT NULL,
    source              TEXT NOT NULL,
    product_key         TEXT NOT NULL,
    url                 TEXT NOT NULL,
    name                TEXT NOT NULL,
    brand               TEXT,
    manufacturer        TEXT,
    description         TEXT,
    category_paths_json TEXT NOT NULL,
    image_urls_json     TEXT NOT NULL,
    attributes_json     TEXT NOT NULL,
    alternatives_json   TEXT NOT NULL,
    variant_count       INTEGER NOT NULL,
    record_status       TEXT NOT NULL,
    extraction_method   TEXT NOT NULL,
    llm_fields_json     TEXT NOT NULL,
    missing_json        TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    root                TEXT,
    task_id             INTEGER,
    PRIMARY KEY (run_id, product_id)
);

CREATE TABLE IF NOT EXISTS variants (
    run_id           TEXT NOT NULL,
    variant_id       TEXT NOT NULL,
    product_id       TEXT NOT NULL,
    sku              TEXT NOT NULL,
    variant_name     TEXT,
    attributes_json  TEXT NOT NULL,
    pack_label       TEXT,
    pack_quantity    INTEGER,
    pack_unit        TEXT,
    price            TEXT,
    currency         TEXT,
    price_visibility TEXT NOT NULL,
    availability     TEXT NOT NULL,
    availability_raw TEXT,
    llm_fields_json  TEXT NOT NULL,
    evidence_json    TEXT NOT NULL,
    missing_json     TEXT NOT NULL,
    PRIMARY KEY (run_id, variant_id),
    UNIQUE (run_id, product_id, sku),
    FOREIGN KEY (run_id, product_id) REFERENCES products(run_id, product_id)
);
