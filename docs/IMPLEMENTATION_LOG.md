# IMPLEMENTATION_LOG

## P-init
Repo initialised (local only, no remote). Specs copied to docs/spec/.

## P00 Toolchain scaffold
- Python 3.12.13 installed via `uv python install 3.12`; uv 0.12.2; git 2.55.0.windows.2.
- Dependencies added with the section-3 `uv add` / `uv add --dev` commands; `uv.lock` generated.
- Scaffold created for `src/techpulse/`, tests, data, config, contracts, migrations, prompts, reports, docs/decisions, scripts.
- Environment report run: `uv run python scripts/write_env_report.py --run-id p00-20261027T221831Z`
  → `artifacts/p00-20261027T221831Z/P00/{environment,manifest,metrics}.json` (status success).
- Docker: not installed (recorded in environment.json).

Real check output:

```text
$ uv lock --check
Resolved 80 packages in 0.79ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
55 files already formatted

$ uv run pytest -q
.                                                                        [100%]
1 passed in 0.01s
```

## P00fix Environment report fatal status
- `scripts/write_env_report.py`: `evaluate_checks` now returns `fatal_error` (with an `errors` list in manifest.json) when Python is not 3.12.x or uv/git are missing; the script exits 1 in that case. Docker missing remains a warning only.
- Added `tests/unit/test_write_env_report.py` covering success, Python 3.11, missing uv, missing docker, and fatal manifest output.
- Verified report run: `uv run python scripts/write_env_report.py --run-id p00fix-verify` → status `success`, exit 0 (Docker not installed on this machine).

Real check output:

```text
$ uv lock --check
Resolved 80 packages in 0.64ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
58 files already formatted

$ uv run pytest -q
......                                                                   [100%]
6 passed in 0.03s
```

## P20 CI, dependency and secret scanning
- Added `.github/workflows/ci.yml`: one `checks` job on `ubuntu-latest` and `windows-latest`, Python 3.12, `uv sync --locked`; runs ruff check, ruff format --check, mypy, pytest (unit + contract with terminal coverage), bandit, pip-audit, and detect-secrets. No deploy job.
- Added `detect-secrets` as the only new dev dependency, plus `.secrets.baseline`, Makefile targets `secrets` and `ci`, and `docs/decisions/ci_baseline.md`.
- Added `tests/fixtures/fake_secret_sample.txt` (`ghp_` + 36 `x`) and `tests/unit/test_secret_scan.py`, which scans the fixture directly and asserts a GitHub Token finding. The fixture is excluded from the baseline-gating scan with `--exclude-files 'fake_secret_sample\.txt'`.
- `.secrets.baseline` records only the reviewed `.env.example` placeholder (`CHANGE_ME`) so the tracked-file scan is clean.
- `make` is not installed on this Windows host, so the `ci` target commands were run directly; the workflow was parsed locally but not executed (no remote).

Real check output:

```text
$ uv lock --check
Resolved 81 packages in 0.77ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
62 files already formatted

$ uv run mypy
Success: no issues found in 49 source files

$ uv run pytest -q
.......                                                                  [100%]
7 passed in 0.49s

$ uv run pytest tests/unit tests/contract -q --cov=src/techpulse --cov-report=term-missing
.......                                                                  [100%]
______________ coverage: platform win32, python 3.12.13-final-0 _______________

Name                                       Stmts   Miss  Cover   Missing
------------------------------------------------------------------------
...
src\techpulse\workflows\orchestrator.py        0      0   100%
------------------------------------------------------------------------
TOTAL                                          1      0   100%
7 passed in 1.08s

$ uv run bandit -q -r src
(no output, exit 0)

$ uv run pip-audit
No known vulnerabilities found
Name         Skip Reason
------------ ---------------------------------------------------------------------------
techpulse-ai Dependency not found on PyPI and could not be audited: techpulse-ai (0.1.0)

$ uv run detect-secrets-hook --baseline .secrets.baseline --exclude-files 'fake_secret_sample\.txt' @(git -c core.quotePath=false ls-files)
(no output, exit 0)
```

Secrets scan without the exclusion does flag the fixture, as asserted by `test_secret_scanner_flags_fake_github_token`.

## P20fix Secret-scan step shell
The `Secret scan` step in `.github/workflows/ci.yml` now sets `shell: bash`, so its `$(git -c core.quotePath=false ls-files)` command substitution is evaluated by the Git Bash available on `windows-latest` instead of the default PowerShell shell (which does not expand `$(...)` that way); nothing else in the workflow changed. Verified locally with `uv run ruff check .` (All checks passed!) and `uv run pytest -q` (7 passed).

