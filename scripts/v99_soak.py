"""V99 24-hour unattended soak runner script (Ticket DECbox).

Low resource. Every N minutes (default 60) it triggers the P0 daily flow
through the O20 local orchestrator. Collectors only for AUTH-04-enabled sources,
within their policy rate limits; on network failure, record the failure,
never fake success.

Each cycle appends a JSONL line with timestamp, run_id, per-source success,
duplicates written, cost (USD, 0 for template steps), errors, QA results and
reports published. Writes to artifacts/v99/cycles.jsonl, plus a heartbeat file.

Resumes safely if restarted (idempotent via O20).

At or after 24h of wall-clock time since the recorded start, it writes:
- artifacts/{run_id}/V99/soak_test.json
- artifacts/{run_id}/V99/manifest.json
- artifacts/{run_id}/V99/metrics.json
- docs/decisions/mvp_acceptance.md

Before 24h it must never write PASS.
A --status flag prints elapsed time and the running tallies.
"""

from __future__ import annotations

from techpulse.ops.soak import main

if __name__ == "__main__":
    raise SystemExit(main())
