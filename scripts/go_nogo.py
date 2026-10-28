"""V90 release gate runner script.

Executes deterministic staging go/no-go checks and writes:
- artifacts/{run_id}/V90/go_nogo.json
- artifacts/{run_id}/V90/manifest.json
- artifacts/{run_id}/V90/metrics.json
- docs/decisions/release_acceptance.md
"""

from __future__ import annotations

from techpulse.release.go_nogo import main

if __name__ == "__main__":
    raise SystemExit(main())