## P10 Contracts and migration
- Added draft 2020-12 JSON Schemas under `contracts/` for the task envelope, task result, raw record, evidence item, trend, and report (`schema_version` 1.0).
- Added matching Pydantic v2 models in `src/techpulse/contracts.py`. Direct dependencies: `pydantic` and `jsonschema`. Dev stub `types-jsonschema` so strict mypy can import jsonschema.
- `migrations/0001_init.sql` is the PostgreSQL 16 DDL from section 5. Alembic revision `0001` runs that script on PostgreSQL and `migrations/0001_init.sqlite.sql` on SQLite, and drops the eight tables on downgrade. This machine has no PostgreSQL, so the contract test runs up/down/up on a `tempfile.TemporaryDirectory` SQLite file.

Real check output:

```text
$ uv lock --check
Resolved 87 packages in 0.86ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
70 files already formatted

$ uv run mypy
Success: no issues found in 51 source files

$ uv run pytest -q
.........................................                                [100%]
41 passed in 1.37s
```

## S20 Egress, least privilege, and prompt guard
- `config/policy.yaml` is default deny. Each section 1.2 role has `network: none` or `network: allowlisted_get`, and a tool allow-list. Only CollectorAgent has network access.
- `check_egress(role, method, url)` allows GET and HEAD to allowlisted hosts for that collector role. Cleaner, Writer, and QA have no network. Anything else raises `PolicyBlocked`. `GuardedTransport`, `AsyncGuardedTransport`, and `egress_request_hook` apply the check to httpx.
- The host allow-list is the enabled sources in `config/source_registry.yaml` when that file exists, and `network_allowlist.domains` in `config/policy.yaml` otherwise. S10 has not added the registry on this branch. Tests use `tests/fixtures/source_registry.yaml`.
- Tool calls go through `authorize_tool_call`: the role must list the tool, then Pydantic validates the arguments. `put_raw`, `write_evidence`, and `update_task_status` use the P10 `RawRecord`, `EvidenceItem`, and `TaskResult` models. `UntrustedData` fences third-party text so a malicious README is not executed. `write_audit_log` redacts secret-looking strings.
- This machine has no container runtime. Isolation is in-process. Container network policy is recorded as future work in `docs/decisions/s20_isolation.md`.
- Added direct dependency `pyyaml` and dev stub `types-pyyaml` so strict mypy can import the registry loader.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 1ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
74 files already formatted

$ uv run mypy
Success: no issues found in 52 source files

$ uv run pytest -q
..............................................................           [100%]
62 passed in 1.56s
```
## S10 Data source policy registry
- `config/source_registry.yaml`: Created policy registry covering all 8 specified sources across Phase 1 and Phase 2.
  - Phase 1 (`openalex`, `crossref`, `arxiv`, `github`, `hackernews`): Verified against live official documentation, terms of service, and robots pages; set `enabled: true`. Every field (allowed endpoints, HTTP methods GET only, domain whitelist, rate limits / polite-pool rules, required User-Agent / mailto, auth needs, attribution, caching/retention, redistribution rights, prohibitions) is backed by quoted official evidence.
  - Phase 2 (`sec_edgar`, `cninfo`, `rss`): Kept strictly `enabled: false`. Verified and recorded official documentation and limitations (cninfo requires SZSE SSO authenticated account with unverified rate limits; RSS is a decentralized format without a single provider base URL; SEC EDGAR requires Cd approval before Phase 2 activation).
- `docs/decisions/source_policy_<source>.md`: Created detailed decision records for each of the 8 sources containing verification metadata, policy snapshot SHA-256 hashes, domain whitelists, rate limits, and verbatim quoted evidence.
- Added `tests/unit/test_source_registry.py`: 25 unit tests asserting registry structure, Phase 1 enabled status, Phase 2 disabled status, mandatory fields, GET-only HTTP methods, domain formats, quote validation, and existence of decision documents.
- Real check output: all 31 tests passing, ruff lint and format check passing.

## S10fix Remove invented HN limits
- config/source_registry.yaml (hackernews): removed invented 60 req/min and 2-connection concurrency limits from verified rate_limits; recorded the official quote `official_rate_limit: "There is currently no rate limit."` and moved client-side pacing to a clearly labelled `self_imposed_throttle` block with note "project choice, not an official limit"; removed the stale top-level `requests_per_minute: 60`.
- Also corrected numbers not backed by quoted official sentences elsewhere: crossref (official pool table Public 5/1, Polite 10/3, Plus 150/None), openalex/sec_edgar/rss concurrency_limit -> "not stated", github concurrency 100 with supporting quote, and search limits marked "not stated"; matching edits in docs/decisions/source_policy_{crossref,hackernews,openalex,github,sec_edgar}.md; supporting quotes added for corrected values.
- Tests: 25 passed in tests/unit/test_source_registry.py (no old numeric assertions needed changing).

## C10 OpenAlex collector
- `src/techpulse/collectors/base.py`: SQLite watermark and provenance store, header-aware interval limiter, and guarded GET (collector role, explicit httpx timeout). 429 waits for Retry-After. 401/403/captcha is policy_blocked. A failed page does not advance the watermark. INSERT OR IGNORE keeps an existing row.
- `src/techpulse/collectors/openalex.py`: cursor-paged GET of `https://api.openalex.org/works` with the registry User-Agent and mailto. Works become RawRecord rows. Institution and topic ids are stored as provenance. Missing abstracts stay empty. A later update inserts a new hash and leaves the old provenance row.
- Fixtures under `tests/fixtures/openalex/` (the 100-work page is 46768 bytes). No live HTTP.
- `tests/unit/test_source_registry.py`: added the missing `dict[str, Any]` arguments so strict mypy can check the file that was already on this branch.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.63ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
86 files already formatted

