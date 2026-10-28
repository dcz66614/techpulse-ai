"""O20 local orchestrator: retry, DLQ, checkpoints, and replay.

Tests use tempfile. They do not open a socket, read an env file, or touch
the sealed retrieval set. The clock sleeps only inside ManualClock.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from techpulse.contracts import TaskStatus
from techpulse.workflows.activities import (
    PIPELINE_BINDINGS,
    RSS_BINDING,
    Activity,
    ActivityContext,
    ActivityResult,
    default_pipeline_activity,
    resolve_binding,
)
from techpulse.workflows.orchestrator import (
    IllegalTransition,
    LocalOrchestrator,
    ManualClock,
    backoff_seconds,
    daily_workflow,
    pipeline_task_ids,
    weekly_workflow,
)

_RUN = "550e8400-e29b-41d4-a716-446655440000"
_SLOT = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)


def _ok(context: ActivityContext, *, qa_pass: bool = True) -> ActivityResult:
    flag: bool | None = qa_pass if context.task_id == "Q10" else None
    return ActivityResult(
        status=TaskStatus.SUCCESS,
        records=(f"{context.task_id}:{context.input_sha256}",),
        qa_pass=flag,
        output_sha256=context.input_sha256,
    )


def _registry(activity: Activity) -> dict[str, Activity]:
    return {task_id: activity for task_id in pipeline_task_ids()}


def _open(root: Path, clock: ManualClock, activity: Activity, **extra: float) -> LocalOrchestrator:
    timeout = extra.get("timeout")
    return LocalOrchestrator(
        root / "o20.sqlite",
        clock=clock,
        activities=_registry(activity),
        timeout_seconds=timeout,
    )


def test_task_states_reject_illegal_transitions() -> None:
    """WAITING goes to READY, then RUNNING, then one terminal status."""
    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with LocalOrchestrator(Path(tmp) / "o20.sqlite", clock=clock) as orch:
            orch.open_task(_RUN, "C10")
            assert orch.task_status(_RUN, "C10") == "waiting"
            with pytest.raises(IllegalTransition):
                orch.transition(_RUN, "C10", TaskStatus.SUCCESS, reason="skip")
            assert orch.task_status(_RUN, "C10") == "waiting"
            orch.transition(_RUN, "C10", TaskStatus.READY, reason="ready")
            orch.transition(_RUN, "C10", TaskStatus.RUNNING, reason="start")
            orch.transition(_RUN, "C10", TaskStatus.SUCCESS, reason="done")
            with pytest.raises(IllegalTransition):
                orch.transition(_RUN, "C10", TaskStatus.RUNNING, reason="again")
            with pytest.raises(IllegalTransition):
                orch.transition(_RUN, "C10", TaskStatus.RETRYABLE_ERROR, reason="late")
            assert orch.task_status(_RUN, "C10") == "success"
            first = orch.audit()[0]
            orch.transition(_RUN, "C11", TaskStatus.WAITING, reason="queued")
            assert orch.audit()[0] == first


def test_backoff_matches_429_and_other_retryable_codes() -> None:
    """429 doubles from 30s. Other retryable waits are 30s then 120s."""
    assert backoff_seconds(1, code="http_429") == 30
    assert backoff_seconds(2, code="http_429") == 60
    assert backoff_seconds(1, code="http_429", retry_after=45) == 45
    assert backoff_seconds(1, code="http_429", jitter=0.5) == 30.5
    assert backoff_seconds(1, code="http_503") == 30
    assert backoff_seconds(2, code="http_503") == 120
    assert backoff_seconds(2, code="timeout") == 120


def test_bindings_name_the_existing_pipeline() -> None:
    """Daily and weekly flows use the same step ids as the pipeline modules."""
    assert pipeline_task_ids() == tuple(
        step.task_id for step in daily_workflow().steps if step.task_id != "publish"
    )
    assert tuple(step.task_id for step in weekly_workflow().steps) == tuple(
        step.task_id for step in daily_workflow().steps
    )
    assert set(pipeline_task_ids()) == set(PIPELINE_BINDINGS)
    for task_id in pipeline_task_ids():
        assert callable(resolve_binding(task_id))
    module_name, attr = RSS_BINDING
    rss = importlib.import_module(module_name)
    assert callable(getattr(rss, attr))


def test_daily_order_and_weekly_window() -> None:
    """Collectors run before D10. The weekly slot carries report_kind weekly."""
    seen: list[tuple[str, str]] = []

    def activity(context: ActivityContext) -> ActivityResult:
        seen.append((context.task_id, context.report_kind))
        return _ok(context)

    start = datetime(2026, 10, 26, tzinfo=UTC)
    assert start.weekday() == 0
    with TemporaryDirectory() as tmp:
        clock = ManualClock(start)
        with _open(Path(tmp), clock, activity) as orch:
            report = orch.simulate(start, hours=24)
    assert [task_id for task_id, kind in seen if kind == "daily"] == list(pipeline_task_ids())
    assert seen[0] == ("C10", "daily")
    assert ("C10", "weekly") in seen
    assert [item.workflow_id for item in report.runs] == [
        "techpulse-daily-v1",
        "techpulse-weekly-v1",
    ]
    assert report.runs[0].slot == "2026-10-26T08:00:00Z"
    assert report.runs[1].slot == "2026-10-26T09:00:00Z"


def test_fatal_error_is_never_retried() -> None:
    """A fatal result is one call. Replay does not call it again."""
    calls: list[int] = []

    def activity(context: ActivityContext) -> ActivityResult:
        if context.task_id == "C12":
            calls.append(context.attempt)
            return ActivityResult(
                status=TaskStatus.FATAL_ERROR,
                code="fatal_error",
                errors=("schema",),
            )
        return _ok(context)

    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with _open(Path(tmp), clock, activity) as orch:
            first = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert clock.slept == []
            again = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert calls == [1]
            assert again.activity_calls == 0
            snapshot = {task.task_id: task for task in first.tasks}
            assert snapshot["C12"].status == "fatal_error"
            assert snapshot["C12"].attempt == 1
            assert orch.dlq_count() == 1


def test_429_uses_exponential_backoff_and_stops_at_three() -> None:
    """Three attempts total. The sleeps after 429 are 30s then 60s."""
    calls: list[int] = []

    def activity(context: ActivityContext) -> ActivityResult:
        if context.task_id != "C10":
            return _ok(context)
        calls.append(context.attempt)
        if context.attempt < 3:
            return ActivityResult(
                status=TaskStatus.RETRYABLE_ERROR,
                code="http_429",
                retry_after=45 if context.attempt == 1 else None,
            )
        return _ok(context)

    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with _open(Path(tmp), clock, activity) as orch:
            orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert calls == [1, 2, 3]
            assert clock.slept == [45, 60]


def test_exhausted_429_lands_in_the_dlq() -> None:
    """A third 429 is not followed by another attempt."""
    calls: list[int] = []

    def activity(context: ActivityContext) -> ActivityResult:
        if context.task_id == "C11":
            calls.append(context.attempt)
            return ActivityResult(status=TaskStatus.RETRYABLE_ERROR, code="http_429")
        return _ok(context)

    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with _open(Path(tmp), clock, activity) as orch:
            report = orch.execute("techpulse-daily-v1", slot=_SLOT)
            replay = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert calls == [1, 2, 3]
            assert clock.slept == [30, 60]
            assert report.dlq == 1
            assert replay.activity_calls == 0
            assert orch.task_status(report.run_id, "C11") == "retryable_error"


def test_timeout_is_retried_then_dead_lettered() -> None:
    """An activity that uses the whole timeout is retryable, then DLQ at attempt 3."""
    calls: list[str] = []

    def activity(context: ActivityContext) -> ActivityResult:
        if context.task_id == "C10":
            calls.append(context.task_id)
            context.clock.sleep(5)
        return _ok(context)

    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with _open(Path(tmp), clock, activity, timeout=5) as orch:
            report = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert calls == ["C10", "C10", "C10"]
            assert clock.slept == [5, 30, 5, 120, 5]
            assert orch.task_status(report.run_id, "C10") == "retryable_error"
            assert report.dlq == 1
            assert not any(
                task.task_id == "C10" and task.status == "success" for task in report.tasks
            )


def test_policy_blocked_disables_the_source_immediately() -> None:
    """The next collector sees the source disabled. A later slot does not call it."""
    calls: list[str] = []
    holder: list[LocalOrchestrator] = []

    def activity(context: ActivityContext) -> ActivityResult:
        calls.append(context.task_id)
        if context.task_id == "C10":
            return ActivityResult(
                status=TaskStatus.POLICY_BLOCKED,
                code="http_403",
                source_id="openalex",
            )
        if context.task_id == "C11":
            assert holder[0].source_enabled("openalex") is False
        return _ok(context)

    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with _open(Path(tmp), clock, activity) as orch:
            holder.append(orch)
            first = orch.execute("techpulse-daily-v1", slot=_SLOT)
            tuesday = _SLOT + timedelta(days=1)
            second = orch.execute("techpulse-daily-v1", slot=tuesday)
            assert orch.source_enabled("openalex") is False
            assert calls.count("C10") == 1
            assert "C11" in calls
            assert second.activity_calls == len(pipeline_task_ids()) - 1
            assert orch.task_status(first.run_id, "C10") == "policy_blocked"
            assert orch.task_status(second.run_id, "C10") == "policy_blocked"


def test_reports_publish_only_after_q10_qa_pass() -> None:
    """Boolean true publishes one row. False and replay do not add another."""

    def activity(context: ActivityContext) -> ActivityResult:
        return _ok(context, qa_pass=False)

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        clock = ManualClock(_SLOT)
        with _open(root, clock, activity) as orch:
            refused = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert refused.published == 0
            assert orch.published_count() == 0
            assert orch.task_status(refused.run_id, "publish") == "success"
        clock = ManualClock(_SLOT)
        with _open(root / "pass", clock, _ok) as orch:
            passed = orch.execute("techpulse-daily-v1", slot=_SLOT)
            replay = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert passed.published == 1
            assert replay.published == 0
            assert orch.published_count() == 1


def test_simulated_24h_replay_writes_no_duplicate_records() -> None:
    """Replaying the same 24h window inserts nothing and reports 0 duplicates."""
    start = datetime(2026, 10, 26, tzinfo=UTC)
    assert start.weekday() == 0

    def activity(context: ActivityContext) -> ActivityResult:
        return _ok(context)

    with TemporaryDirectory() as tmp:
        clock = ManualClock(start)
        with _open(Path(tmp), clock, activity) as orch:
            first = orch.simulate(start, hours=24)
            stored = orch.record_count()
            published = orch.published_count()
            replay = orch.simulate(start, hours=24)
            assert first.duplicate_records == 0
            assert first.records_inserted == stored
            assert first.records_inserted > 0
            assert first.published == 2
            assert published == 2
            assert replay.duplicate_records == 0
            assert replay.records_inserted == 0
            assert replay.activity_calls == 0
            assert replay.published == 0
            assert orch.record_count() == stored
            assert orch.published_count() == published


def test_replay_checks_input_sha256_before_run_id() -> None:
    """The same payload under a new run id does not call activities or insert rows."""
    calls: list[str] = []

    def activity(context: ActivityContext) -> ActivityResult:
        calls.append(context.run_id)
        return _ok(context)

    payload = b'{"fixture":"same"}'
    monday = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
    tuesday = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
    with TemporaryDirectory() as tmp:
        clock = ManualClock(monday)
        with _open(Path(tmp), clock, activity) as orch:
            first = orch.execute("techpulse-daily-v1", slot=monday, payload=payload)
            second = orch.execute("techpulse-daily-v1", slot=tuesday, payload=payload)
            assert first.run_id != second.run_id
            assert second.activity_calls == 0
            assert second.records_inserted == 0
            assert second.duplicate_records == 0
            assert second.published == 0
            assert orch.record_count() == first.records_inserted
            assert orch.published_count() == 1
            assert len(calls) == len(pipeline_task_ids())


def test_scheduler_fault_keeps_the_queue() -> None:
    """An activity OSError pauses new slots. Resume finishes the same attempt's successor."""
    failed = {"once": False}
    calls: list[str] = []

    def activity(context: ActivityContext) -> ActivityResult:
        calls.append(context.task_id)
        if context.task_id == "C10" and not failed["once"]:
            failed["once"] = True
            raise OSError("disk full")
        return _ok(context)

    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with _open(Path(tmp), clock, activity) as orch:
            failed_run = orch.execute("techpulse-daily-v1", slot=_SLOT)
            checkpoints = orch.checkpoint_count()
            assert failed_run.status == "paused"
            assert orch.paused()
            assert "scheduler fault" in orch.pause_reason()
            assert orch.task_status(failed_run.run_id, "C10") == "running"
            blocked = orch.execute("techpulse-weekly-v1", slot=_SLOT + timedelta(hours=1))
            assert blocked.status == "paused"
            assert blocked.activity_calls == 0
            orch.resume()
            resumed = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert resumed.status == "success"
            assert orch.checkpoint_count() >= checkpoints
            assert calls.count("C10") == 2
            assert resumed.records_inserted > 0


