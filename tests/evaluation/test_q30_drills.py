"""Q30 capacity, cost, and disaster-recovery drills.

Records are generated in the test process. The capacity replay is marked
``capacity`` and is excluded from the default suite. This file does not read
the sealed I30 retrieval set.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from techpulse.qa.drills import (
    CAPACITY_N,
    FAST_N,
    Q30_MAX_USD,
    batch_sha256,
    run_q30_drill,
    synthetic_records,
)

ROOT = Path(__file__).resolve().parents[2]
_FAST_DAY = datetime(2026, 10, 28, tzinfo=UTC)


def test_q30_small_day_uses_the_capacity_path(tmp_path: Path) -> None:
    """A small N runs the same drill as the 10k replay, including faults and restore."""
    first = synthetic_records(FAST_N, day=_FAST_DAY)
    assert synthetic_records(FAST_N, day=_FAST_DAY) == first
    report = run_q30_drill(
        n=FAST_N,
        work=tmp_path / "work",
        release_root=tmp_path / "artifacts",
        day=_FAST_DAY,
    )
    assert report.status == "pass", report.errors
    assert report.record_count == FAST_N
    assert report.raw_rows == FAST_N
    assert report.tokens == 0
    assert report.cost_usd == 0.0
    assert report.budget_spent_usd == 0.0
    assert report.cost_usd <= Q30_MAX_USD
    assert report.retry_attempts == 3
    assert report.backoff_seconds == (30.0, 30.0)
    assert report.retry_backoff_seconds == 60.0
    assert report.replay_backoff_seconds == 0.0
    assert report.replay_records_inserted == 0
    assert report.replay_duplicate_records == 0
    assert report.replay_activity_calls == 0
    assert report.published_reports == 1
    assert report.duplicate_public_reports == 0
    assert report.paused_published == (0,)
    assert report.faults == {"database_disconnect": 1, "http_429": 1, "object_store": 1}
    assert report.rolled_back is False
    assert report.sealed_i30_used is False
    assert report.network_calls == 0
    assert report.latency_seconds >= 0.0
    assert report.restore.match is True
    assert report.restore.database_rows == report.restore.restored_rows
    assert report.restore.database_sha256 == report.restore.restored_sha256
    assert report.restore.artifact_files == report.restore.restored_artifact_files
    assert report.restore.artifact_files > 0
    assert report.restore.artifact_sha256 == report.restore.restored_artifact_sha256
    assert report.restore.tables == report.restore.restored_tables
    assert report.batch_sha256 == batch_sha256(first)
    directory = tmp_path / "artifacts" / report.run_id / "Q30"
    _assert_release_files(directory, report.run_id)


@pytest.mark.capacity
def test_q30_capacity_replays_one_day_of_10k() -> None:
    """One daily slot of 10k synthetic records, replayed through the O20 orchestrator."""
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        report = run_q30_drill(
            n=CAPACITY_N,
            work=Path(tmp),
            release_root=ROOT / "artifacts",
        )
    assert report.status == "pass", report.errors
    assert report.record_count == CAPACITY_N
    assert report.raw_rows == CAPACITY_N
    assert report.tokens == 0
    assert report.cost_usd == 0.0
    assert report.replay_records_inserted == 0
    assert report.duplicate_public_reports == 0
    assert report.published_reports == 1
    assert report.restore.match is True
    assert report.sealed_i30_used is False
    directory = ROOT / "artifacts" / report.run_id / "Q30"
    _assert_release_files(directory, report.run_id)


def _assert_release_files(directory: Path, run_id: str) -> None:
    release = _read(directory / "qa_release.json")
    metrics = _read(directory / "metrics.json")
    manifest = _read(directory / "manifest.json")
    assert release["ticket"] == "Q30"
    assert release["status"] == "pass"
    assert release["tokens"] == 0
    assert release["duplicate_public_reports"] == 0
    assert release["restore_match"] is True
    assert metrics["ticket"] == "Q30"
    assert metrics["tokens"] == 0
    assert metrics["template_steps"] is True
    assert metrics["sealed_i30_used"] is False
    assert metrics["network_calls"] == 0
    assert manifest["ticket"] == "Q30"
    assert manifest["status"] == "pass"
    files = manifest["files"]
    assert isinstance(files, list)
    names = set()
    for entry in files:
        assert isinstance(entry, dict)
        path = entry["path"]
        digest = entry["sha256"]
        assert isinstance(path, str)
        assert isinstance(digest, str)
        name = Path(path).name
        names.add(name)
        assert path == f"artifacts/{run_id}/Q30/{name}"
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
    assert names == {"metrics.json", "qa_release.json"}


def _read(path: Path) -> dict[str, object]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(path.name)
    parsed: dict[str, object] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise AssertionError(path.name)
        parsed[key] = value
    return parsed
