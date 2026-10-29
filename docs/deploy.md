# Deployment Guide: Local Single-Machine Deployment (AUTH-02 / AUTH-06)

This document describes the deployment architecture and execution steps for TechPulse-AI. Under decisions **AUTH-02** (`docs/decisions/auth02_deploy_target.md`) and **AUTH-06** (`docs/decisions/auth06_local_release.md`), delegated by Cd on 2026-10-29 (Ticket DEC), the authorized deployment target is a **local single-machine deployment on the owner's PC**.

---

## 1. Deployment Architecture & Boundaries

- **Deploy Target:** Local single-machine deployment on the owner's PC (`127.0.0.1`).
- **Database Backend:** SQLite (managed via Alembic migrations at `migrations/`). No external PostgreSQL server is required or connected.
- **Application Server:** FastAPI application served via `uvicorn` (`techpulse.api.main:app`).
- **Workflow Pipeline:** Local task orchestrator (`O20` / `techpulse.workflows.orchestrator.LocalOrchestrator`) executing linear stages with SQLite checkpoints. No external Temporal server is required.
- **Containerization:** None. No Docker daemon is used; no container image is built or pushed. Image digest is recorded honestly as `"N/A (local deployment, no container)"`.
- **Cloud Infrastructure Spend:** $0.00 cloud spend cap. No cloud VMs, managed clusters, or paid services are authorized.
- **Network Boundary:** Egress is strictly controlled per `config/policy.yaml` (default `deny`). The local API binds to `127.0.0.1` only. Public cloud deployment and public outreach stay strictly **REFUSED**.

---

## 2. Prerequisites

1. **Python:** Python 3.12 or higher.
2. **Package Manager:** `uv` installed and available in PATH.
3. **Environment:** Set `UV_PROJECT_ENVIRONMENT=.venv` (or activate `.venv`).

---

## 3. Local Deployment Steps

### Step 1: Environment Setup
Sync project dependencies from the locked specification:
```bash
uv sync --frozen
```

### Step 2: Database Migration
Initialize or upgrade the local SQLite database schema to Alembic head revision:
```bash
# Verify schema migration via dry-run on a temporary SQLite instance
uv run python scripts/migration_dry_run.py --database artifacts/app.sqlite

# Or apply migrations directly to the local SQLite database file
uv run alembic upgrade head
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
Launch the local API server using the provided start scripts:

**On Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_local.ps1
```

**On Linux / macOS (Bash):**
```bash
./scripts/run_local.sh
```

By default, the server listens at `http://127.0.0.1:8000`. Custom host and port can be passed as arguments:
```powershell
powershell -File scripts/run_local.ps1 -HostAddress 127.0.0.1 -Port 8080
```

### Step 6: Verify Local Endpoints
- **Liveness Probe:** `GET http://127.0.0.1:8000/health/live`
- **Readiness Probe:** `GET http://127.0.0.1:8000/health/ready`
- **Interactive OpenAPI Documentation:** `GET http://127.0.0.1:8000/docs`
- **Static Dashboard UI:** `GET http://127.0.0.1:8000/ui` (when `web/src` is mounted)

---

## 4. Release Gate & Security Governance

- **AUTH-02 (Local Deploy Target):** Granted for local single-machine target only (`config/deploy.yaml`). Cloud and container deployments are prohibited.
- **AUTH-06 (Local Release Approval):** Approved for local/self-hosted "v1.0 local release" only. Public internet exposure and outreach stay REFUSED.
- **Container Images and `<PINNED_DIGEST>`:** Docker builds and container registry pushes are not used for local deployment; `<PINNED_DIGEST>` in the Dockerfile remains an unpinned placeholder because no container is built. Cloud deployment was previously blocked on AUTH-02; under Ticket DEC, AUTH-02 grants local single-machine deployment only.
- **V99 Soak Testing:** 24-hour soak testing remains GATED pending a continuous run in the staging environment. Do not claim V99 until executed.