$ uv run mypy
Success: no issues found in 55 source files

$ uv run pytest -q
........................................................................ [ 71%]
.............................                                            [100%]
101 passed in 3.54s
```

## C11 Crossref collector
- `src/techpulse/collectors/crossref.py`: cursor-paged GET of `https://api.crossref.org/works` with the registry User-Agent, Accept, and mailto. DOIs are lower-cased and `doi:` / `doi.org` / `dx.doi.org` prefixes are removed. The same DOI on another page is not stored again. Provenance keeps DOI, publication date, type, title, link URLs, and license URLs. `rows` is capped at the registry maximum of 1000. 429 waits for Retry-After. Other network errors retry at most twice. 403 is policy_blocked and leaves the watermark on the last successful page.
- Fixtures under `tests/fixtures/crossref/`. No live HTTP.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.67ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
87 files already formatted

$ uv run mypy
Success: no issues found in 56 source files

$ uv run pytest -q
........................................................................ [ 62%]
...........................................                              [100%]
115 passed in 3.82s
```

## C12 arXiv collector
- `src/techpulse/collectors/arxiv.py`: GET `https://export.arxiv.org/api/query` (Atom only) with the registry User-Agent. Each request waits at least three seconds. Records keep the arXiv id, version, categories, and dates. A new version is a new row; the older provenance row stays. PDF, source, and HTML links in the feed are not requested. `max_results` is capped at 2000. A `totalResults` above 30000 is policy_blocked and stores nothing. 429 waits for Retry-After. A timeout is retryable_error and does not advance the watermark.
- Fixtures under `tests/fixtures/arxiv/`. No live HTTP.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.67ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
88 files already formatted

$ uv run mypy
Success: no issues found in 57 source files

$ uv run pytest -q
........................................................................ [ 56%]
.......................................................                  [100%]
127 passed in 6.76s
```

## CAfix Cursor commit after artifacts
- `src/techpulse/collectors/base.py`: `collect_pages` keeps each page in one transaction and commits the cursor only after `raw.jsonl`, `manifest.json`, and `metrics.json` are written and fsynced. If that write fails, the transaction is rolled back, so the cursor and seen rows stay at their previous values.
- `tests/integration/test_artifact_cursor.py`: an `os.fsync` failure after both OpenAlex pages are fetched leaves the cursor unset, and the rerun returns the same records as a clean collect. Crossref and arXiv use this same `collect_pages` path.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.77ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
90 files already formatted

$ uv run mypy
Success: no issues found in 58 source files

$ uv run pytest -q
........................................................................ [ 56%]
........................................................                 [100%]
128 passed in 4.39s
```

## C20 GitHub read-only collector
- Added fixture-backed GitHub repository/release metadata collection with stable numeric IDs, idempotent cursors, S20 GET-only egress, registry pacing and headers, optional environment-only token authentication, secondary-limit circuit breaking, and Retry-After handling. Repository and release instructions are never retrieved for execution.
- Verified `uv run pytest tests/integration/test_github.py -q` → `6 passed in 1.15s`; full final gates are recorded under C21. Also corrected pre-existing unparameterized `dict` annotations in `test_source_registry.py` required by strict mypy.

