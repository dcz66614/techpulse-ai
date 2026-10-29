# Decision: AUTH-02 Deploy Target Authorization

## Current Decision (Ticket DECbox, 2026-10-29)

- **Decision:** The owner changed AUTH-02. The deploy target is now a separate always-on Linux machine (x86_64, Python via uv, no Docker, no Postgres), NOT the owner's Windows PC. SQLite is the database dialect; local runner consists of uvicorn serving the A10 FastAPI application bound to 127.0.0.1 only and the O20 local orchestrator for background task pipelines. The container image digest remains recorded as `"N/A (local deployment, no container)"`.
- **Reason for Change:** The owner's Windows PC shuts down nightly, which breaks 24-hour continuous operation and unattended soak testing (V99). Moving the deploy target to a dedicated always-on Linux machine (x86_64) ensures uninterrupted background execution and soak verification.
- **Scope limits:** Single-machine self-hosted deployment on the dedicated Linux machine only. Python via uv, SQLite database, local runner (uvicorn bound to 127.0.0.1 only + O20 local orchestrator). No Docker containerization, no Postgres database cluster, no cloud spend ($0.00 cloud spend cap). Cloud targets, Kubernetes clusters, Docker daemon builds or registry pushes, external network listeners, and public internet access remain strictly prohibited and refused by the release gate.
- **Date:** 2026-10-29
- **Decided-by:** Owner / Dispatcher

## Decision History

### Initial Delegated Decision (Ticket DEC, 2026-10-29)
- **Decision:** The deploy target is a local single-machine deployment on the owner's PC. SQLite is the database dialect; local runner consists of uvicorn serving the A10 FastAPI application and the O20 local orchestrator for background task pipelines. No Docker containerization, no Postgres database cluster, no cloud spend ($0.00 cloud spend cap).
- **Rationale:** Single-machine self-hosted deployment on the owner's PC fulfills all functional and verification requirements for local execution while strictly eliminating cloud infrastructure spend, third-party operational dependencies (Docker daemon, container registry, PostgreSQL instance, Temporal cluster), and external network exposure.
- **Superseded because:** The owner's PC shuts down nightly, preventing continuous 24-hour soak runs.
