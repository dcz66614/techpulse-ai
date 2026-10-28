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