## C21 Hacker News and compliant RSS collectors
- Added HN public item metadata collection and reviewed per-feed RSS/Atom metadata collection through S20. Records validate against the P10 schema; usernames, contacts, and full text are discarded. RSS remains disabled in the shipped phase-2 registry and unknown feeds are policy-blocked before HTTP.
- Real final output: `uv lock --check` → `Resolved 88 packages in 0.58ms`; `uv run ruff check .` → `All checks passed!`; `uv run ruff format --check .` → `76 files already formatted`; `uv run mypy` → `Success: no issues found in 55 source files`; `uv run pytest -q` → `97 passed in 3.27s`.

## CBfix Collector review fixes
- GitHub authentication forwards the environment token without logging it; RSS cursors now use monotonic `(published_at, id)` keys and replay tests; HN and RSS accept numeric and HTTP-date `Retry-After` values.
- Real final output: `uv lock --check` → `Resolved 88 packages in 0.79ms`; `uv run ruff check .` → `All checks passed!`; `uv run ruff format --check .` → `78 files already formatted`; `uv run mypy` → `Success: no issues found in 55 source files`; `uv run pytest -q` → `102 passed in 3.72s`.

## D10 Raw archive and lineage
- `src/techpulse/workflows/activities.py`: stages collector NDJSON through an `ObjectStore`. `LocalObjectStore` writes under the artifacts root (no S3 here). Each line is checked against `contracts/raw_record.schema.json` and the file SHA-256. A declared hash mismatch is `fatal_error` and is not staged. An object-store `OSError` is `retryable_error`. Schema, JSON, and invalid UTF-8 lines go to `dlq.ndjson` and `raw_dlq`. `idempotency_key` is sha256 of `run_id|source_id|source_record_id|content_sha256`, so a replay of the same run inserts no new `raw_records` rows. SQLite indexes `raw_uri`, `source_id`, `collected_at`, `published_at`, and `policy_snapshot_id`. Each row's `raw_uri` points at the staged object line. The archive watermark moves only when the DLQ is empty and the commit succeeds. Response headers named like authorization, cookie, token, or secret are dropped; other values go through `redact_secrets`.
- Tests use `tests/fixtures/openalex/one_work.json`, `tests/fixtures/crossref/one_work.json`, `tests/fixtures/github/{repository,releases}.json`, and `tests/fixtures/hn/item.json` through the collectors. No live HTTP. SQLite and the object root are `tempfile` directories.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.60ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
98 files already formatted

$ uv run mypy
Success: no issues found in 64 source files

$ uv run pytest -q
........................................................................ [ 48%]
........................................................................ [ 97%]
....                                                                     [100%]
148 passed in 6.33s
```

## D20 Cleaning and licence marking
- `src/techpulse/normalize/clean.py`: strips HTML, control characters, and invalid Unicode offline, normalises timestamps to UTC `Z` and language to a BCP-47 primary subtag (else `und`). `metadata_only` keeps no body, `reusable_excerpt` keeps at most 280 characters, `fulltext_allowed` keeps the cleaned text. `blocked` and `unknown` bodies are withheld and are not written to the clean file or the quarantine file. Bad rows go to `quarantine.jsonl` with `extraction_failure_reason`. A run is `fatal_error` when extractable-body success is below 97% or required-field completeness is below 98%. A declared SHA-256 mismatch is fatal and writes no clean file. pyarrow is not installed, so the clean output is `clean.jsonl` and the manifest note is `pyarrow is not installed; wrote clean.jsonl instead of clean.parquet`.
- `clean_from_manifest` reads a D10 raw manifest and cleans only those lines. Tests use the OpenAlex, Crossref, GitHub, and Hacker News collector fixtures. No live HTTP. Outputs are under a `tempfile` directory.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.68ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
99 files already formatted

$ uv run mypy
Success: no issues found in 65 source files

$ uv run pytest -q
........................................................................ [ 47%]
........................................................................ [ 94%]
.........                                                                [100%]
153 passed in 8.06s
```
## Q20 Red team: prompt-injection and permission suite
- Added 160 frozen 10.2 red-team cases under `tests/redteam/cases/` (readme, issue, rss, pdf_text, paper_abstract families; tool_call, shell, exfiltration, policy_change, obfuscation payloads, each well under 100 KB) and `tests/redteam/test_injection.py`, which runs every case through the S20 guards (`ingest_untrusted` fencing, `perform_tool_call` authorization, `check_egress` allowlist, `write_audit_log`/`redact_secrets` redaction) and asserts zero unauthorised tool calls, zero non-allowlisted egress, zero secret leakage in outputs/logs. No S20 guard code was modified; no breakouts found, so no xfail entries were needed.
- Real final output: `uv lock --check` → `Resolved 88 packages in 0.70ms`; `uv run ruff check .` → `All checks passed!`; `uv run ruff format --check .` → `86 files already formatted`; `uv run mypy` → `Success: no issues found in 61 source files`; `uv run pytest -q` → `496 passed in 7.24s`.

