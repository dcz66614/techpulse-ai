# Staging templates

These files are templates for a future staging apply. They are not a cluster, and nothing in this directory has been applied.

`auth02.granted` in `config/deploy.yaml` is false. That is staging-only. The release gate refuses production, and it also refuses a staging deploy until AUTH-02 is granted and Q10 `qa_pass` is true.

The container image is `pending-auth-02.invalid/techpulse@pending-AUTH-02`. The host `.invalid` is reserved and is not a registry. The digest is the placeholder `pending-AUTH-02` because the Dockerfile digest is still `<PINNED_DIGEST>`.

Do not run `kubectl apply`, `docker build`, `docker push`, or `docker compose`. This machine has no Docker, and AUTH-02 is still pending. There is no Ingress and no domain: domain use is part of AUTH-02.

| File | What it describes |
| --- | --- |
| `staging/namespace.yaml` | Namespace `techpulse-staging` |
| `staging/deployment.yaml` | One non-root replica of the API, with live and ready probes |
| `staging/service.yaml` | ClusterIP only. No external address |

Replicas stay at 1. There is no autoscaler. A failed release does not add capacity.
