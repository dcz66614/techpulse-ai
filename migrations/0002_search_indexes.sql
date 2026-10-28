-- I10 search indexes for PostgreSQL 16 + pgvector.
-- This host has no PostgreSQL, so this script is the target DDL only.
-- EXPLAIN was not executed and no plan is invented here.
-- The HNSW index is the planned cosine path. The GIN index is the planned
-- text path. Runtime BM25 is pure Python and does not read this index.
-- The local process stores the same rows in SQLite (0002_search_indexes.sqlite.sql)
-- with the embedding packed as a float32 BLOB. vector(256) matches the
-- hashed character n-gram baseline. A remote model with another dimension
-- needs a later migration before it is written to Postgres.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE index_versions (
    index_version TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    corpus_sha256 CHAR(64) NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staging', 'published', 'degraded', 'retired')),
    entry_count INTEGER NOT NULL,
    degraded BOOLEAN NOT NULL,
    manifest_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE TABLE chunk_vectors_staging (
    index_version TEXT NOT NULL,
    vector_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    language TEXT NOT NULL,
    license_status TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    vector_sha256 CHAR(64) NOT NULL,
    embedding vector(256),
    body TEXT NOT NULL,
    PRIMARY KEY (index_version, vector_id)
);

CREATE TABLE chunk_vectors (
    index_version TEXT NOT NULL REFERENCES index_versions (index_version),
    vector_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    language TEXT NOT NULL,
    license_status TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    vector_sha256 CHAR(64) NOT NULL,
    embedding vector(256),
    body TEXT NOT NULL,
    PRIMARY KEY (index_version, vector_id)
);

CREATE TABLE index_pointer (
    name TEXT PRIMARY KEY CHECK (name IN ('published', 'text')),
    index_version TEXT NOT NULL REFERENCES index_versions (index_version)
);

CREATE INDEX ix_index_versions_run ON index_versions (run_id);
CREATE INDEX ix_chunk_vectors_language ON chunk_vectors (language);
CREATE INDEX ix_chunk_vectors_source ON chunk_vectors (source_id, source_record_id);
CREATE INDEX ix_chunk_vectors_embedding_hnsw
    ON chunk_vectors USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ix_chunk_vectors_body_gin
    ON chunk_vectors USING gin (to_tsvector('simple', body));