## QBfix Red-team review fixes
- Per-case egress checks now extract every URL/host from each red-team fixture (src/techpulse/redteam.py harness) and replay them through check_egress, replacing the hard-coded URL list; added scripts/run_redteam.py writing artifacts/<run_id>/Q20/{redteam,manifest,metrics}.json (real run: 160/160 cases pass, 1330 egress checks blocked, 330 forbidden tool calls denied, 0 secrets leaked) and docs/security_redteam.md. No S20 guard code modified.
- Real final output: `uv lock --check` -> `Resolved 88 packages in 0.85ms`; `uv run ruff check .` -> `All checks passed!`; `uv run ruff format --check .` -> `91 files already formatted`; `uv run mypy` -> `Success: no issues found in 62 source files`; `uv run pytest -q` -> `651 passed in 8.19s`.

## D21 PII redaction and deletion map
- `src/techpulse/governance/pii.py`: replaces phone, email, id-document, token, and password spans with `[REDACTED]` and does not keep the matched text. Contact fields (email, phone, username, password, token, and the same) are dropped. Dedup names (`author`, `authors`, `name`, `display_name`, `full_name`) are stored only as HMAC-SHA256 in `name_hmacs`. The key is `TECHPULSE_PII_HMAC_KEY` from the process environment. The module does not open a `.env` file. Embedding input is the redacted title and excerpt. If a pattern is still present, the run is `fatal_error`, the text is not stored, `ops_notice.json` is written, and `run_guard.index_isolated` is set.
- SQLite table `deletion_map` records `source_id`, `source_record_id`, `chunk_id`, `chunk_uri`, `vector_id`, `vector_uri`, `report_id`, `report_uri`. `locate_derived` returns every row for a source id or source record id. Chunk and vector bytes go through the D10 `ObjectStore`. `LocalObjectStore` writes under the artifacts root. There is no S3 client and no Postgres connection. pyarrow is not installed, so the sanitized file is `sanitized.jsonl` and the manifest note says so.
- `docs/deletion_map.md` describes the chain. Tests use `tempfile` directories, `tests/fixtures/fake_secret_sample.txt`, and the email in `tests/fixtures/github/repository.json`. No live HTTP.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.71ms
## D30 Cross-source deduplication and near-duplicate merge
- `src/techpulse/normalize/dedup.py`: normalises DOI, arXiv id, and GitHub repo id (stable numeric id before owner/name), then the source-local record id. Text matching uses a title fingerprint and rapidfuzz token-sort ratio at 95. Conflicting external ids are never forced together; they stay separate with `unresolved=true` and an `unresolved` candidate. Every input row is kept, with `canonical_id` and the sorted `source_record_ids` needed to reverse a merge. `run_dedup` writes `artifacts/{run_id}/D30/{dedup.jsonl,manifest.json,metrics.json}` through the `ObjectStore` interface; pyarrow is absent, so the output is JSONL with a manifest note.
- `data/gold/dedup_gold.jsonl` has 12 hand-labelled rows. `tests/unit/test_dedup.py` measures pairwise precision/recall against it without hard-coding the score; the real result is `precision=1.0000 recall=1.0000 TP=4 FP=0 FN=0`. Other tests cover identifier priority, reversible merging, source-id fallback, conflicting-id unresolved handling, and artifacts. No network, no clean.py/PII changes.
- Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.61ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
102 files already formatted

$ uv run mypy
Success: no issues found in 66 source files

$ uv run pytest -q
........................................................................ [ 45%]
........................................................................ [ 91%]
..............                                                           [100%]
158 passed in 8.30s
101 files already formatted

$ uv run mypy
Success: no issues found in 69 source files

