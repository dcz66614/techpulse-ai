-- SQLite stand-in for 0001_init.sql. PostgreSQL types are mapped to affinities
-- this interpreter can execute. Vector columns are omitted, same as the
-- PostgreSQL script: I10 owns that DDL.

CREATE TABLE source_registry (
    source_id TEXT PRIMARY KEY,
    source_family TEXT NOT NULL,
    base_url TEXT NOT NULL,
    policy_snapshot_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    requests_per_minute INTEGER NOT NULL DEFAULT 10,
    checked_at TEXT NOT NULL
);

CREATE TABLE raw_records (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_registry (source_id),
    source_record_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    abstract_or_excerpt TEXT,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    license_status TEXT NOT NULL,
    policy_snapshot_id TEXT NOT NULL,
    raw_uri TEXT NOT NULL,
    UNIQUE (source_id, source_record_id, content_sha256)
);

CREATE TABLE canonical_entities (
    canonical_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE evidence_items (
    evidence_id TEXT PRIMARY KEY,
    canonical_id TEXT REFERENCES canonical_entities (canonical_id),
    source_record_id INTEGER NOT NULL REFERENCES raw_records (id),
    source_family TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    quality_tier TEXT NOT NULL,
    license_status TEXT NOT NULL,
    embedding_status TEXT NOT NULL DEFAULT 'pending',
    sha256 TEXT NOT NULL
);

CREATE TABLE trend_scores (
    trend_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    window_days INTEGER NOT NULL CHECK (window_days IN (7, 28)),
    score REAL NOT NULL,
    confidence TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    features_json TEXT NOT NULL,
    source_families TEXT NOT NULL
);

CREATE TABLE trend_evidence (
    trend_id TEXT REFERENCES trend_scores (trend_id),
    evidence_id TEXT REFERENCES evidence_items (evidence_id),
    relation TEXT NOT NULL CHECK (relation IN ('support', 'contradict')),
    PRIMARY KEY (trend_id, evidence_id, relation)
);

CREATE TABLE published_reports (
    report_id TEXT PRIMARY KEY,
    report_kind TEXT NOT NULL,
    content_uri TEXT NOT NULL,
    qa_manifest_uri TEXT NOT NULL,
    evidence_snapshot_uri TEXT NOT NULL,
    published_at TEXT NOT NULL,
    generated_by_ai INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE task_runs (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    budget_spent_usd NUMERIC NOT NULL DEFAULT 0,
    result_uri TEXT,
    PRIMARY KEY (run_id, task_id, attempt)
);
