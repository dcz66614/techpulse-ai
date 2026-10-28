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