$ uv run pytest -q
676 passed in 10.34s
```

## D40 Entity resolution and cross-source relations
- `src/techpulse/normalize/entities.py` resolves canonical D30 rows into paper, repository, company, and technology-topic entities, then adds relations with `source_record_id`, all supporting `source_record_ids`, the source field, evidence text, and a high/medium/low confidence. Explicit DOI, arXiv, and GitHub identifiers and a small topic dictionary drive the links; repository rename keeps the stable numeric repo id as canonical while old and new names become aliases. Companies with the same name but different domains stay separate, and name-only companies are per-provenance `unresolved` instead of being force-merged. An `owned_by` conflict keeps both relations, marks them `unresolved`, and lowers them to low confidence. `run_entities` writes `artifacts/{run_id}/D40/{entities.jsonl,relations.jsonl,manifest.json,metrics.json}` through the `ObjectStore` interface; pyarrow is absent, so the output is JSONL with a manifest note. No clean.py/PII changes; no network.
- `tests/unit/test_entities.py` covers paper-repo-company provenance, repository rename aliases, same-name companies with different domains, ownership conflicts, name-only unresolved entities and unknown topics, and the artifacts/metrics. Every relation in the fixtures has a non-empty `source_record_id`.
- Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.70ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
101 files already formatted

$ uv run mypy
Success: no issues found in 69 source files

$ uv run pytest -q
676 passed in 9.69s
```

## I30 Retrieval evaluation gold set
- `data/gold/retrieval/` is frozen with a 32-document corpus and 40 graded queries (20 English, 20 Chinese) across AI Agents, RAG, vector databases, and developer tools. The split is 24 train / 8 dev / 8 test. The corpus is local and derived from the collector fixtures; no live API was called. The sealed test split was not run.
- `src/techpulse/evaluation/retrieval_metrics.py` implements nDCG@10 (gain `2**rel - 1`, log2 discount, ideal DCG from gold grades) and Recall@20 (grade > 0). `evaluate(retriever, gold)` accepts any `query -> ranked doc ids` callable and returns per-query plus overall, per-language, and per-topic macro averages. Thresholds are 0.75 and 0.85.
- `tests/evaluation/test_retrieval_metrics.py` covers tiny hand-computed nDCG/Recall examples, duplicate-hit handling, aggregation, tempfile JSONL loading, invalid-grade rejection, and the frozen gold set's topic/language/split coverage. The real index was not evaluated.
- Freeze record is in `docs/decisions/i30_gold_set.md`: `corpus.jsonl` 32 lines, `queries.jsonl` 40 lines, no hashing scheme.
- Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.89ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
109 files already formatted

$ uv run mypy
Success: no issues found in 73 source files

$ uv run pytest -q
689 passed in 11.80s
```

## I10 Embedding and index versioning
- `src/techpulse/index/embed.py`: `Embedder` with `HashedNgramEmbedder` (deterministic hashed character n-gram TF, used by tests, no downloaded weights) and `OpenAICompatibleEmbedder` (base URL from `TECHPULSE_EMBED_BASE_URL`, key read from the env var named by `TECHPULSE_EMBED_API_KEY_ENV`). The remote path stays off unless `TECHPULSE_EMBED_REMOTE` is set, and tests never call it. This module does not open a `.env` file.
- Vectors and the index-version manifest are stored in SQLite. `migrations/0002_search_indexes.sql` is the PostgreSQL 16 + pgvector target (`vector(256)`, HNSW cosine, GIN text). This host has no PostgreSQL, so EXPLAIN was not run. Alembic applies `0002_search_indexes.sqlite.sql` here and stores embeddings as float32 BLOBs. Rows are written to `chunk_vectors_staging`, copied, then the `published` pointer moves in one transaction.
- Inputs are D21 sanitized rows when that run succeeded and is not isolated, otherwise D20. Secrets, HTML or control characters, and blocked or unknown licences are not indexed. The same run and corpus does not insert again. An embedder failure writes a text-only version, marks it degraded, and leaves the published pointer unchanged. Vector objects go through `ObjectStore`; `LocalObjectStore` writes under the artifacts root. No S3 client.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.60ms

$ uv run ruff check .
Resolved 88 packages in 0.66ms
All checks passed!

$ uv run ruff format --check .
109 files already formatted

$ uv run mypy
Success: no issues found in 69 source files

$ uv run pytest -q
........................................................................ [ 10%]
........................................................................ [ 21%]
........................................................................ [ 31%]
........................................................................ [ 42%]
........................................................................ [ 53%]
........................................................................ [ 63%]
........................................................................ [ 74%]
........................................................................ [ 85%]
........................................................................ [ 95%]
.............................                                            [100%]
677 passed in 11.74s
```

