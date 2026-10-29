#!/usr/bin/env bash
# Starts the TechPulse-AI read-only API locally on a dedicated Linux box.
# Authorized by AUTH-02 / AUTH-06 (Ticket DEC / DECbox) for local single-machine deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"
export UV_PROJECT_ENVIRONMENT="${REPO_ROOT}/.venv"

# Sync dependencies from frozen lockfile
uv sync --frozen

# Ensure artifacts/deploy directory exists
mkdir -p "${REPO_ROOT}/artifacts/deploy"

PORT="${1:-8000}"
LOG_FILE="${REPO_ROOT}/artifacts/deploy/uvicorn.log"

echo "Starting TechPulse-AI API bound to 127.0.0.1:${PORT} (logging to ${LOG_FILE})"

exec uv run uvicorn techpulse.api.main:app --host 127.0.0.1 --port "${PORT}" >> "${LOG_FILE}" 2>&1
