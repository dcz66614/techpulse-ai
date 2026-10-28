# Deploy

O10 prepared a release on this worktree and did not deploy it. AUTH-02, an approved deployment environment, is pending. `config/deploy.yaml` sets `auth02.granted` to false. A missing file or any value other than boolean true is the same: not granted. That scope is staging-only.

The release gate refuses a deploy unless Q10 `qa_pass` is true and AUTH-02 is granted. With the default config it refuses production. It also refuses a staging deploy. Staging templates, the local smoke check, and the SQLite migration dry-run are preparation, not an apply.

No Docker daemon was used. No image was built or pushed. No registry was contacted. The sealed retrieval test set was not run.

## What was prepared

- `Dockerfile` uses `python:3.12-slim@sha256:<PINNED_DIGEST>`, copies `uv.lock`, and runs as user `techpulse` (uid 10001). The digest is a placeholder. No digest was invented.
- `infra/staging/` holds namespace, deployment, and service templates. The image is `pending-auth-02.invalid/techpulse@pending-AUTH-02`. The service is ClusterIP. There is no Ingress.
- `scripts/release_manifest.py` writes `artifacts/{run_id}/O10/release_manifest.json`, plus `manifest.json`, `metrics.json`, and `log.jsonl`. The image digest field is `pending-AUTH-02`. The manifest records the git commit, model version `none`, policy version `policy-2026-10-27`, schema version `1.0`, and the Alembic revision from the dry-run.
- `scripts/smoke_test.py` starts the A10 app through FastAPI `TestClient` and checks `/health/live` and `/health/ready`. It does not bind a port.
- `scripts/migration_dry_run.py` applies Alembic head to a temporary SQLite file. It does not open the PostgreSQL URL in `alembic.ini`.

## Still blocked on AUTH-02

1. Replace `<PINNED_DIGEST>` in the Dockerfile. After AUTH-02, on a machine allowed to reach the registry, run `docker buildx imagetools inspect python:3.12-slim` and paste the 64 hex characters that follow `sha256:`. Do not invent a digest. Pin the uv image the same way before `uv sync --frozen --no-dev`.
2. Build the image. This machine has no Docker, and the digest is unfilled.
3. Push the image, or pull it from a registry.
4. Apply `infra/staging/`. Do not run `kubectl apply`.
5. Choose a staging cluster, a cloud resource cap, and a domain. Those are the AUTH-02 grant. Domain use is not in the templates: there is no Ingress.
6. Promote to production. The gate refuses production while `auth02.granted` is false, and it also refuses production when Q10 `qa_pass` is not true.
7. Run migrations against PostgreSQL. The only migration executed here is the SQLite dry-run on a temporary file.
8. Install dependencies inside the image. That `RUN` is commented in the Dockerfile.

Set `auth02.granted` to boolean true only after the user grants AUTH-02. A passing gate still does not build or deploy by itself. The manifest writer in this ticket keeps the digest at `pending-AUTH-02` and `deployed` at false.

There is no previous image digest to roll back to, because nothing has been published. Do not add replicas or spend to recover from a failure.
