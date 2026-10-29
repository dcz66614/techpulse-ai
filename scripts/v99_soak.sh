#!/usr/bin/env bash
# V99 24-hour unattended soak runner (Ticket DECbox).
# Triggers P0 daily flow through O20 local orchestrator every N minutes (default 60).
# POSIX and nohup-friendly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"
export UV_PROJECT_ENVIRONMENT="${REPO_ROOT}/.venv"

# Ensure artifacts/v99 directory exists
mkdir -p "${REPO_ROOT}/artifacts/v99"

exec uv run python "${REPO_ROOT}/scripts/v99_soak.py" "$@"
