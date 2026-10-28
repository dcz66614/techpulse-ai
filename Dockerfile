# syntax=docker/dockerfile:1

# The base image is pinned by digest. Replace <PINNED_DIGEST> before any build.
# After AUTH-02, on a machine allowed to reach the registry, read the digest:
#
#   docker buildx imagetools inspect python:3.12-slim
#
# Paste the 64 hex characters that follow "sha256:" in place of <PINNED_DIGEST>.
# Do not invent a digest. This repository does not record one. O10 does not
# build, pull, or push this image.
FROM python:3.12-slim@sha256:<PINNED_DIGEST>

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin techpulse \
    && mkdir -p /app \
    && chown techpulse:techpulse /app

WORKDIR /app

# uv.lock is the frozen dependency set. Do not replace it with an unpinned install.
COPY --chown=techpulse:techpulse pyproject.toml uv.lock README.md ./
COPY --chown=techpulse:techpulse src ./src
COPY --chown=techpulse:techpulse migrations ./migrations
COPY --chown=techpulse:techpulse alembic.ini ./
COPY --chown=techpulse:techpulse config ./config
COPY --chown=techpulse:techpulse contracts ./contracts

USER techpulse

EXPOSE 8000

# Dependency install stays commented until AUTH-02. Pin a uv image by digest
# the same way as the base image, then run: uv sync --frozen --no-dev
CMD ["python", "-m", "uvicorn", "techpulse.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
