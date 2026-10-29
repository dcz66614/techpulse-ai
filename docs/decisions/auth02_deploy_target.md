# Decision: AUTH-02 Deploy Target Authorization

- **Decision:** The deploy target is a local single-machine deployment on the owner's PC. SQLite is the database dialect; local runner consists of uvicorn serving the A10 FastAPI application and the O20 local orchestrator for background task pipelines. No Docker containerization, no Postgres database cluster, no cloud spend ($0.00 cloud spend cap).
- **Rationale:** Single-machine self-hosted deployment on the owner's PC fulfills all functional and verification requirements for local execution while strictly eliminating cloud infrastructure spend, third-party operational dependencies (Docker daemon, container registry, PostgreSQL instance, Temporal cluster), and external network exposure.
- **Scope limits:** Local single-machine deployment on the owner's PC only. Cloud targets, Kubernetes clusters, Docker daemon builds or registry pushes, external network listeners, and public internet access remain strictly prohibited and refused by the release gate.
- **Date:** 2026-10-29
- **Decided-by:** dispatcher, delegated by Cd
