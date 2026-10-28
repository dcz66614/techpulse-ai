"""Run the Q20 red-team suite and write its redteam, manifest, and metrics artifacts."""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from techpulse.redteam import run_suite, suite_document


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON document with stable formatting."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    """Replay the red-team corpus through the S20 guards and write Q20 artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="Run identifier for the artifact path")
    args = parser.parse_args()

    started = time.perf_counter()
    generated_at = datetime.now(UTC).isoformat()
    root = Path(__file__).resolve().parents[1]
    output_dir = root / "artifacts" / args.run_id / "Q20"
    output_dir.mkdir(parents=True, exist_ok=True)

    report = run_suite(
        root / "tests" / "redteam" / "cases",
        policy_path=root / "config" / "policy.yaml",
        registry_path=root / "tests" / "fixtures" / "source_registry.yaml",
    )
    document = suite_document(report)

    redteam_path = output_dir / "redteam.json"
    write_json(redteam_path, {"task_id": "Q20", "run_id": args.run_id, **document})

    duration = round(time.perf_counter() - started, 6)
    metrics = {
        "task_id": "Q20",
        "run_id": args.run_id,
        "generated_at": generated_at,
        "status": report.status,
        "duration_seconds": duration,
        "counts": {
            "cases_total": report.cases_total,
            "cases_passed": report.cases_passed,
            "cases_failed": report.cases_failed,
            "cases_blocked_at_ingest": report.cases_blocked_at_ingest,
            "egress_targets": report.egress_targets,
            "egress_checks_blocked": report.egress_checks_blocked,
            "forbidden_tool_calls_denied": report.forbidden_tool_calls_denied,
            "secrets_leaked": report.secrets_leaked,
            "unauthorized_tool_calls_executed": report.unauthorized_tool_calls_executed,
        },
    }
    metrics_path = output_dir / "metrics.json"
    write_json(metrics_path, metrics)

    manifest = {
        "task_id": "Q20",
        "run_id": args.run_id,
        "generated_at": generated_at,
        "status": report.status,
        "artifacts": [
            {
                "path": redteam_path.relative_to(root).as_posix(),
                "sha256": sha256_file(redteam_path),
                "bytes": redteam_path.stat().st_size,
            },
            {
                "path": metrics_path.relative_to(root).as_posix(),
                "sha256": sha256_file(metrics_path),
                "bytes": metrics_path.stat().st_size,
            },
        ],
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)

    print(
        json.dumps(
            {"output_dir": str(output_dir), "status": report.status, "metrics": metrics["counts"]},
            indent=2,
        )
    )
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
