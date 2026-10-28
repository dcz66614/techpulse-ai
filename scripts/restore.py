"""O40 restore script.

Restore a backup into an isolated directory and verify row counts and SHA-256.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from techpulse.ops.backup import restore_backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup-dir",
        "-b",
        type=Path,
        required=True,
        help="Backup directory to restore from",
    )
    parser.add_argument(
        "--target-dir",
        "-t",
        type=Path,
        default=None,
        help="Target directory to restore into (default: isolated temporary directory)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip row count and checksum verification",
    )
    args = parser.parse_args(argv)

    if args.target_dir is not None:
        result = restore_backup(args.backup_dir, args.target_dir, verify=not args.no_verify)
        if result.ok:
            print(f"Restore succeeded into {args.target_dir}")
            print(
                f"Database rows: {result.database_rows}, "
                f"sha256 match: {result.database_sha256_match}"
            )
            return 0
        print(f"Restore verification failed: {'; '.join(result.errors)}", file=sys.stderr)
        return 1

    with TemporaryDirectory(prefix="o40-isolated-restore-") as tmp:
        target = Path(tmp) / "restored"
        result = restore_backup(args.backup_dir, target, verify=not args.no_verify)
        if result.ok:
            print(f"Isolated restore succeeded into temporary directory {target}")
            print(
                f"Database rows: {result.database_rows}, "
                f"sha256 match: {result.database_sha256_match}"
            )
            return 0
        print(f"Restore verification failed: {'; '.join(result.errors)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
