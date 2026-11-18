# TechPulse-AI / 知潮

TechPulse-AI (知潮) is a local, self-hosted tech-intelligence pipeline. It collects
technology signals from approved sources (OpenAlex, Crossref, arXiv, GitHub, Hacker
News), normalises and deduplicates them, indexes them for bilingual (en/zh) retrieval,
and renders traceable trend reports. Everything runs through the O20 local orchestrator
with SQLite and a local file object store, served by a FastAPI app bound to `127.0.0.1`.

> **This is NOT a final-acceptance-passed release.** It is a development build with
> documented known issues. See [Acceptance status / known limitations](#acceptance-status--known-limitations).

## Run locally (Windows)

Python 3.12 is required. Use `uv` to manage the virtual environment:

```powershell
uv sync --frozen
$env:UV_PROJECT_ENVIRONMENT = ".venv"
$env:PYTHONPATH = "src"
```

Set `PYTHONPATH=src` so the `techpulse` package under `src/` is importable during
development. Start the read-only API on `127.0.0.1`:

```powershell
uv run uvicorn techpulse.api.main:app --host 127.0.0.1 --port 8000
# or: .\scripts\run_local.ps1
```

Then check the health endpoints:

- `GET http://127.0.0.1:8000/health/live`
- `GET http://127.0.0.1:8000/health/ready`
- `GET http://127.0.0.1:8000/docs` (interactive OpenAPI)
- `GET http://127.0.0.1:8000/ui` (static dashboard, when `web/src` is mounted)

Apply SQLite migrations with `uv run alembic upgrade head`, and verify the schema with
`uv run python scripts/migration_dry_run.py`. See [docs/deploy.md](docs/deploy.md) for the
full local deployment guide and [docs/runbook.md](docs/runbook.md) for operational
procedures.

## Run tests

From the repository root:

```powershell
$env:PYTHONPATH = "src"
uv run pytest
```

The default suite excludes the `capacity` marker (a 10k-record replay). Lint and type
checks: `uv run ruff check .` and `uv run mypy`.

## Repository layout

- `src/techpulse/` — application code, grouped by stage: `collectors/`, `normalize/`,
  `index/`, `trends/`, `evidence/`, `reports/`, `qa/`, `api/`, `workflows/` (O20 local
  orchestrator), `ops/`, `evaluation/`, `governance/`, `release/`.
- `tests/` — unit, contract, integration, evaluation, growth and red-team tests;
  `web/tests/` covers the static UI.
- `docs/` — deployment guide, runbook, architecture, specs, operational docs,
  `docs/decisions/` (delegated decisions and known limits) and `docs/acceptance/`
  (final-review package, verdict and archived evidence).
- `config/` — deployment, policy, model-router and source-registry YAML.
- `scripts/` — local runners, smoke test, migration dry-run, release manifest, V99 soak
  runner and backup/restore helpers.
- `data/`, `migrations/`, `web/` — sample data, Alembic migrations and the static UI.

## Acceptance status / known limitations

| Item | Status | Note |
| --- | --- | --- |
| GPT final review | NO-GO (2026-11-18) | 5 open reasons; see `docs/acceptance/GPT_FINAL_REVIEW_VERDICT.md`. |
| V99 24h soak | FAIL 23/24, owner-accepted, re-run waived | `v99pc1` on the Windows PC: 95.83% < 99% (one transient arxiv `retryable_error` in cycle #21). Cd accepted the risk and waived a re-run on 2026-11-18; the verdict stays FAIL. |
| Held-out Recall@20 0.8182 < 0.85 | owner-waived | Re-measured on `f41d756`; nDCG@10 0.8954 ok. RETfix found no sound improvement; `docs/decisions/heldout_recall_known_limit.md`. |
| AUTH-02 deploy target | fixed (Windows PC) | Corrected 2026-11-18 to the environment validated by smoke + V99; the separate Linux target was dropped. |
| v99box3 duplicate-after-resume | fixed (regression-tested, no new 24h run) | Root cause: `SoakRunner` kept one in-memory record list per run_id; when a collector timed out after the 6.5h host suspension and was retried, records of both attempts were archived by D10, so the same `(source_id, source_record_id)` (github 1362490) appeared twice and the orchestrator counted 1 duplicate. Fix (`src/techpulse/ops/soak.py`): per-task record buffer with an attempt guard, successful retry fully replaces earlier attempt, D10 dedupe on `(source_id, source_record_id)`, per-task records persisted to disk for restart/adopt recovery, saved `last_due` used for the next slot. 950 tests incl. new regression tests; not re-validated by a new 24h run (waived). Detail: `docs/acceptance/GPT_FINAL_REVIEW_VERDICT.md`. |
| README stale | fixed | This file now reflects the real state. |
| Evidence archive | fixed | Tracked under `docs/acceptance/` (`.orchestra/` and `artifacts/` are git-ignored). |

This is a development build, not a final-acceptance-passed release. Deploy and release
boundaries stay local/self-hosted only; public deployment and outreach remain refused.