## I20 Dense and BM25 hybrid retrieval
- `src/techpulse/index/bm25.py`: pure-Python Okapi BM25 (k1 1.5, b 0.75) with ASCII tokens plus CJK unigrams and bigrams. No extra dependency.
- `src/techpulse/index/rerank.py`: `Reranker` plus `OverlapReranker`, a deterministic token-overlap baseline. No network call.
- `src/techpulse/index/hybrid.py`: dense top-100 and BM25 top-100, RRF (k 60) top-50, rerank top-10. Cuts shrink when the corpus is smaller. If vectors are unavailable, retrieval uses BM25 only and sets `retrieval_degraded`. nDCG@10 and Recall@20 are computed from `system_ranking`.
- Evaluation uses `tests/fixtures/retrieval/labelled.jsonl` (hand-labelled, under 100 KB). The sealed I30 test set was not run. `mvp_threshold_met` is the comparison of those measured fixture means with 0.75 and 0.85. It is not a sealed-test or production claim.

Fixture measurement (`uv run python` on the labelled file, hashed embedder, tempfile, no third-party call):

```text
status success
retrieval_degraded False
high_confidence_blocked False
mvp_threshold_met True
ndcg_at_10 {"en": 1.0, "zh": 1.0}
recall_at_20 {"en": 1.0, "zh": 1.0}
p95_latency_ms 0.6924999906914309
cost_usd 0.0
eval_set hand_labelled_fixture
q-en-bm25 en ndcg 1.0 recall 1.0 degraded False dense 24 bm25 3 fused 24 reranked 10
q-en-vec en ndcg 1.0 recall 1.0 degraded False dense 24 bm25 3 fused 24 reranked 10
q-en-rrf en ndcg 1.0 recall 1.0 degraded False dense 24 bm25 2 fused 24 reranked 10
q-en-pii en ndcg 1.0 recall 1.0 degraded False dense 24 bm25 2 fused 24 reranked 10
q-zh-hyb zh ndcg 1.0 recall 1.0 degraded False dense 24 bm25 8 fused 24 reranked 10
q-zh-idx zh ndcg 1.0 recall 1.0 degraded False dense 24 bm25 8 fused 24 reranked 10
q-zh-lic zh ndcg 1.0 recall 1.0 degraded False dense 24 bm25 3 fused 24 reranked 10
q-zh-emb zh ndcg 1.0 recall 1.0 degraded False dense 24 bm25 5 fused 24 reranked 10
```

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.62ms

$ uv run ruff check .
Resolved 88 packages in 0.63ms
All checks passed!

$ uv run ruff format --check .
110 files already formatted

$ uv run mypy
Success: no issues found in 70 source files

$ uv run pytest -q
........................................................................ [ 10%]
........................................................................ [ 21%]
........................................................................ [ 31%]
........................................................................ [ 42%]
........................................................................ [ 52%]
........................................................................ [ 63%]
........................................................................ [ 73%]
........................................................................ [ 84%]
........................................................................ [ 94%]
....................................                                     [100%]
684 passed in 11.68s
```

## IAfix I10 review fixes
- `build_index_from_run` indexes only a successful D21 sanitized file. If that manifest is absent, or its status is not `success`, the I10 manifest is `policy_blocked` with a clear error and no index is written. D20 clean rows are not used. The SQLite runtime and the pgvector DDL are unchanged.
- `OpenAICompatibleEmbedder.embed` turns an HTTP 200 with invalid JSON into `EmbedderError`. A decoded payload of the wrong shape already raises `EmbedderError`. `build_index` then writes the text-only degraded version and leaves the published pointer unchanged. The new test uses `httpx.MockTransport`. No network call.

Real check output:

```text
$ uv run ruff format src/techpulse/index/embed.py tests/integration/test_i10_embed.py
2 files left unchanged

$ uv run ruff check src/techpulse/index/embed.py tests/integration/test_i10_embed.py
All checks passed!

$ uv run ruff format --check src/techpulse/index/embed.py tests/integration/test_i10_embed.py
2 files already formatted

$ uv run mypy src/techpulse/index/embed.py tests/integration/test_i10_embed.py
Success: no issues found in 2 source files

$ uv run pytest -q tests/integration/test_i10_embed.py tests/evaluation/test_retrieval.py
...................                                                      [100%]
19 passed in 1.98s
## G10-prep Compliant sample template and interview guide
- Added `growth/sample_template.md`, an evidence-complete report skeleton with topic, trend score, confidence, claim/evidence and placeholder source URL fields, a counter-evidence section, `as_of`, the `AI 生成` label, and `q10_status: PENDING`; every value is an obvious `<PLACEHOLDER>` and no fact, number or URL is invented.
- Added `growth/interview_guide.md` (Chinese): consent wording, workflow/budget/alternatives script, anonymised feedback form, and CRM field list with no names, emails, phones or other personal contact data. Drafts only; no third party is contacted; sending invitations requires AUTH-06.
- Added `growth/README.md` explaining that `sample_1.md`/`sample_2.md` are produced only after R10+Q10 PASS and that unpublished or QA-failed samples must not be shown externally.
- Added `tests/growth/test_growth_docs.py` covering file existence, UTF-8 decoding, the AUTH-06 notice, absence of email/phone patterns, required template fields (`Q10`, `as_of`, `AI 生成`), and placeholder-only http(s) URLs.
- Real check output:

