"""Tests for the P00 environment report status logic."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.write_env_report import evaluate_checks, write_reports  # noqa: E402


def test_all_checks_pass() -> None:
    """A 3.12 interpreter with uv and git installed reports success."""
    status, errors = evaluate_checks("3.12.13", "uv 0.12.2", "git version 2.55.0", "not installed")

    assert status == "success"
    assert errors == []


def test_python_311_is_fatal() -> None:
    """A non-3.12 interpreter reports a fatal error."""
    status, errors = evaluate_checks("3.11.9", "uv 0.12.2", "git version 2.55.0", "not installed")

    assert status == "fatal_error"
    assert errors == ["python is not 3.12.x"]


def test_missing_uv_is_fatal() -> None:
    """A missing uv command reports a fatal error."""
    status, errors = evaluate_checks(
        "3.12.13", "not installed", "git version 2.55.0", "not installed"
    )

    assert status == "fatal_error"
    assert errors == ["uv is not installed"]


def test_missing_docker_is_only_a_warning() -> None:
    """A missing docker command still reports success."""
    status, errors = evaluate_checks("3.12.13", "uv 0.12.2", "git version 2.55.0", "not installed")

    assert status == "success"
    assert errors == []


def test_reports_record_fatal_errors() -> None:
    """The manifest lists the failed checks and metrics carry the fatal status."""
    environment = {
        "task_id": "P00",
        "run_id": "test-run",
        "generated_at": "2026-10-27T00:00:00+00:00",
        "python": {"version": "3.11.9", "is_3_12": False},
        "uv": {"version": "uv 0.12.2"},
        "git": {"version": "git version 2.55.0"},
        "docker": {"version": "not installed"},
    }
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        output_dir = root / "P00"
        output_dir.mkdir()

        write_reports(
            output_dir,
            root,
            "test-run",
            environment,
            "fatal_error",
            ["python is not 3.12.x"],
            0.01,
        )

        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        metrics = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))

    assert manifest["status"] == "fatal_error"
    assert manifest["errors"] == ["python is not 3.12.x"]
    assert metrics["status"] == "fatal_error"
