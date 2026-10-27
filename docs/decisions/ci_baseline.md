# CI baseline (P20)

Decision: run one GitHub Actions `checks` job on `ubuntu-latest` and
`windows-latest` with Python 3.12 and locked dependencies (`uv sync --locked`).
There is no deploy job. This repository has no remote yet, so the workflow is
committed but was only validated locally.

## What each step checks

| Step | Command | Purpose |
|---|---|---|
| Ruff lint | `ruff check .` | Lint and import ordering; fails on any finding |
| Ruff format | `ruff format --check .` | Formatting drift |
| Mypy | `mypy` | Strict type check of `src/techpulse` and `tests` (config in `pyproject.toml`) |
| Pytest | `pytest tests/unit tests/contract -q --cov=src/techpulse --cov-report=term-missing` | Unit and contract tests, with a terminal coverage report |
| Bandit | `bandit -q -r src` | Static security scan of application code |
| pip-audit | `pip-audit` | Known vulnerabilities in installed dependencies, using the PyPI advisory database |
| Secret scan | `detect-secrets-hook --baseline .secrets.baseline --exclude-files 'fake_secret_sample\.txt' $(git -c core.quotePath=false ls-files)` | Fails on secrets not already recorded in the baseline |

Dependency cache is provided by `astral-sh/setup-uv` and keyed on `uv.lock`.
Nothing is deployed and no test-environment secrets are required.

## Run locally

The same steps, in the same order:

```bash
make ci
```

Individual targets:

```bash
make lint
make test
make secrets
```

If `make` is unavailable (for example on the Windows host used for P20), run
the commands directly:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest tests/unit tests/contract -q --cov=src/techpulse --cov-report=term-missing
uv run bandit -q -r src
uv run pip-audit
uv run detect-secrets-hook --baseline .secrets.baseline --exclude-files 'fake_secret_sample\.txt' $(git -c core.quotePath=false ls-files)
```

PowerShell equivalent for the last command:

```powershell
uv run detect-secrets-hook --baseline .secrets.baseline --exclude-files 'fake_secret_sample\.txt' @(git -c core.quotePath=false ls-files)
```

`pip-audit` needs network access to the PyPI advisory database. If it fails
only because the advisory database cannot be reached, record that as a
network-only failure and treat the other steps as the gate.

## Secret baseline and the fake-token fixture

`.secrets.baseline` was generated from git-tracked files. It currently records
one reviewed placeholder finding in `.env.example` (`CHANGE_ME` credentials) so
the repository scan stays clean. The fixture
`tests/fixtures/fake_secret_sample.txt` intentionally contains a fake
`ghp_...` token; it is excluded from the baseline-gating scan with
`--exclude-files`, and `tests/unit/test_secret_scan.py` scans it directly to
prove the scanner still flags it.

After adding or rotating content, refresh and review the baseline:

```bash
uv run detect-secrets scan --exclude-files 'fake_secret_sample\.txt' . > .secrets.baseline
uv run detect-secrets audit .secrets.baseline
uv run detect-secrets-hook --baseline .secrets.baseline --exclude-files 'fake_secret_sample\.txt' $(git -c core.quotePath=false ls-files)
```

Never commit real credentials; inline false positives use
`pragma: allowlist secret` only after review.
