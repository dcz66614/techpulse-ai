-- TechPulse-AI P10 initial schema for PostgreSQL 16.
-- Search indexes and vector columns belong to I10, not this migration.

CREATE TABLE source_registry (
    source_id TEXT PRIMARY KEY,
    source_family TEXT NOT NULL,
    base_url TEXT NOT NULL,
    policy_snapshot_id TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    requests_per_minute INTEGER NOT NULL DEFAULT 10,
    checked_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE raw_records (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_registry (source_id),
    source_record_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    abstract_or_excerpt TEXT,
    published_at TIMESTAMPTZ,
    collected_at TIMESTAMPTZ NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE evidence_items (
    evidence_id TEXT PRIMARY KEY,
    canonical_id TEXT REFERENCES canonical_entities (canonical_id),
    source_record_id BIGINT NOT NULL REFERENCES raw_records (id),
    source_family TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    quality_tier TEXT NOT NULL,
    license_status TEXT NOT NULL,
    embedding_status TEXT NOT NULL DEFAULT 'pending',
    sha256 CHAR(64) NOT NULL
);

CREATE TABLE trend_scores (
    trend_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    window_days INTEGER NOT NULL CHECK (window_days IN (7, 28)),
    score DOUBLE PRECISION NOT NULL,
    confidence TEXT NOT NULL,
    scored_at TIMESTAMPTZ NOT NULL,
    features_json JSONB NOT NULL,
    source_families JSONB NOT NULL
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
    published_at TIMESTAMPTZ NOT NULL,
    generated_by_ai BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE task_runs (
    run_id UUID NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    input_sha256 CHAR(64) NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    budget_spent_usd NUMERIC(12,5) NOT NULL DEFAULT 0,
    result_uri TEXT,
    PRIMARY KEY (run_id, task_id, attempt)
);
