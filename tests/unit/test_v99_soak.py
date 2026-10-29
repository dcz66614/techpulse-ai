"""Unit tests for V99 24-hour unattended soak runner (Ticket DECbox).

Tests use a fake clock (ManualClock), short intervals, and tempfile.
They do not make live third-party API calls, open listening ports, or read .env.
Covers:
- No PASS before 24h of wall-clock time
- Criteria computed strictly from real cycles
- Failures recorded honestly (never fake success)
- Resumes safely if restarted (idempotent state)
- Running status output
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from techpulse.ops.soak import (
    AUTH04_SOURCES,
    CycleRecord,
    SoakRunner,
)
from techpulse.workflows.orchestrator import ManualClock

_T0 = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)


def test_no_pass_before_24h() -> None:
    """Before 24h elapsed wall-clock time, evaluate must NEVER return PASS."""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        clock = ManualClock(_T0)
        runner = SoakRunner(
            artifacts_dir=base / "artifacts",
            docs_dir=base / "docs" / "decisions",
            database_path=base / "soak.sqlite",
            clock=clock,
            interval_minutes=60.0,
            target_hours=24.0,
        )

        # Run 5 cycles, advancing clock by 1 hour (3600s) each cycle -> 5 hours elapsed
        for idx in range(1, 6):
            rec = runner.execute_cycle(idx)
            assert rec.success is True
            clock.sleep(3600.0)

        assert runner.elapsed_seconds() == 5 * 3600.0
        assert runner.elapsed_seconds() < 86400.0

        # Attempt evaluation before 24h
        eval_early = runner.evaluate()
        assert eval_early.status == "FAIL"
        assert eval_early.criteria["duration_24h_met"] is False
        assert any("24h soak duration requirement not satisfied" in b for b in eval_early.blockers)
        assert any("PASS is strictly prohibited before 24h" in b for b in eval_early.blockers)

        # Artifacts written before 24h must record FAIL, never PASS
        artifacts_early = runner.write_artifacts(eval_early)
        soak_json = json.loads(artifacts_early["soak_test"].read_text(encoding="utf-8"))
        assert soak_json["status"] == "FAIL"

        # Advance clock to 25 hours (past 24h threshold)
        clock.sleep(20 * 3600.0)
        assert runner.elapsed_seconds() >= 86400.0

        eval_full = runner.evaluate()
        assert eval_full.status == "PASS"
        assert eval_full.criteria["duration_24h_met"] is True
        assert eval_full.criteria["p0_success_rate_met"] is True

        artifacts_full = runner.write_artifacts(eval_full)
        soak_full = json.loads(artifacts_full["soak_test"].read_text(encoding="utf-8"))
        assert soak_full["status"] == "PASS"
        metrics_full = json.loads(artifacts_full["metrics"].read_text(encoding="utf-8"))
        assert metrics_full["status"] == "PASS"
        manifest_full = json.loads(artifacts_full["manifest"].read_text(encoding="utf-8"))
        assert manifest_full["status"] == "PASS"
        assert artifacts_full["mvp_acceptance"].is_file()
        md_text = artifacts_full["mvp_acceptance"].read_text(encoding="utf-8")
        assert "APPROVED (PASS)" in md_text


def test_criteria_computed_from_cycles() -> None:
    """V99 metrics and pass/fail criteria are computed strictly from real cycle records."""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        clock = ManualClock(_T0)
        runner = SoakRunner(
            artifacts_dir=base / "artifacts",
            docs_dir=base / "docs" / "decisions",
            database_path=base / "soak.sqlite",
            clock=clock,
            interval_minutes=15.0,
            target_hours=24.0,
        )

        # Inject 100 cycles directly into cycles.jsonl: 99 success, 1 failure
        runner.start_time()

        for idx in range(1, 101):
            is_success = idx != 42  # cycle 42 fails
            errors = ["synthetic network failure"] if not is_success else []
            per_source = {src: (is_success or src != "openalex") for src in AUTH04_SOURCES}
            rec = CycleRecord(
                timestamp=clock.now().isoformat(),
                run_id=f"v99-test-{idx:04d}",
                success=is_success,
                per_source_success=per_source,
                duplicates_written=0,
                cost=0.0,
                cost_usd=0.0,
                errors=errors,
                qa_results={"qa_pass": is_success},
                reports_published=1 if is_success else 0,
            )
            runner.append_cycle(rec)
            clock.sleep(900.0)  # 15 minutes per cycle -> 100 * 900s = 90,000s = 25h

        assert runner.elapsed_seconds() >= 86400.0
        eval_result = runner.evaluate()

        assert eval_result.cycle_count == 100
        assert eval_result.successful_cycles == 99
        assert eval_result.failed_cycles == 1
        assert eval_result.p0_success_rate == 0.99
        assert eval_result.status == "PASS"
        assert eval_result.criteria["p0_success_rate_met"] is True
        assert eval_result.criteria["duration_24h_met"] is True
        assert eval_result.duplicates_written == 0
        assert eval_result.total_cost_usd == 0.0
        assert eval_result.reports_published == 99

        # OpenAlex had 1 failure out of 100
        assert eval_result.per_source["openalex"]["successes"] == 99
        assert eval_result.per_source["openalex"]["rate"] == 0.99
        # Other sources had 100 successes
        assert eval_result.per_source["github"]["successes"] == 100
        assert eval_result.per_source["github"]["rate"] == 1.0


def test_criteria_fails_when_success_rate_below_99() -> None:
    """When success rate is below 99%, evaluation must be FAIL even after 24h."""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        clock = ManualClock(_T0)
        runner = SoakRunner(
            artifacts_dir=base / "artifacts",
            docs_dir=base / "docs" / "decisions",
            database_path=base / "soak.sqlite",
            clock=clock,
            interval_minutes=15.0,
            target_hours=24.0,
        )
        runner.start_time()

        # 95 successes, 5 failures -> 95% success rate < 99%
        for idx in range(1, 101):
            is_success = idx > 5
            rec = CycleRecord(
                timestamp=clock.now().isoformat(),
                run_id=f"v99-test-{idx:04d}",
                success=is_success,
                per_source_success={src: is_success for src in AUTH04_SOURCES},
                duplicates_written=0,
                cost=0.0,
                cost_usd=0.0,
                errors=["simulated failure"] if not is_success else [],
                qa_results={"qa_pass": is_success},
                reports_published=1 if is_success else 0,
            )
            runner.append_cycle(rec)
            clock.sleep(900.0)

        assert runner.elapsed_seconds() >= 86400.0
        eval_result = runner.evaluate()
        assert eval_result.status == "FAIL"
        assert eval_result.p0_success_rate == 0.95
        assert eval_result.criteria["p0_success_rate_met"] is False
        assert any("below threshold" in b for b in eval_result.blockers)


def test_failures_recorded_honestly() -> None:
    """Network failure is recorded in per-source success and cycle errors; never fakes success."""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        clock = ManualClock(_T0)
        runner = SoakRunner(
            artifacts_dir=base / "artifacts",
            docs_dir=base / "docs" / "decisions",
            database_path=base / "soak.sqlite",
            clock=clock,
            interval_minutes=60.0,
            fail_sources=["github"],  # Simulate GitHub collector network failure
        )

        record = runner.execute_cycle(1)
        assert record.success is False
        assert record.per_source_success["github"] is False
        assert record.per_source_success["openalex"] is True
        assert record.per_source_success["crossref"] is True
        assert record.per_source_success["arxiv"] is True
        assert record.per_source_success["hn"] is True
        assert any("github" in err for err in record.errors)

        # Verify appended to cycles.jsonl
        cycles = runner.load_cycles()
        assert len(cycles) == 1
        assert cycles[0].success is False
        assert cycles[0].per_source_success["github"] is False

        # Verify heartbeat file
        assert runner.heartbeat_file.is_file()
        heartbeat = json.loads(runner.heartbeat_file.read_text(encoding="utf-8"))
        assert heartbeat["last_cycle_status"] == "FAIL"
        assert heartbeat["cycle_count"] == 1


def test_resumes_safely_if_restarted() -> None:
    """Restarting the soak runner preserves recorded start time and cycle history."""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        clock = ManualClock(_T0)
        artifacts = base / "artifacts"
        docs = base / "docs" / "decisions"
        db = base / "soak.sqlite"

        runner1 = SoakRunner(
            artifacts_dir=artifacts,
            docs_dir=docs,
            database_path=db,
            clock=clock,
            interval_minutes=60.0,
        )
        rec1 = runner1.execute_cycle(1)
        clock.sleep(3600.0)
        rec2 = runner1.execute_cycle(2)
        clock.sleep(3600.0)

        start_time_1 = runner1.start_time()
        assert len(runner1.load_cycles()) == 2

        # Simulate process restart by creating a new runner instance on the same directory
        runner2 = SoakRunner(
            artifacts_dir=artifacts,
            docs_dir=docs,
            database_path=db,
            clock=clock,
            interval_minutes=60.0,
        )
        assert runner2.start_time() == start_time_1
        assert len(runner2.load_cycles()) == 2
        assert runner2.elapsed_seconds() == 7200.0

        # Execute cycle 3 on resumed runner
        rec3 = runner2.execute_cycle(3)
        assert rec3.run_id != rec2.run_id
        cycles = runner2.load_cycles()
        assert len(cycles) == 3
        assert [c.run_id for c in cycles] == [rec1.run_id, rec2.run_id, rec3.run_id]


def test_status_flag_output() -> None:
    """The status report formats running tallies, elapsed time, and per-source stats."""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        clock = ManualClock(_T0)
        runner = SoakRunner(
            artifacts_dir=base / "artifacts",
            docs_dir=base / "docs" / "decisions",
            database_path=base / "soak.sqlite",
            clock=clock,
            interval_minutes=60.0,
        )
        runner.execute_cycle(1)
        clock.sleep(3600.0)

        report = runner.status_report()
        assert "TechPulse-AI V99 Soak Runner Status" in report
        assert "IN PROGRESS" in report
        assert "PASS strictly prohibited before 24h" in report
        assert "Cycles Completed:   1" in report
        assert "openalex" in report
        assert "github" in report