```text
$ uv run python -m pytest -q tests/growth
........                                                                 [100%]
8 passed in 0.02s

$ uv run python -m ruff check tests/growth .
All checks passed!
```

## G10fix Remove invented timings
- `growth/interview_guide.md` no longer states an interview length, a purchase lookback window, or a coding deadline that the plan does not specify. These are now `<INTERVIEW_DURATION>`, `<PURCHASE_LOOKBACK_WINDOW>`, and `<CODING_DEADLINE>`.
- The remaining numbers in `growth/` trace to the plan or project spec: the `$99/$199/$299` experimental price anchors, the `30/90` day horizon, `sample_1.md`/`sample_2.md`, the `3.1` target-customer section, and task/gate ids such as `G10`, `R10`, `Q10`, and `AUTH-06`.
- Added `test_interview_guide_has_no_duration_figures` in `tests/growth/test_growth_docs.py`; it rejects any `<number> 分钟` duration figure in the guide.
- Real check output:

```text
$ python -m pytest -q tests/growth
.........                                                                [100%]
9 passed, 1 warning in 0.03s

$ ruff check tests/growth
All checks passed!
```

## T10 Topic clustering and stable topic ids
- `src/techpulse/trends/cluster.py`: seed aliases assign candidates first. Unseeded records are clustered with average linkage on I10 `HashedNgramEmbedder` vectors (or vectors already produced by that embedder). No model download. Topic id and display name are separate. A stored id is kept when the name changes, and a cluster id is kept when member overlap stays at least half. If at least half of the stored assignments would change, the SQLite dictionary is left unchanged and `cluster_degraded` is set. Fixed seed `0`, join cosine `0.60`.
- Artifacts go through `ObjectStore` (`LocalObjectStore` under the artifacts root). pyarrow is not installed, so the topic table is `topics.jsonl`. The dictionary is SQLite at `{run_id}/T10/topics.sqlite`.
- `tests/unit/test_t10_cluster.py` uses `tempfile`. Near-copy grouping is checked against cosine computed from the baseline vectors in the test.
- `tests/growth/test_growth_docs.py`: one ruff format spacing change in a slice (`match.start() : match.start() + 40`) so `ruff format --check` passes on this tree.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.59ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
122 files already formatted

$ uv run mypy
Success: no issues found in 77 source files

$ uv run pytest -q
........................................................................ [  9%]
........................................................................ [ 19%]
........................................................................ [ 29%]
........................................................................ [ 39%]
........................................................................ [ 49%]
........................................................................ [ 59%]
........................................................................ [ 69%]
........................................................................ [ 79%]
........................................................................ [ 89%]
........................................................................ [ 99%]
......                                                                   [100%]
726 passed in 11.50s
```

## T20 7d/28d windows and growth features
- `src/techpulse/trends/features.py`: 7-day and 28-day windows are UTC calendar days, half-open at midnight. Independent records are deduped by upstream id, canonical id, then content hash. The row stores numerators, denominators, and the record ids behind growth, diversity, quality, novelty, 28-day persistence, and developer activity. Growth is `null` when the previous window has fewer than 3 independent records. Reprint, same-domain, bot-share, and small-base penalties each carry the raw count they used, and the sum is capped at 0.30.
- Artifacts go through `LocalObjectStore`. pyarrow is not installed, so the table is `features.jsonl`. The same rows are inserted into SQLite `trend_feature_snapshots`.
- `tests/unit/test_t20_features.py` derives every expected rate from the fixture's own counts and dates. Timestamps are built in the test. `tempfile` holds the run directory.

Real check output:

```text
$ uv lock --check
Resolved 88 packages in 0.65ms

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
123 files already formatted

$ uv run mypy
Success: no issues found in 78 source files

$ uv run pytest -q
........................................................................ [  9%]
........................................................................ [ 19%]
........................................................................ [ 29%]
........................................................................ [ 39%]
........................................................................ [ 48%]
........................................................................ [ 58%]
........................................................................ [ 68%]
........................................................................ [ 78%]
........................................................................ [ 87%]
........................................................................ [ 97%]
.................                                                        [100%]
737 passed in 11.08s
```
