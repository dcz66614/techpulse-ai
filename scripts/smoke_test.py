"""Check A10 /health/live and /health/ready in process.

The app is the one uvicorn serves. This script uses FastAPI TestClient, so
it does not bind a port, call a network, or read an env file. When no
database is given, it first applies the migrations to a temporary SQLite file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from techpulse.ops.release import migration_dry_run, run_smoke


def main(argv: list[str] | None = None) -> int:
    """Return 0 when both health endpoints pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.database is None:
        with TemporaryDirectory(prefix="o10-smoke-") as tmp:
            database = Path(tmp) / "dry-run.sqlite"
            migration = migration_dry_run(database)
            if not migration.ok:
                print("smoke: migration dry-run failed", file=sys.stderr)
                return 1
            result = run_smoke(database, Path(tmp) / "objects")
    else:
        result = run_smoke(args.database, args.database.parent / "o10-smoke-objects")
    if result.ok:
        print("smoke: pass")
        return 0
    print("smoke: " + "; ".join(result.errors), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
