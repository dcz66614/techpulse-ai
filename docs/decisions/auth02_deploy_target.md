# Decision: AUTH-02 Deploy Target Authorization

## Current Decision (2026-11-18)

- **Decision:** The deploy target is the owner's Windows PC. This is the environment that was actually validated by the smoke test and the 24-hour V99 soak run (v99pc1): scheduled tasks running a local `uvicorn` bound to `127.0.0.1` plus the soak runner, SQLite as the database, and Python via `uv`/venv. There is no Docker and no Postgres. It was always-on for the duration of the validated run.
- **Reason for Change:** The separate Linux target was never validated. The two box runs (v99box3, v99box4) failed because the Linux box suspends when idle, and the validated soak ran on the owner's PC. On 2026-11-18 the owner dropped the separate Linux target and confirmed the Windows PC as the deploy target.
- **Scope limits:** Single-machine self-hosted deployment on the owner's PC only. Python via `uv`/venv, SQLite database, local runner (`uvicorn` bound to `127.0.0.1` only + O20 local orchestrator). No Docker containerization, no Postgres database cluster, no cloud spend ($0.00 cloud spend cap). Cloud targets, Kubernetes clusters, Docker daemon builds or registry pushes, external network listeners, and public internet access remain strictly prohibited and refused by the release gate.
- **Date:** 2026-11-18
- **Decided-by:** Owner (Cd)

## Decision History

### Superseded: Linux Box Deploy Target (Ticket DECbox, 2026-10-29)

- **Decision:** The deploy target was changed to a separate always-on Linux machine (x86_64, Python via uv, no Docker, no Postgres), NOT the owner's Windows PC. SQLite was the database dialect; local runner consisted of uvicorn serving the A10 FastAPI application bound to 127.0.0.1 only and the O20 local orchestrator for background task pipelines. The container image digest remained recorded as `"N/A (local deployment, no container)"`.
- **Reason for Change:** At the time, the owner's Windows PC was believed to shut down nightly, which breaks 24-hour continuous operation and unattended soak testing (V99). Moving the deploy target to a dedicated always-on Linux machine (x86_64) was intended to ensure uninterrupted background execution and soak verification.
- **Scope limits:** Single-machine self-hosted deployment on the dedicated Linux machine only. Python via uv, SQLite database, local runner (uvicorn bound to 127.0.0.1 only + O20 local orchestrator). No Docker containerization, no Postgres database cluster, no cloud spend ($0.00 cloud spend cap). Cloud targets, Kubernetes clusters, Docker daemon builds or registry pushes, external network listeners, and public internet access remain strictly prohibited and refused by the release gate.
- **Superseded because:** The separate Linux target was never validated; box runs v99box3 and v99box4 failed from box suspension, and the validated 24h soak ran on the owner's PC. Dropped by owner decision 2026-11-18.

### Initial Delegated Decision (Ticket DEC, 2026-10-29)
- **Decision:** The deploy target is a local single-machine deployment on the owner's PC. SQLite is the database dialect; local runner consists of uvicorn serving the A10 FastAPI application and the O20 local orchestrator for background task pipelines. No Docker containerization, no Postgres database cluster, no cloud spend ($0.00 cloud spend cap).
- **Rationale:** Single-machine self-hosted deployment on the owner's PC fulfills all functional and verification requirements for local execution while strictly eliminating cloud infrastructure spend, third-party operational dependencies (Docker daemon, container registry, PostgreSQL instance, Temporal cluster), and external network exposure.
- **Superseded because:** The owner's PC was believed to shut down nightly, preventing continuous 24-hour soak runs. This assumption was later revised by the owner on 2026-11-18.
