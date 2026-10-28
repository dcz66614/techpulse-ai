# Deletion map

D21 keeps a reverse map from a source record to the chunk, vector, and report
derived from it. `locate_derived(database, source_id)` returns those rows.
Pass either `source_id` or `source_record_id`.

## Chain

Each row stores `source_id`, `source_record_id`, `chunk_id`, `chunk_uri`,
`vector_id`, `vector_uri`, `report_id`, and `report_uri`.

The table is `deletion_map` in the SQLite file the caller passes. This step
does not open Postgres. `run_guard` records whether the index for that run
is isolated.

Chunk and vector bytes go through `ObjectStore`. `LocalObjectStore` from D10
writes them under the artifacts directory. The keys are
`{run_id}/D21/chunks/{chunk_id}.json` and
`{run_id}/D21/vectors/{vector_id}.json`. The URI is `local:` plus that key.
An S3 backend can implement the same `put` and `get` later. This process
does not talk to S3.

`attach_report` fills `report_id` and `report_uri` once a report exists.
Until then those columns are null. A later sanitize of the same chunk does
not clear a report already attached.

## Redaction

Phone numbers, email addresses, national-id and SSN shapes, passport labels,
token strings, and password or token assignments are replaced with
`[REDACTED]`. The matched characters are not stored.

Contact fields are dropped and not written: email, phone, address, username,
password, token, secret, api key, ssn, national id, passport, birthday, and
the same names with a hyphen or an underscore.

Names used for deduplication (`author`, `authors`, `author_name`, `name`,
`display_name`, `full_name`) are stored only as HMAC-SHA256 hex digests in
`name_hmacs`. The key is the environment variable `TECHPULSE_PII_HMAC_KEY`,
read from `os.environ` at run time. This module does not open a `.env` file.
Tests set a fake key on the process. The plaintext name is not written and
is not part of the embedding input. The same normalized name and key always
produce the same digest, so a later dedup step can compare digests.

The embedding input is the redacted title and excerpt, joined by a newline.
That string is on the sanitized row and in the chunk object. The vector
object stores ids only. No embedding service is called.

## Leak

If text offered as embedding input still matches a phone, email, id, token,
or password pattern, the run status is `fatal_error`. That text is not
written to the sanitized file or the object store. `manifest.json` sets
`isolated` to true, `ops_notice.json` is written for Ops, and
`run_guard.index_isolated` is set so the index for the run is not used.

A missing HMAC key, when a name field is present, is also `fatal_error`.
The name is not written. That case does not isolate the index.

## Run files

`artifacts/{run_id}/D21/sanitized.parquet` when pyarrow is installed.
Otherwise the file is `sanitized.jsonl`, and the manifest note says pyarrow
is not installed. `manifest.json` and `metrics.json` are written either way.

## Retention policy

TechPulse-AI enforces data retention boundaries across the ingestion and indexing lifecycle:

1. **180-day raw cache limit**:
   Raw text staged from external sources is retained in the local/object storage cache for at most 180 days. Beyond 180 days, raw staging files are aged out according to the data lifecycle schedule.
2. **License-tier retention rules**:
   - `fulltext_allowed`: Full text is retained for internal indexing and research synthesis. Raw text is cached up to 180 days; sanitized excerpts and vector embeddings are indexed.
   - `reusable_excerpt`: Short excerpt only (up to 280 characters); full text beyond the excerpt is dropped during normalization (D20).
   - `metadata_only`: Descriptive metadata (title, author HMAC digests, publication timestamp, canonical identifiers) is retained; raw text bodies are withheld.
   - `withheld`: Blocked, unauthorized, or unknown license status; content is withheld immediately and never enters indexing or vector storage.
3. **Permanent report archives**:
   Published synthesis reports and their cryptographically signed QA manifests (`qa_manifest.json`) are preserved permanently in the report archive (`published_reports`) for citation integrity, auditability, and historical provenance.

## Deletion drill and purge workflow

Upon receipt of a data subject deletion request (e.g. GDPR right to be forgotten) or an upstream copyright takedown for a given source record (`source_record_id`):

1. **Mapping lookup**:
   The ops deletion workflow queries `locate_derived(database, source_record_id)` against the `deletion_map` table to identify all downstream artifacts:
   - Chunk identifier (`chunk_id`) and chunk object store URI (`chunk_uri`).
   - Vector identifier (`vector_id`) and vector object store URI (`vector_uri`).
   - Report identifier (`report_id`) and report cache URI (`report_uri`).
2. **Deterministic purge actions**:
   - **Raw text**: Purged from the SQLite `raw_records` table and deleted/truncated from staged raw NDJSON or staging object storage (`raw_uri`).
   - **Chunks**: Purged from the object store at `chunk_uri` (`{run_id}/D21/chunks/{chunk_id}.json`).
   - **Vectors**: Purged from the object store at `vector_uri` (`{run_id}/D21/vectors/{vector_id}.json`) and deleted from SQLite search index tables (`chunk_vectors` and `chunk_vectors_staging`).
   - **Affected report caches**: Purged from the object store at `report_uri` and deleted from SQLite `published_reports` cache.
   - **Deletion map**: Removed from the `deletion_map` table.
3. **Retrieval verification via I20**:
   Following purge execution, I20 hybrid retrieval is re-queried (`techpulse.index.hybrid.retrieve`) against the active corpus. Verification PASS requires that the sensitive record cannot be retrieved across dense, BM25, fused, or reranked candidate lists.
4. **Customer data protection invariant**:
   Deletion drills execute exclusively against synthetic test fixtures (`drill-*` / `synthetic-*`). Real customer data is never touched or modified during drill runs.

## Backup, restore, and target PostgreSQL strategy

Postgres is not available on the dev host environment; local development and testing rely on SQLite.

1. **Target PostgreSQL path (documented target only)**:
   In production PostgreSQL environments:
   - **Backup**:
     ```bash
     pg_dump --format=custom --no-owner --no-privileges -d "${DATABASE_URL}" -f "${BACKUP_DIR}/postgres.dump"
     ```
   - **Restore**:
     ```bash
     pg_restore --clean --if-exists --no-owner --no-privileges -d "${DATABASE_URL}" "${BACKUP_DIR}/postgres.dump"
     ```
2. **SQLite development runtime**:
   - The backup process bundles the SQLite database(s), source policy decision snapshots (`docs/decisions/source_policy_*.md` and `config/policy.yaml`, `config/source_registry.yaml`), and artifact manifests (`artifacts/**/manifest.json`).
   - Backups record table row counts, file SHA-256 digests, and manifest metadata in `backup_manifest.json`.
   - Restore tests restore into an isolated temporary directory and verify that restored table row counts and SHA-256 digests exactly match the backup manifest.
