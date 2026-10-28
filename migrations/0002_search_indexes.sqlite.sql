-- SQLite stand-in for 0002_search_indexes.sql.
-- pgvector remains the PostgreSQL target in that file. Embeddings here are
-- float32 BLOBs. There is no PostgreSQL on this host, so no EXPLAIN was run.
-- BM25 is computed in Python from the body column.

CREATE TABLE IF NOT EXISTS index_versions (
    index_version TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    corpus_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staging', 'published', 'degraded', 'retired')),
    entry_count INTEGER NOT NULL,
    degraded INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS chunk_vectors_staging (
    index_version TEXT NOT NULL,
    vector_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    language TEXT NOT NULL,
    license_status TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    vector_sha256 TEXT NOT NULL,
    embedding BLOB,
    body TEXT NOT NULL,
    PRIMARY KEY (index_version, vector_id)
);

CREATE TABLE IF NOT EXISTS chunk_vectors (
    index_version TEXT NOT NULL REFERENCES index_versions (index_version),
    vector_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    language TEXT NOT NULL,
    license_status TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    vector_sha256 TEXT NOT NULL,
    embedding BLOB,
    body TEXT NOT NULL,
    PRIMARY KEY (index_version, vector_id)
);

CREATE TABLE IF NOT EXISTS index_pointer (
    name TEXT PRIMARY KEY CHECK (name IN ('published', 'text')),
    index_version TEXT NOT NULL REFERENCES index_versions (index_version)
);

CREATE INDEX IF NOT EXISTS ix_index_versions_run ON index_versions (run_id);
CREATE INDEX IF NOT EXISTS ix_chunk_vectors_language ON chunk_vectors (language);
CREATE INDEX IF NOT EXISTS ix_chunk_vectors_source
    ON chunk_vectors (source_id, source_record_id);
