"""Write artifacts/{run_id}/O10/release_manifest.json.

The image digest is "N/A (local deployment, no container)". Never write a fake digest.
This script does not build an image, contact a registry, or deploy. It runs the
SQLite migration dry-run and the local A10 smoke check, then records the release gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from techpulse.ops.release import (
    LOCAL_IMAGE_DIGEST,
    repository_root,
    write_release,
)


def main(argv: list[str] | None = None) -> int:
    """Write the O10 artifact directory and return 0 when local checks pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifacts-dir", type=Path, default=None)
    parser.add_argument("--qa-manifest", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    root = repository_root()
    artifacts = args.artifacts_dir if args.artifacts_dir is not None else root / "artifacts"
    try:
        result = write_release(
            run_id=args.run_id,
            artifacts_dir=artifacts,
            qa_manifest=args.qa_manifest,
            config_path=args.config,
            image_digest=LOCAL_IMAGE_DIGEST,
        )
    except (OSError, ValueError) as exc:
        print(f"release_manifest: {exc}", file=sys.stderr)
        return 2
    print(result.directory)
    if not result.smoke_ok or not result.migration_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
