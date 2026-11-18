#!/usr/bin/env bash
# Starts the TechPulse-AI read-only API locally on the owner's Windows PC.
# Authorized by AUTH-02 / AUTH-06 for local single-machine deployment (corrected 2026-11-18).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"
export UV_PROJECT_ENVIRONMENT="${REPO_ROOT}/.venv"

# Ensure API database path and URL are configured
export TECHPULSE_API_DATABASE="${TECHPULSE_API_DATABASE:-${REPO_ROOT}/artifacts/app.sqlite}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///${TECHPULSE_API_DATABASE}}"

# Sync dependencies from frozen lockfile
uv sync --frozen

# Ensure database schema is initialized if not present
if [ ! -f "${TECHPULSE_API_DATABASE}" ]; then
    echo "Initializing database schema at ${TECHPULSE_API_DATABASE}..."
    uv run alembic upgrade head
fi

# Ensure artifacts/deploy directory exists
mkdir -p "${REPO_ROOT}/artifacts/deploy"

PORT="${1:-8000}"
LOG_FILE="${REPO_ROOT}/artifacts/deploy/uvicorn.log"

echo "Starting TechPulse-AI API bound to 127.0.0.1:${PORT} (logging to ${LOG_FILE})"

exec uv run uvicorn techpulse.api.main:app --host 127.0.0.1 --port "${PORT}" >> "${LOG_FILE}" 2>&1
