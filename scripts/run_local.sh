#!/usr/bin/env bash
# Starts the TechPulse-AI read-only API locally on the owner's PC.
# Authorized by AUTH-02 / AUTH-06 (Ticket DEC) for local single-machine deployment.
set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-8000}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "Starting TechPulse-AI API locally at http://${HOST}:${PORT}"
echo "Local deployment target: SQLite + uvicorn (AUTH-02 / AUTH-06)"

cd "${REPO_ROOT}"
export UV_PROJECT_ENVIRONMENT="${REPO_ROOT}/.venv"

exec uv run uvicorn techpulse.api.main:app --host "${HOST}" --port "${PORT}"
