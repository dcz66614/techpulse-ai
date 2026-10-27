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
=============================== tests coverage ================================
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
