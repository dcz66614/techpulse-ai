"""Apply Alembic head to a temporary SQLite database.

This does not open the PostgreSQL URL in alembic.ini and does not edit that
file. The temporary database is deleted when the process exits unless
--database points at a file the caller owns.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from techpulse.ops.release import migration_dry_run


def main(argv: list[str] | None = None) -> int:
    """Return 0 when the temporary database reaches Alembic head."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.database is None:
        with TemporaryDirectory(prefix="o10-migrate-") as tmp:
            result = migration_dry_run(Path(tmp) / "dry-run.sqlite")
    else:
        result = migration_dry_run(args.database)
    if result.ok:
        print(f"migration dry-run: pass revision={result.revision}")
        return 0
    print("migration dry-run: " + "; ".join(result.errors), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