def test_daily_budget_stops_new_triggers() -> None:
    """At $25 the scheduler pauses and does not call an activity."""
    calls: list[str] = []

    def activity(context: ActivityContext) -> ActivityResult:
        calls.append(context.task_id)
        return _ok(context)

    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with _open(Path(tmp), clock, activity) as orch:
            orch.note_spend(25)
            report = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert calls == []
            assert report.activity_calls == 0
            assert report.status == "paused"
            assert orch.pause_reason() == "daily budget"


def test_task_budget_above_five_dollars_is_fatal() -> None:
    """A success that costs more than $5 is fatal and is not retried."""
    calls: list[int] = []

    def activity(context: ActivityContext) -> ActivityResult:
        if context.task_id == "C10":
            calls.append(context.attempt)
            return ActivityResult(
                status=TaskStatus.SUCCESS,
                cost_usd=6,
                records=("spent",),
                output_sha256=context.input_sha256,
            )
        return _ok(context)

    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with _open(Path(tmp), clock, activity) as orch:
            report = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert calls == [1]
            assert orch.task_status(report.run_id, "C10") == "fatal_error"
            assert "C11" in {task.task_id for task in report.tasks if task.status == "success"}


def test_non_retryable_code_is_not_retried() -> None:
    """schema_incompatible stays fatal even when the activity labels it retryable."""
    calls: list[int] = []

    def activity(context: ActivityContext) -> ActivityResult:
        if context.task_id == "D20":
            calls.append(context.attempt)
            return ActivityResult(
                status=TaskStatus.RETRYABLE_ERROR,
                code="schema_incompatible",
            )
        return _ok(context)

    with TemporaryDirectory() as tmp:
        clock = ManualClock(_SLOT)
        with _open(Path(tmp), clock, activity) as orch:
            report = orch.execute("techpulse-daily-v1", slot=_SLOT)
            assert calls == [1]
            assert clock.slept == []
            assert orch.task_status(report.run_id, "D20") == "fatal_error"
            assert orch.task_status(report.run_id, "D21") == "waiting"


