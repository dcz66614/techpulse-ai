"""Write the P00 environment, manifest, and metrics reports."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def command_version(name: str) -> str:
    """Return the version reported by an external command."""
    if shutil.which(name) is None:
        return "not installed"
    result = subprocess.run(
        [name, "--version"],
        capture_output=True,
        check=False,
        text=True,
    )
    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0 or not output:
        return "not installed"
    return output


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
    """Generate the P00 reports and print their location."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="Run identifier for the artifact path")
    args = parser.parse_args()

    started = time.perf_counter()
    generated_at = datetime.now(UTC).isoformat()
    root = Path(__file__).resolve().parents[1]
    output_dir = root / "artifacts" / args.run_id / "P00"
    output_dir.mkdir(parents=True, exist_ok=True)

    python_version = platform.python_version()
    uv_version = command_version("uv")
    git_version = command_version("git")
    docker_version = command_version("docker")

    environment = {
        "task_id": "P00",
        "run_id": args.run_id,
        "generated_at": generated_at,
        "python": {
            "version": python_version,
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "is_3_12": python_version.startswith("3.12."),
        },
        "uv": {"version": uv_version},
        "git": {"version": git_version},
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "arch": platform.machine(),
        },
        "docker": {
            "version": docker_version,
            "container_image_digest": "not applicable",
        },
    }

    environment_path = output_dir / "environment.json"
    write_json(environment_path, environment)

    elapsed = round(time.perf_counter() - started, 6)
    metrics = {
        "task_id": "P00",
        "run_id": args.run_id,
        "generated_at": generated_at,
        "status": "success",
        "duration_seconds": elapsed,
        "checks": {
            "python_3_12": environment["python"]["is_3_12"],
            "uv_available": uv_version != "not installed",
            "git_available": git_version != "not installed",
            "docker_available": docker_version != "not installed",
        },
    }
    metrics_path = output_dir / "metrics.json"
    write_json(metrics_path, metrics)

    manifest = {
        "task_id": "P00",
        "run_id": args.run_id,
        "generated_at": generated_at,
        "artifacts": [
            {
                "path": environment_path.relative_to(root).as_posix(),
                "sha256": sha256_file(environment_path),
                "bytes": environment_path.stat().st_size,
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

    print(json.dumps({"output_dir": str(output_dir), "status": "success"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
