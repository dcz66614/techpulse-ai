# Deployment Guide: Dedicated Linux Machine Deployment (AUTH-02 / AUTH-06)

This document describes the deployment architecture and execution steps for TechPulse-AI. Under decision **AUTH-02** (`docs/decisions/auth02_deploy_target.md`, updated under Ticket DECbox) and **AUTH-06** (`docs/decisions/auth06_local_release.md`), the authorized deployment target is a **separate always-on Linux machine (x86_64, Python via uv, no Docker, no Postgres)**, NOT the owner's Windows PC (which shuts down nightly).

---

## 1. Deployment Architecture & Boundaries

- **Deploy Target:** Single-machine self-hosted deployment on a dedicated always-on Linux box (`x86_64`, `127.0.0.1`).
- **Database Backend:** SQLite (managed via Alembic migrations at `migrations/`). No external PostgreSQL server is required or connected.
- **Application Server:** FastAPI application served via `uvicorn` (`techpulse.api.main:app`) bound strictly to `127.0.0.1`.
- **Workflow Pipeline:** Local task orchestrator (`O20` / `techpulse.workflows.orchestrator.LocalOrchestrator`) executing linear stages with SQLite checkpoints. No external Temporal server is required.
- **Containerization:** None. No Docker daemon is used; no container image is built or pushed. Image digest is recorded honestly as `"N/A (local deployment, no container)"`.
- **Cloud Infrastructure Spend:** $0.00 cloud spend cap. No cloud VMs, managed clusters, or paid services are authorized.
- **Network Boundary:** Egress is strictly controlled per `config/policy.yaml` (default `deny`). The local API binds to `127.0.0.1` only. Public cloud deployment and public outreach stay strictly **REFUSED**.

---

## 2. Prerequisites

1. **Host:** Linux machine (`x86_64`) with always-on availability.
2. **Python:** Python 3.12 or higher.
3. **Package Manager:** `uv` installed and available in PATH.
4. **Environment:** Set `UV_PROJECT_ENVIRONMENT=.venv` (or activate `.venv`).

---

## 3. Deployment Steps

### Step 1: Environment Setup
Sync project dependencies from the locked specification:
```bash
uv sync --frozen
```

### Step 2: Database Migration
Initialize or upgrade the local SQLite database schema to Alembic head revision:
```bash
# Verify schema migration via dry-run on a temporary SQLite instance
uv run python scripts/migration_dry_run.py

# Apply migrations directly to the local SQLite database file
# (Alembic resolves SQLite from DATABASE_URL / TECHPULSE_API_DATABASE / config/deploy.yaml)
uv run alembic upgrade head
# Or apply directly via migration runner:
# uv run python scripts/migration_dry_run.py --database artifacts/app.sqlite
```

### Step 3: Local Smoke Health Verification
Verify that the FastAPI ASGI application starts properly and responds to health probes in-process:
```bash
uv run python scripts/smoke_test.py --database artifacts/app.sqlite
```
Expected output:
- `/health/live`: `200 OK` (schema version `1.0`)
- `/health/ready`: `200 OK` (database and object store status `ok`)

### Step 4: Generate Release Manifest
Generate the immutable release manifest for the local v1.0 deployment:
```bash
uv run python scripts/release_manifest.py --run-id local-v1.0
```
This writes:
- `artifacts/local-v1.0/O10/release_manifest.json`
- `artifacts/local-v1.0/O10/manifest.json`
- `artifacts/local-v1.0/O10/metrics.json`
- `artifacts/local-v1.0/O10/log.jsonl`

The image digest field is recorded honestly as `"N/A (local deployment, no container)"`. Never write a fake digest.

### Step 5: Start the Local API
Launch the local API server using `scripts/run_local.sh`:
```bash
./scripts/run_local.sh
```
This script runs `uv sync --frozen`, creates `artifacts/deploy/`, and starts `uvicorn` bound strictly to `127.0.0.1` only, redirecting all server logs to `artifacts/deploy/uvicorn.log`.

Optional port can be passed as argument (e.g., `./scripts/run_local.sh 8080`), with host strictly retained as `127.0.0.1`.

### Step 6: Verify Local Endpoints
- **Liveness Probe:** `GET http://127.0.0.1:8000/health/live`
- **Readiness Probe:** `GET http://127.0.0.1:8000/health/ready`
- **Interactive OpenAPI Documentation:** `GET http://127.0.0.1:8000/docs`
- **Static Dashboard UI:** `GET http://127.0.0.1:8000/ui` (when `web/src` is mounted)

### Step 7: V99 24-Hour Soak Runner
Launch the 24-hour unattended soak runner in the background using `scripts/v99_soak.sh`:
```bash
nohup ./scripts/v99_soak.sh > artifacts/v99/soak.log 2>&1 &
```
The runner executes the P0 daily flow every N minutes (default 60), logs cycles to `artifacts/v99/cycles.jsonl`, maintains a heartbeat file, and at or after 24h evaluates V99 criteria to write `artifacts/{run_id}/V99/` and `docs/decisions/mvp_acceptance.md`.

To check progress and running tallies without interrupting:
```bash
uv run python scripts/v99_soak.py --status
```

---

## 4. Release Gate & Security Governance

- **AUTH-02 (Deploy Target):** Granted for local single-machine deployment on a separate always-on Linux box (`config/deploy.yaml`). The owner's Windows PC was superseded because it shuts down nightly. Cloud and container deployments remain strictly prohibited.
- **AUTH-06 (Local Release Approval):** Approved for local/self-hosted "v1.0 local release" only. Public internet exposure and outreach stay REFUSED.
- **Container Images and `<PINNED_DIGEST>`:** Docker builds and container registry pushes are not used; `<PINNED_DIGEST>` in the Dockerfile remains an unpinned placeholder because no container is built. The image digest stays `"N/A (local deployment, no container)"`.
- **V99 Soak Testing:** Evaluated after a continuous 24-hour run on the dedicated always-on Linux box. Before 24h elapsed wall-clock time, PASS is strictly forbidden.