def test_default_d30_activity_calls_dedup_and_collectors_do_not_dial() -> None:
    """The default activity can dedup local rows. Collectors without a fixture stop."""
    clock = ManualClock(_SLOT)
    deadline = clock.now() + timedelta(seconds=5)
    rows: list[dict[str, object]] = [
        {"source_id": "openalex", "source_record_id": "a", "doi": "10.1000/abc123"},
        {"source_id": "crossref", "source_record_id": "b", "doi": "10.1000/abc123"},
    ]
    context = ActivityContext(
        task_id="D30",
        run_id=_RUN,
        attempt=1,
        input_sha256="a" * 64,
        report_kind="daily",
        slot="2026-10-26T08:00:00Z",
        deadline=deadline,
        clock=clock,
        params={"records": rows},
        upstream={},
    )
    result = default_pipeline_activity(context)
    assert result.status is TaskStatus.SUCCESS
    assert result.records == ("openalex:a", "crossref:b")
    collector = ActivityContext(
        task_id="C10",
        run_id=_RUN,
        attempt=1,
        input_sha256="b" * 64,
        report_kind="daily",
        slot="2026-10-26T08:00:00Z",
        deadline=deadline,
        clock=clock,
        params={},
        upstream={},
    )
    blocked = default_pipeline_activity(collector)
    assert blocked.status is TaskStatus.FATAL_ERROR
    assert blocked.code == "fatal_error"


def test_registry_shape_is_used_by_workflows() -> None:
    """Both flows reference every binding. Publish is the gate, not a pipeline module."""
    binding_ids = set(PIPELINE_BINDINGS)
    assert "publish" not in binding_ids
    assert binding_ids <= {step.task_id for step in daily_workflow().steps}
    weekly_ids = {step.task_id for step in weekly_workflow().steps}
    assert binding_ids <= weekly_ids
    groups = {step.group for step in daily_workflow().steps}
    assert groups == {"collectors", "cleaners", "inference", "deployment"}
