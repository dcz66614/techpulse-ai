"""Regression tests for DUPfix (TechPulse-AI P0004).

Evidence (artifacts/tmp/box3, run v99box3): cycle 2
(v99-20261103T005323Z-0002) is the only cycle with duplicates_written=1 and
took 24008s. o20_task_state shows C20 (github) attempt 1 -> retryable_error
reason=timeout at 2026-11-03T07:32:00Z (host suspended), then attempt 2
success at 07:33:31Z. The cycle-2 D10 manifest contains github record
"1362490" twice: line 28 collected_at 07:32:00.382273Z (stars 54593) and
line 29 collected_at 07:33:31.264298Z (stars 54594). The old soak runner
accumulated both attempts' records in a single in-memory list via extend(),
so D10 emitted the record key twice and _store_success counted the second as
1 duplicate (o20_records PK (task_id, input_sha256, record_key) prevents the
row being stored twice, hence zero duplicate groups in the DB).

Covers:
(a) Duplicate-after-resume scenario (fake clock/fake sources): a cycle
    suspended mid-collector is retried; must write 0 duplicates. Fails on the
    old shared-list buffer, passes on the fix (per-task buffer + D10 dedupe).
(b) Saved last_due path in soak_state.json with no cycle records: after a
    restart the next due is the next grid slot strictly after saved last_due.
(c) Resume-after-suspension: missed slots are not backfilled, the first cycle
    after resume runs once immediately, then the grid continues with no
    duplicate slot.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from techpulse.collectors.base import CollectResult
from techpulse.collectors.github import GitHubCollection
from techpulse.collectors.hn import HNCollection
from techpulse.contracts import LicenseStatus, RawRecord, SourceId, TaskStatus
from techpulse.ops.soak import SoakRunner, _parse_iso, make_stub_activities
from techpulse.workflows.activities import ActivityContext
from techpulse.workflows.orchestrator import ManualClock


class SuspendSimulatingClock(ManualClock):
    """Manual clock that can jump the wall clock to simulate host suspension."""

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("advance seconds must be >= 0")
        self._now += timedelta(seconds=seconds)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _enable_github_in_config(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the runner at an isolated config where AUTH-04 sources are enabled.

    Keeps the test hermetic: no shared ``config/source_registry.yaml`` state and
    no dependence on which tests ran before. This only redirects the runner's
    own registry/policy paths; production registry code is unchanged.
    """
    config_dir = base / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    sources = {
        "openalex": "academic_papers",
        "crossref": "academic_papers",
        "arxiv": "academic_papers",
        "github": "code_repositories",
        "hn": "community_discussions",
    }
    lines = ["version: '1.0.0'", "sources:"]
    for source, family in sources.items():
        lines.extend(
            [
                f"  {source}:",
                f'    source_id: "{source}"',
                f'    source_family: "{family}"',
                "    policy_snapshot_id: 'sha256-test'",
                "    enabled: true",
                "    requests_per_minute: 600",
            ]
        )
    (config_dir / "source_registry.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (config_dir / "policy.yaml").write_text("version: '1.0.0'\nrules: []\n", encoding="utf-8")
    monkeypatch.setattr("techpulse.ops.soak.default_repository_root", lambda: base)


def _raw_record(**values: Any) -> RawRecord:
    """Build a RawRecord from JSON-native values (mirrors the collector helpers)."""
    return RawRecord.model_validate(values)


def _github_record(clock: ManualClock, stars: int) -> RawRecord:
    return _raw_record(
        schema_version="1.0",
        source_id=SourceId.GITHUB,
        source_record_id="1362490",
        canonical_url="https://github.com/psf/requests",
        title="psf/requests",
        abstract_or_excerpt=f"stars: {stars}",
        published_at=_iso_z(datetime(2026, 5, 14, 19, 27, 15, tzinfo=UTC)),
        collected_at=_iso_z(clock.now()),
        language="en",
        license_status=LicenseStatus.REUSABLE_EXCERPT,
        content_sha256=hashlib.sha256(f"stars:{stars}".encode()).hexdigest(),
        policy_snapshot_id="sha256-test",
    )


# ---------------------------------------------------------------------------
# (a) Duplicate-after-resume scenario with fake clock/fake sources
# ---------------------------------------------------------------------------


def test_cycle_interrupted_midway_resumes_writes_zero_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collector suspended past its deadline and retried must not double-write.

    Mirrors box3 cycle 2: github attempt 1 collects "1362490", the host clock
    jumps 6.5h so the orchestrator marks attempt 1 as timeout, and attempt 2
    collects the same source_record_id again. The fixed runner replaces the
    per-task buffer entry and D10 deduplicates, so 0 duplicates are written and
    raw_records holds exactly one row for the record.
    """
    t0 = datetime(2026, 11, 3, 0, 53, 23, tzinfo=UTC)
    clock = SuspendSimulatingClock(t0)

    github_attempts: list[int] = []

    class FakeGitHubCollector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeGitHubCollector:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def collect(self, _repos: list[str], cursor: str | None = None) -> GitHubCollection:
            attempt = len(github_attempts) + 1
            github_attempts.append(attempt)
            if attempt == 1:
                rec = _github_record(clock, 54593)
                clock.advance(6.5 * 3600)  # host suspended past the 1h activity deadline
                return GitHubCollection((rec,), "cursor-1")
            rec = _github_record(clock, 54594)
            return GitHubCollection((rec,), "cursor-2")

    def empty_collect(**_kwargs: object) -> CollectResult:
        return CollectResult(status=TaskStatus.SUCCESS, records=[], new_count=0, cursor=None)

    class FakeHNCollector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeHNCollector:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def collect(self, _ids: list[int], cursor: str | None = None) -> HNCollection:
            return HNCollection((), "cursor-hn")

    monkeypatch.setattr("techpulse.collectors.github.GitHubCollector", FakeGitHubCollector)
    monkeypatch.setattr("techpulse.collectors.hn.HNCollector", FakeHNCollector)
    monkeypatch.setattr("techpulse.collectors.openalex.collect_openalex", empty_collect)
    monkeypatch.setattr("techpulse.collectors.crossref.collect_crossref", empty_collect)
    monkeypatch.setattr("techpulse.collectors.arxiv.collect_arxiv", empty_collect)

    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        artifacts = base / "artifacts"
        docs = base / "docs"
        db = base / "soak.sqlite"
        _enable_github_in_config(base, monkeypatch)

        runner = SoakRunner(
            artifacts_dir=artifacts,
            docs_dir=docs,
            database_path=db,
            clock=clock,
            interval_minutes=60.0,
            target_hours=24.0,
        )

        rec = runner.execute_cycle(1)

        # The timed-out collector was retried exactly twice (mirror of the incident).
        assert github_attempts == [1, 2]
        assert rec.success is True
        assert rec.errors == []
        assert all(rec.per_source_success.values())

        # Crucial invariant: no duplicate was counted or written.
        assert rec.duplicates_written == 0
        cycles = runner.load_cycles()
        assert len(cycles) == 1
        assert cycles[0].duplicates_written == 0

        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        gh_records = con.execute(
            "SELECT * FROM raw_records WHERE source_id='github' AND source_record_id='1362490'"
        ).fetchall()
        assert len(gh_records) == 1
        assert gh_records[0]["abstract_or_excerpt"] == "stars: 54594"

        dupe_check = con.execute(
            "SELECT task_id, input_sha256, record_key, COUNT(*) AS cnt "
            "FROM o20_records GROUP BY task_id, input_sha256, record_key HAVING cnt > 1"
        ).fetchall()
        assert len(dupe_check) == 0
        con.close()


# ---------------------------------------------------------------------------
# (a2) Successful empty retry must clear a timed-out attempt's stale records
# ---------------------------------------------------------------------------


def test_successful_empty_retry_clears_stale_records_from_timed_out_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SUCCESS retry with zero records replaces the earlier attempt's buffer.

    Mirror of the Copilot DUPfix4 finding on c65f6d9: attempt 1 of github
    collects record "1362490" and is staged in the buffer while SUCCESS, but
    the host clock jump makes the orchestrator time it out and discard its
    result; attempt 2 is retried and succeeds with an EMPTY records list. The
    old ``if status is SUCCESS and records:`` guard skips the empty success, so
    attempt 1's stale record survives into D10's archive input. The fix always
    assigns on SUCCESS (even empty), so the archived NDJSON has none of the
    stale record and zero duplicates.
    """
    t0 = datetime(2026, 11, 3, 0, 53, 23, tzinfo=UTC)
    clock = SuspendSimulatingClock(t0)

    github_attempts: list[int] = []

    class FakeGitHubCollector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeGitHubCollector:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def collect(self, _repos: list[str], cursor: str | None = None) -> GitHubCollection:
            attempt = len(github_attempts) + 1
            github_attempts.append(attempt)
            if attempt == 1:
                rec = _github_record(clock, 54593)
                clock.advance(6.5 * 3600)  # host suspends past the 1h deadline
                return GitHubCollection((rec,), "cursor-1")
            # The retry succeeds but the cursor already advanced, so no new
            # records: an empty SUCCESS that must clear the stale buffer.
            return GitHubCollection((), "cursor-2")

    def empty_collect(**_kwargs: object) -> CollectResult:
        return CollectResult(status=TaskStatus.SUCCESS, records=[], new_count=0, cursor=None)

    class FakeHNCollector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeHNCollector:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def collect(self, _ids: list[int], cursor: str | None = None) -> HNCollection:
            return HNCollection((), "cursor-hn")

    monkeypatch.setattr("techpulse.collectors.github.GitHubCollector", FakeGitHubCollector)
    monkeypatch.setattr("techpulse.collectors.hn.HNCollector", FakeHNCollector)
    monkeypatch.setattr("techpulse.collectors.openalex.collect_openalex", empty_collect)
    monkeypatch.setattr("techpulse.collectors.crossref.collect_crossref", empty_collect)
    monkeypatch.setattr("techpulse.collectors.arxiv.collect_arxiv", empty_collect)

    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        artifacts = base / "artifacts"
        docs = base / "docs"
        db = base / "soak.sqlite"
        _enable_github_in_config(base, monkeypatch)

        runner = SoakRunner(
            artifacts_dir=artifacts,
            docs_dir=docs,
            database_path=db,
            clock=clock,
            interval_minutes=60.0,
            target_hours=24.0,
        )

        rec = runner.execute_cycle(1)

        assert github_attempts == [1, 2]
        assert rec.success is True
        assert rec.errors == []
        assert all(rec.per_source_success.values())
        assert rec.duplicates_written == 0

        run_id = rec.run_id
        raw_path = artifacts / run_id / "D10" / "staging" / "raw.ndjson"
        assert raw_path.is_file()
        raw_text = raw_path.read_text(encoding="utf-8")
        assert "1362490" not in raw_text
        assert raw_text.strip() == ""

        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        gh_records = con.execute(
            "SELECT * FROM raw_records WHERE source_id='github' AND source_record_id='1362490'"
        ).fetchall()
        assert len(gh_records) == 0

        dupe_check = con.execute(
            "SELECT task_id, input_sha256, record_key, COUNT(*) AS cnt "
            "FROM o20_records GROUP BY task_id, input_sha256, record_key HAVING cnt > 1"
        ).fetchall()
        assert len(dupe_check) == 0
        con.close()


# ---------------------------------------------------------------------------
# DUPfix5 — restart adoption loses the in-memory raw-record buffer
# ---------------------------------------------------------------------------


def _activity_context(
    clock: ManualClock,
    task_id: str,
    run_id: str,
    *,
    attempt: int = 1,
    source_id: str | None = None,
) -> ActivityContext:
    """Build a minimal ActivityContext for directly invoking a soak activity."""
    return ActivityContext(
        task_id=task_id,
        run_id=run_id,
        attempt=attempt,
        input_sha256="a" * 64,
        report_kind="daily",
        slot="",
        deadline=clock.now() + timedelta(seconds=3600),
        clock=clock,
        params={"source_id": source_id} if source_id else {},
        upstream={},
    )


def _install_github_collector(
    monkeypatch: pytest.MonkeyPatch,
    records_by_attempt: Sequence[RawRecord],
) -> list[int]:
    """Replace GitHubCollector with a fake returning one record per invocation."""
    calls: list[int] = []

    class FakeGitHubCollector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeGitHubCollector:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def collect(self, _repos: list[str], cursor: str | None = None) -> GitHubCollection:
            idx = len(calls)
            calls.append(idx)
            rec = records_by_attempt[min(idx, len(records_by_attempt) - 1)]
            return GitHubCollection((rec,), f"cursor-{idx}")

    monkeypatch.setattr("techpulse.collectors.github.GitHubCollector", FakeGitHubCollector)
    return calls


def test_collector_records_survive_restart_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D10 recovers a collector's raw records from disk after the buffer is lost.

    Mirrors the Copilot DUPfix5 blocking finding on 6fe0fea: after a restart the
    orchestrator may ``_adopt()`` a cached successful collector result via
    ``_lookup`` / ``_resume_running`` WITHOUT invoking the collector activity,
    so ``_run_records`` is empty. On 6fe0fea D10 then archives no records for
    that task (legit data lost). The fix persists records on collector SUCCESS
    and D10 falls back to that file, so the archive still holds the record with
    zero duplicates.
    """
    clock = SuspendSimulatingClock(datetime(2026, 11, 3, 0, 53, 23, tzinfo=UTC))
    _install_github_collector(monkeypatch, [_github_record(clock, 54594)])

    artifacts = tmp_path / "artifacts"
    db = tmp_path / "soak.sqlite"
    run_id = "v99-restart-adopt-0001"

    runner1 = SoakRunner(
        artifacts_dir=artifacts,
        docs_dir=tmp_path / "docs",
        database_path=db,
        clock=clock,
    )
    collector1 = runner1.build_activities()["C20"]
    cres = collector1(_activity_context(clock, "C20", run_id, source_id="github"))
    assert cres.status is TaskStatus.SUCCESS

    # Simulate a process restart: a brand-new runner shares artifacts/db but
    # its in-memory record buffer is empty (the cached collector result would
    # be adopted without invoking the activity).
    runner2 = SoakRunner(
        artifacts_dir=artifacts,
        docs_dir=tmp_path / "docs",
        database_path=db,
        clock=clock,
    )
    assert run_id not in runner2._run_records

    d10 = runner2.build_activities()["D10"]
    dres = d10(_activity_context(clock, "D10", run_id))
    assert dres.status is TaskStatus.SUCCESS

    raw_text = (artifacts / run_id / "D10" / "staging" / "raw.ndjson").read_text(encoding="utf-8")
    assert "1362490" in raw_text
    assert raw_text.count("1362490") == 1

    con = sqlite3.connect(db)
    gh = con.execute("SELECT * FROM raw_records WHERE source_record_id='1362490'").fetchall()
    assert len(gh) == 1
    assert gh[0][5] == "stars: 54594"
    dupe_check = con.execute(
        "SELECT source_id, source_record_id, COUNT(*) AS cnt "
        "FROM raw_records GROUP BY source_id, source_record_id HAVING cnt > 1"
    ).fetchall()
    assert len(dupe_check) == 0
    con.close()


def test_corrupt_collector_cache_file_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt persisted collector file is treated as empty, never crashes D10."""
    clock = SuspendSimulatingClock(datetime(2026, 11, 3, 1, 0, 0, tzinfo=UTC))
    _install_github_collector(monkeypatch, [_github_record(clock, 54594)])

    artifacts = tmp_path / "artifacts"
    db = tmp_path / "soak.sqlite"
    run_id = "v99-corrupt-cache-0001"

    runner = SoakRunner(
        artifacts_dir=artifacts,
        docs_dir=tmp_path / "docs",
        database_path=db,
        clock=clock,
    )
    collector = runner.build_activities()["C20"]
    assert collector(_activity_context(clock, "C20", run_id, source_id="github")).status is (
        TaskStatus.SUCCESS
    )

    # Overwrite the persisted file with corrupt JSON.
    cache_file = artifacts / run_id / "_collect" / "C20.json"
    assert cache_file.is_file()
    cache_file.write_text("{not valid json", encoding="utf-8")

    # Simulate restart (empty buffer); D10 must ignore the corrupt file.
    runner2 = SoakRunner(
        artifacts_dir=artifacts,
        docs_dir=tmp_path / "docs",
        database_path=db,
        clock=clock,
    )
    d10 = runner2.build_activities()["D10"]
    dres = d10(_activity_context(clock, "D10", run_id))
    assert dres.status is TaskStatus.SUCCESS

    raw_text = (artifacts / run_id / "D10" / "staging" / "raw.ndjson").read_text(encoding="utf-8")
    assert "1362490" not in raw_text
    assert raw_text.strip() == ""

    con = sqlite3.connect(db)
    gh = con.execute("SELECT * FROM raw_records WHERE source_record_id='1362490'").fetchall()
    assert len(gh) == 0
    con.close()


def test_retry_persists_only_latest_attempt_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried collector overwrites its cache; a late zombie attempt cannot.

    Both attempts collect source_record_id "1362490"; only the latest
    SUCCESS's records (stars 54594) may be persisted, and a late-finishing
    attempt 1 result (stars 54593) must not overwrite them. D10 after a restart
    yields exactly one record — the latest attempt's.
    """
    clock = SuspendSimulatingClock(datetime(2026, 11, 3, 2, 0, 0, tzinfo=UTC))
    _install_github_collector(
        monkeypatch,
        [_github_record(clock, 54593), _github_record(clock, 54594)],
    )

    artifacts = tmp_path / "artifacts"
    db = tmp_path / "soak.sqlite"
    run_id = "v99-retry-latest-0001"

    runner = SoakRunner(
        artifacts_dir=artifacts,
        docs_dir=tmp_path / "docs",
        database_path=db,
        clock=clock,
    )
    collector = runner.build_activities()["C20"]
    assert collector(
        _activity_context(clock, "C20", run_id, attempt=1, source_id="github")
    ).status is (TaskStatus.SUCCESS)
    assert collector(
        _activity_context(clock, "C20", run_id, attempt=2, source_id="github")
    ).status is (TaskStatus.SUCCESS)
    # A late zombie result from superseded attempt 1 must be ignored.
    assert collector(
        _activity_context(clock, "C20", run_id, attempt=1, source_id="github")
    ).status is (TaskStatus.SUCCESS)

    # Restart: empty buffer, D10 loads only the latest attempt's records.
    runner2 = SoakRunner(
        artifacts_dir=artifacts,
        docs_dir=tmp_path / "docs",
        database_path=db,
        clock=clock,
    )
    d10 = runner2.build_activities()["D10"]
    dres = d10(_activity_context(clock, "D10", run_id))
    assert dres.status is TaskStatus.SUCCESS

    raw_text = (artifacts / run_id / "D10" / "staging" / "raw.ndjson").read_text(encoding="utf-8")
    assert raw_text.count("1362490") == 1
    assert "stars: 54594" in raw_text
    assert "stars: 54593" not in raw_text

    con = sqlite3.connect(db)
    gh = con.execute("SELECT * FROM raw_records WHERE source_record_id='1362490'").fetchall()
    assert len(gh) == 1
    assert gh[0][5] == "stars: 54594"
    con.close()


# ---------------------------------------------------------------------------
# (b) Saved last_due path in soak_state.json
# ---------------------------------------------------------------------------


def test_soak_restart_with_saved_last_due_computes_next_grid_slot() -> None:
    """With no cycle records but a saved last_due, restart schedules the next slot.

    On the old code compute_next_due() returns start_time() when cycles.jsonl
    is empty, ignoring the saved last_due and reusing an already-executed slot.
    The fix uses last_due as the reference so the next grid slot is strictly
    after the saved value, and that survives a process restart.
    """
    t0 = datetime(2026, 11, 2, 10, 0, 0, tzinfo=UTC)
    clock = SuspendSimulatingClock(t0)
    interval_minutes = 60.0
    interval = timedelta(minutes=interval_minutes)

    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        artifacts = base / "artifacts"
        docs = base / "docs"
        db = base / "soak.sqlite"
        stubs = make_stub_activities()

        runner1 = SoakRunner(
            artifacts_dir=artifacts,
            docs_dir=docs,
            database_path=db,
            clock=clock,
            interval_minutes=interval_minutes,
            activities=stubs,
        )
        assert runner1.start_time() == t0
        # Simulate two slots already scheduled whose cycle records were not
        # persisted (e.g. a crash between scheduling and recording).
        saved_last_due = t0 + 2 * interval
        runner1.last_due = saved_last_due
        runner1._save_state()

        state_data: Mapping[str, Any] = json.loads(runner1.state_file.read_text(encoding="utf-8"))
        assert state_data["last_due"] == "2026-11-02T12:00:00Z"

        # Next due must be the slot strictly after the saved last_due.
        assert runner1.compute_next_due() == t0 + 3 * interval

        # Simulate process restart: no cycles.jsonl, only soak_state.json.
        runner2 = SoakRunner(
            artifacts_dir=artifacts,
            docs_dir=docs,
            database_path=db,
            clock=clock,
            interval_minutes=interval_minutes,
            activities=stubs,
        )
        assert runner2.load_cycles() == []
        assert runner2.last_due == saved_last_due
        assert runner2.compute_next_due() == t0 + 3 * interval


# ---------------------------------------------------------------------------
# (c) Resume-after-suspension soak test
# ---------------------------------------------------------------------------


def test_resume_after_suspension_no_backfill_and_no_duplicate_slot() -> None:
    """Due slots missed during suspension are not backfilled.

    The first cycle after resume runs once immediately; the grid then snaps
    to the next future slot with no duplicate slot and no duplicates written.
    """
    t0 = datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC)
    clock = SuspendSimulatingClock(t0)
    interval_minutes = 60.0
    interval = timedelta(minutes=interval_minutes)

    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        artifacts = base / "artifacts"
        docs = base / "docs"
        db = base / "soak.sqlite"
        stubs = make_stub_activities()

        runner = SoakRunner(
            artifacts_dir=artifacts,
            docs_dir=docs,
            database_path=db,
            clock=clock,
            interval_minutes=interval_minutes,
            activities=stubs,
        )

        # Cycle 1 at 00:00 UTC (slot 0).
        rec1 = runner.execute_cycle(1)
        assert rec1.success is True
        assert rec1.timestamp == "2026-11-02T00:00:00Z"
        runner.last_due = t0
        runner._save_state()

        due_slot_1 = runner.compute_next_due()
        assert due_slot_1 == t0 + interval  # 01:00 UTC

        # Machine runs 30 minutes, then suspends for 5.75h (00:30 -> 06:15 UTC);
        # missed slots 01:00..06:00.
        clock.advance(1800.0)
        clock.advance(5.75 * 3600.0)
        assert clock.now() == datetime(2026, 11, 2, 6, 15, 0, tzinfo=UTC)

        # Resume: overdue slot returns immediately (no wait, no backfill burst).
        assert clock.now() > due_slot_1
        gap = runner.sleep_until_next_cycle(due_slot_1, slice_seconds=30.0)
        assert gap > 0

        # Cycle 2 runs ONCE immediately at the real time 06:15 UTC.
        rec2 = runner.execute_cycle(2)
        assert rec2.success is True
        assert rec2.timestamp == "2026-11-02T06:15:00Z"
        runner.last_due = due_slot_1
        runner._save_state()

        # Next due snaps strictly to the next FUTURE slot: 07:00 UTC.
        due_slot_3 = runner.compute_next_due(after_time=_parse_iso(rec2.timestamp))
        assert due_slot_3 == t0 + 7 * interval
        assert due_slot_3 > clock.now()
        assert (due_slot_3 - clock.now()).total_seconds() == 45 * 60.0

        gap3 = runner.sleep_until_next_cycle(due_slot_3, slice_seconds=30.0)
        assert gap3 == 0.0

        rec3 = runner.execute_cycle(3)
        assert rec3.success is True
        assert rec3.timestamp == "2026-11-02T07:00:00Z"
        runner.last_due = due_slot_3
        runner._save_state()

        cycles = runner.load_cycles()
        assert len(cycles) == 3
        assert [c.timestamp for c in cycles] == [
            "2026-11-02T00:00:00Z",
            "2026-11-02T06:15:00Z",
            "2026-11-02T07:00:00Z",
        ]
        assert all(c.duplicates_written == 0 for c in cycles)
