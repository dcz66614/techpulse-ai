"""O40 backup script.

Back up SQLite database(s), source policy snapshots, and manifests.
PostgreSQL pg_dump is documented as target only.
"""

from __future__ import annotations

from techpulse.ops.backup import main as backup_main


def main(argv: list[str] | None = None) -> int:
    return backup_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
