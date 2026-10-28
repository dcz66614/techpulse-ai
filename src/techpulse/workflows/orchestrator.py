"""Local workflow runtime for daily and weekly report flows.

There is no Temporal server in this process. ``WorkflowRuntime`` is the
small surface a Temporal worker can implement later. ``LocalOrchestrator``
is the in-process implementation: SQLite checkpoints, a DLQ, three attempts,
and replay that looks up ``input_sha256`` before it calls an activity.

The O30 budget hook records spend on the same SQLite file. At $25 it opens
the circuit breaker and this runtime pauses. The hook cannot be turned off.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from techpulse.contracts import TaskStatus
from techpulse.ops.metrics import BudgetHook
from techpulse.workflows.activities import (
    PIPELINE_BINDINGS,
    Activity,
    ActivityContext,
    ActivityResult,
    ActivityTimeout,
    Clock,
    default_pipeline_activity,
    task_idempotency_key,
)

MAX_ATTEMPTS = 3
ACTIVITY_TIMEOUT_SECONDS = 3600.0
TASK_MAX_USD = 5.0
DAILY_MAX_USD = 25.0
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_SECOND_SECONDS = 120.0
PARALLELISM: dict[str, int] = {
    "collectors": 5,
    "cleaners": 3,
    "inference": 2,
    "deployment": 1,
}
_RUN_NAMESPACE = uuid.UUID("6f2c8a1e-0b4d-4e5f-9a7c-1d2e3f4a5b6c")
_POLICY_CODES = frozenset({"http_401", "http_403", "captcha", "tos_block", "policy_blocked"})
_FATAL_CODES = frozenset({"schema_incompatible", "permission_violation", "fatal_error", "budget"})
_TERMINAL = frozenset({TaskStatus.SUCCESS, TaskStatus.FATAL_ERROR, TaskStatus.POLICY_BLOCKED})
_ALLOWED: dict[TaskStatus | None, frozenset[TaskStatus]] = {
    None: frozenset({TaskStatus.WAITING}),
    TaskStatus.WAITING: frozenset({TaskStatus.READY}),
    TaskStatus.READY: frozenset({TaskStatus.RUNNING}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCESS,
            TaskStatus.RETRYABLE_ERROR,
            TaskStatus.FATAL_ERROR,
            TaskStatus.POLICY_BLOCKED,
        }
    ),
    TaskStatus.RETRYABLE_ERROR: frozenset({TaskStatus.READY}),
    TaskStatus.SUCCESS: frozenset(),
    TaskStatus.FATAL_ERROR: frozenset(),
    TaskStatus.POLICY_BLOCKED: frozenset(),
}
_SOURCES: tuple[tuple[str, str, str, int], ...] = (
    ("openalex", "academic_papers", "https://api.openalex.org", 1),
    ("crossref", "academic_papers", "https://api.crossref.org", 1),
    ("arxiv", "academic_papers", "https://export.arxiv.org/api/query", 1),
    ("github", "code_repositories", "https://api.github.com", 1),
    ("hn", "community_discussions", "https://hacker-news.firebaseio.com/v0/", 1),
    ("rss", "syndicated_feeds", "not stated", 0),
    ("sec", "regulatory_filings", "https://data.sec.gov", 0),
    ("cninfo", "regulatory_filings", "http://webapi.cninfo.com.cn", 0),
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_registry (
    source_id TEXT PRIMARY KEY,
    source_family TEXT NOT NULL,
    base_url TEXT NOT NULL,
    policy_snapshot_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    requests_per_minute INTEGER NOT NULL DEFAULT 10,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS published_reports (
    report_id TEXT PRIMARY KEY,
    report_kind TEXT NOT NULL,
    content_uri TEXT NOT NULL,
    qa_manifest_uri TEXT NOT NULL,
    evidence_snapshot_uri TEXT NOT NULL,
    published_at TEXT NOT NULL,
    generated_by_ai INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS o20_task_state (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    input_sha256 TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id)
);
CREATE TABLE IF NOT EXISTS o20_checkpoints (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    result_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS o20_audit (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS o20_dlq (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    input_sha256 TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS o20_results (
    task_id TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    run_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (task_id, input_sha256)
);
CREATE TABLE IF NOT EXISTS o20_records (
    task_id TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    record_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    PRIMARY KEY (task_id, input_sha256, record_key)
);
CREATE TABLE IF NOT EXISTS o20_fires (
    workflow_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    run_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (workflow_id, slot)
);
CREATE TABLE IF NOT EXISTS o20_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS o20_budget (
    day TEXT PRIMARY KEY,
    spent_usd REAL NOT NULL
);
"""


class IllegalTransition(ValueError):
    """A task status change outside the allowed graph."""

    def __init__(self, src: str | None, dst: str) -> None:
        super().__init__(f"illegal transition {src} -> {dst}")
        self.src = src
        self.dst = dst


class WorkflowRuntime(Protocol):
    """Temporal-shaped entry points. The local class is the only implementation."""

    def execute(
        self,
        workflow_id: str,
        *,
        slot: datetime,
        run_id: str | None = None,
        payload: bytes | None = None,
    ) -> RunReport: ...

    def simulate(self, start: datetime, *, hours: float = 24) -> Simulation: ...


@dataclass(frozen=True)
class Step:
    """One node in a report flow."""

    task_id: str
    depends: tuple[str, ...]
    source_id: str | None = None
    collector: bool = False
    group: str = "inference"
    timeout_seconds: float = ACTIVITY_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Workflow:
    """A scheduled report flow."""

    workflow_id: str
    report_kind: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class TaskState:
    """Current checkpoint for one task in one run."""

    status: TaskStatus
    attempt: int
    input_sha256: str
    idempotency_key: str
    result: Mapping[str, object]


@dataclass(frozen=True)
class TaskSnapshot:
    """Task state copied onto a run report."""

    task_id: str
    status: str
    attempt: int
    input_sha256: str


@dataclass(frozen=True)
class RunReport:
    """Outcome of one workflow slot."""

    workflow_id: str
    run_id: str
    slot: str
    status: str
    records_inserted: int
    duplicate_records: int
    skipped: int
    published: int
    dlq: int
    activity_calls: int
    tasks: tuple[TaskSnapshot, ...]


@dataclass(frozen=True)
class Simulation:
    """Every slot inside one window."""

    start: str
    end: str
    runs: tuple[RunReport, ...]
    records_inserted: int
    duplicate_records: int
    published: int
    skipped: int
    dlq: int
    activity_calls: int


@dataclass(frozen=True)
class _Cached:
    status: TaskStatus
    result: Mapping[str, object]


class SystemClock:
    """Wall clock. ``advance`` refuses, so a simulated day cannot sleep for real."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def advance(self, seconds: float) -> None:
        raise RuntimeError("refusing to skip a live clock")


class ManualClock:
    """Clock that moves only when ``sleep`` or ``advance`` is called."""

    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("clock requires a timezone")
        self._now = now.astimezone(UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("advance seconds must be >= 0")
        self._now += timedelta(seconds=seconds)


def assert_transition(src: TaskStatus | None, dst: TaskStatus) -> None:
    """Raise ``IllegalTransition`` when ``dst`` is not allowed after ``src``."""
    allowed = _ALLOWED[src]
    if dst not in allowed:
        src_text = None if src is None else src.value
        raise IllegalTransition(src_text, dst.value)


def backoff_seconds(
    attempt: int,
    *,
    code: str,
    retry_after: float | None = None,
    jitter: float = 0.0,
) -> float:
    """Seconds to wait after a failed attempt. Attempt 1 is the first failure.

    HTTP 429 uses 30 * 2^(attempt-1), and at least ``retry_after`` when that
    value is positive. Other retryable failures wait 30s then 120s.
    """
    if attempt < 1:
        raise ValueError("attempt must be positive")
    if jitter < 0:
        raise ValueError("jitter must be >= 0")
    if code == "http_429":
        delay = BACKOFF_BASE_SECONDS * float(2 ** (attempt - 1))
        if retry_after is not None and retry_after > delay:
            delay = retry_after
    elif attempt == 1:
        delay = BACKOFF_BASE_SECONDS
    else:
        delay = BACKOFF_SECOND_SECONDS
    return delay + jitter


def classify_result(result: ActivityResult) -> ActivityResult:
    """Map an activity result onto a status the runtime is allowed to store."""
    if result.cost_usd < 0 or (
        result.status is TaskStatus.SUCCESS and result.cost_usd > TASK_MAX_USD
    ):
        errors = result.errors + ("budget exceeded",)
        return replace(result, status=TaskStatus.FATAL_ERROR, code="budget", errors=errors)
    if result.status is TaskStatus.SUCCESS:
        return result
    if result.status is TaskStatus.FATAL_ERROR:
        return result
    if result.status is TaskStatus.POLICY_BLOCKED or result.code in _POLICY_CODES:
        code = result.code or "policy_blocked"
        return replace(result, status=TaskStatus.POLICY_BLOCKED, code=code)
    if result.code in _FATAL_CODES:
        return replace(result, status=TaskStatus.FATAL_ERROR, code=result.code)
    if result.status is TaskStatus.RETRYABLE_ERROR:
        return result
    errors = result.errors + ("illegal activity status",)
    return replace(result, status=TaskStatus.FATAL_ERROR, code="fatal_error", errors=errors)


def run_id_for(workflow_id: str, slot: str) -> str:
    """Stable UUID for one workflow slot. A replay of the slot keeps it."""
    return str(uuid.uuid5(_RUN_NAMESPACE, f"{workflow_id}|{slot}"))


def scheduled_payload(workflow_id: str, slot: datetime, report_kind: str) -> bytes:
    """Canonical bytes hashed into every step of a scheduled run."""
    windows = [7, 28] if report_kind == "weekly" else [1]
    body = {
        "report_kind": report_kind,
        "slot": _iso(slot),
        "windows": windows,
        "workflow_id": workflow_id,
    }
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")


def iter_slots(start: datetime, end: datetime) -> tuple[tuple[str, datetime], ...]:
    """Daily 08:00 UTC and Monday 09:00 UTC slots in ``[start, end)``."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("window requires a timezone")
    slots: list[tuple[str, datetime]] = []
    day = start.astimezone(UTC).date()
    last = end.astimezone(UTC).date()
    while day <= last:
        daily = datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC)
        weekly = datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC)
        if start <= daily < end:
            slots.append(("techpulse-daily-v1", daily))
        if day.weekday() == 0 and start <= weekly < end:
            slots.append(("techpulse-weekly-v1", weekly))
        day += timedelta(days=1)
    slots.sort(key=lambda item: (item[1], item[0]))
    return tuple(slots)


def daily_workflow() -> Workflow:
    """Section 12.1 chain. Collectors share no edges. The tail is serial."""
    return Workflow("techpulse-daily-v1", "daily", _report_steps())


def weekly_workflow() -> Workflow:
    """Monday chain. Same steps as the daily flow, with 7-day and 28-day windows."""
    return Workflow("techpulse-weekly-v1", "weekly", _report_steps())


def pipeline_task_ids() -> tuple[str, ...]:
    """Task ids the report flows hand to existing pipeline callables."""
    return tuple(step.task_id for step in _report_steps() if step.task_id != "publish")


def _report_steps() -> tuple[Step, ...]:
    collectors = (
        ("C10", "openalex"),
        ("C11", "crossref"),
        ("C12", "arxiv"),
        ("C20", "github"),
        ("C21", "hn"),
    )
    steps = [
        Step(
            task_id,
            (),
            source_id=source_id,
            collector=True,
            group="collectors",
        )
        for task_id, source_id in collectors
    ]
    tail = (
        ("D10", ("C10", "C11", "C12", "C20", "C21"), "cleaners"),
        ("D20", ("D10",), "cleaners"),
        ("D21", ("D20",), "cleaners"),
        ("D30", ("D21",), "cleaners"),
        ("D40", ("D30",), "cleaners"),
        ("I10", ("D40", "D21"), "inference"),
        ("I20", ("I10",), "inference"),
        ("T10", ("I20", "D40"), "inference"),
        ("T20", ("T10",), "inference"),
        ("T30", ("T20",), "inference"),
        ("E10", ("T30", "I20"), "inference"),
        ("E20", ("E10",), "inference"),
        ("R10", ("E20",), "inference"),
        ("Q10", ("R10", "E20"), "inference"),
        ("publish", ("Q10",), "deployment"),
    )
    for task_id, depends, group in tail:
        steps.append(Step(task_id, depends, group=group))
    known = {step.task_id for step in steps}
    for step in steps:
        for dep in step.depends:
            if dep not in known:
                raise ValueError(f"{step.task_id} depends on unknown {dep}")
        cap = PARALLELISM.get(step.group)
        if cap is None:
            raise ValueError(f"unknown group {step.group}")
    return tuple(steps)


class LocalOrchestrator:
    """In-process runtime. One SQLite file holds checkpoints, the DLQ, and records."""

    def __init__(
        self,
        database: Path,
        *,
        clock: Clock | None = None,
        activities: Mapping[str, Activity] | None = None,
        timeout_seconds: float | None = None,
        jitter: Callable[[int, str], float] | None = None,
    ) -> None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._database = database
        self._clock = SystemClock() if clock is None else clock
        self._activities = dict(activities or {})
        self._timeout = timeout_seconds
        self._jitter = jitter if jitter is not None else _zero_jitter
        self._workflows = {
            daily_workflow().workflow_id: daily_workflow(),
            weekly_workflow().workflow_id: weekly_workflow(),
        }
        self._steps = {
            workflow.workflow_id: {step.task_id: step for step in workflow.steps}
            for workflow in self._workflows.values()
        }
        database.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(database)
        self._conn.isolation_level = None
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO o20_control (id, paused, reason) VALUES (1, 0, '')"
        )
        self._seed_sources()
        self._reset_counters()
        self._held: set[str] = set()
        try:
            self._budget_hook = BudgetHook(self._conn)
        except Exception:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> LocalOrchestrator:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def execute(
        self,
        workflow_id: str,
        *,
        slot: datetime,
        run_id: str | None = None,
        payload: bytes | None = None,
    ) -> RunReport:
        """Run one slot, or resume it from the checkpoints already stored."""
        workflow = self._workflows.get(workflow_id)
        if workflow is None:
            raise ValueError(f"unknown workflow {workflow_id}")
        if slot.tzinfo is None:
            raise ValueError("slot requires a timezone")
        slot_text = _iso(slot)
        self._reset_counters()
        self._held.clear()
        if self.paused():
            return self._report(workflow, "", slot_text, "paused")
        body = (
            payload
            if payload is not None
            else scheduled_payload(workflow_id, slot, workflow.report_kind)
        )
        chosen = run_id if run_id is not None else run_id_for(workflow_id, slot_text)
        stored = self._fire(workflow_id, slot_text, chosen, body)
        self._run(workflow, stored[0], stored[1])
        status = "paused" if self.paused() else self._run_status(workflow, stored[0])
        return self._report(workflow, stored[0], slot_text, status)

    def simulate(self, start: datetime, *, hours: float = 24) -> Simulation:
        """Fire every daily and weekly slot in a window. The clock jumps between slots."""
        if hours <= 0:
            raise ValueError("hours must be positive")
        if start.tzinfo is None:
            raise ValueError("start requires a timezone")
        end = start + timedelta(hours=hours)
        runs: list[RunReport] = []
        for workflow_id, slot in iter_slots(start, end):
            self._advance_to(slot)
            if self.paused():
                runs.append(self._idle_report(self._workflows[workflow_id], _iso(slot)))
                break
            runs.append(self.execute(workflow_id, slot=slot))
        return Simulation(
            start=_iso(start),
            end=_iso(end),
            runs=tuple(runs),
            records_inserted=sum(item.records_inserted for item in runs),
            duplicate_records=sum(item.duplicate_records for item in runs),
            published=sum(item.published for item in runs),
            skipped=sum(item.skipped for item in runs),
            dlq=sum(item.dlq for item in runs),
            activity_calls=sum(item.activity_calls for item in runs),
        )

    def pause(self, reason: str) -> None:
        """Stop new triggers. Checkpoints and the queue stay."""
        self._conn.execute("UPDATE o20_control SET paused = 1, reason = ? WHERE id = 1", (reason,))

    def resume(self) -> None:
        """Allow triggers again. Unfinished tasks keep their idempotency keys."""
        self._conn.execute("UPDATE o20_control SET paused = 0, reason = '' WHERE id = 1")

    def paused(self) -> bool:
        row = self._conn.execute("SELECT paused, reason FROM o20_control WHERE id = 1").fetchone()
        if row is None:
            return True
        return _bit(row["paused"])

    def pause_reason(self) -> str:
        row = self._conn.execute("SELECT reason FROM o20_control WHERE id = 1").fetchone()
        if row is None:
            return ""
        return _cell_text(row, "reason")

    def note_spend(self, amount: float) -> None:
        """Add already-spent dollars for the clock's UTC day."""
        if amount < 0:
            raise ValueError("amount must be >= 0")
        self._charge(amount)
        self._budget_hook.add_spend(amount, day=self._day())

    def budget_mode(self) -> str:
        """O30 mode for the clock's UTC day: normal, degraded, stopped, or fail_closed."""
        return self._budget_hook.decision(day=self._day()).mode

    def llm_allowed(
        self,
        *,
        priority: str,
        uses_llm: bool,
        tier: int = 1,
        essential: bool = True,
    ) -> bool:
        """Ask the O30 gate whether this LLM call may run."""
        decision = self._budget_hook.decision(day=self._day())
        return decision.allow(
            priority=priority,
            uses_llm=uses_llm,
            tier=tier,
            essential=essential,
        )

    def set_metrics_available(self, available: bool) -> None:
        """Mark the O30 metrics store up or down. The budgeter stays locked."""
        self._budget_hook.set_metrics_available(available)

    def source_enabled(self, source_id: str) -> bool:
        row = self._conn.execute(
            "SELECT enabled FROM source_registry WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return False
        return _bit(row["enabled"])

    def record_count(self) -> int:
        return _count(self._conn, "o20_records")

    def published_count(self) -> int:
        return _count(self._conn, "published_reports")

    def dlq_count(self) -> int:
        return _count(self._conn, "o20_dlq")

    def checkpoint_count(self) -> int:
        return _count(self._conn, "o20_checkpoints")

    def task_status(self, run_id: str, task_id: str) -> str | None:
        state = self._load(run_id, task_id)
        if state is None:
            return None
        return state.status.value

    def open_task(self, run_id: str, task_id: str) -> None:
        """Create a task at ``waiting`` when it is not already stored."""
        if self._load(run_id, task_id) is not None:
            return
        self.transition(run_id, task_id, TaskStatus.WAITING, reason="queued", attempt=1)

    def transition(
        self,
        run_id: str,
        task_id: str,
        dst: TaskStatus,
        *,
        reason: str,
        attempt: int | None = None,
        input_sha256: str | None = None,
        idempotency_key: str | None = None,
        result: Mapping[str, object] | None = None,
    ) -> None:
        """Move one task and append an audit row. Illegal moves change nothing."""
        current = self._load(run_id, task_id)
        src = None if current is None else current.status
        assert_transition(src, dst)
        if dst is not TaskStatus.WAITING and current is None:
            raise IllegalTransition(None, dst.value)
        next_attempt = current.attempt if current is not None else 1
        if attempt is not None:
            next_attempt = attempt
        digest = current.input_sha256 if current is not None else ""
        if input_sha256 is not None:
            digest = input_sha256
        key = current.idempotency_key if current is not None else ""
        if idempotency_key is not None:
            key = idempotency_key
        stored = dict(current.result) if current is not None else {}
        if result is not None:
            stored = dict(result)
        when = _iso(self._clock.now())
        payload = _json(stored)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if current is None:
                self._conn.execute(
                    """
                    INSERT INTO o20_task_state (
                        run_id, task_id, status, attempt, input_sha256,
                        idempotency_key, result_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, task_id, dst.value, next_attempt, digest, key, payload, when),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE o20_task_state
                    SET status = ?, attempt = ?, input_sha256 = ?, idempotency_key = ?,
                        result_json = ?, updated_at = ?
                    WHERE run_id = ? AND task_id = ?
                    """,
                    (dst.value, next_attempt, digest, key, payload, when, run_id, task_id),
                )
            self._conn.execute(
                """
                INSERT INTO o20_checkpoints (
                    run_id, task_id, attempt, status, input_sha256, idempotency_key,
                    reason, result_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, task_id, next_attempt, dst.value, digest, key, reason, payload, when),
            )
            self._conn.execute(
                """
                INSERT INTO o20_audit (
                    run_id, task_id, from_status, to_status, attempt, reason, at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    None if src is None else src.value,
                    dst.value,
                    next_attempt,
                    reason,
                    when,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def audit(self) -> tuple[tuple[str | None, str, str, str], ...]:
        """Append-only status changes as ``(from_status, to_status, task_id, reason)``."""
        rows = self._conn.execute(
            """
            SELECT from_status, to_status, task_id, reason
            FROM o20_audit ORDER BY id
            """
        ).fetchall()
        found: list[tuple[str | None, str, str, str]] = []
        for row in rows:
            src = row["from_status"]
            src_text = src if isinstance(src, str) or src is None else None
            found.append(
                (
                    src_text,
                    _cell_text(row, "to_status"),
                    _cell_text(row, "task_id"),
                    _cell_text(row, "reason"),
                )
            )
        return tuple(found)

    def write_o20_artifacts(
        self,
        artifacts_dir: Path,
        run_id: str,
        first: Simulation,
        replay: Simulation,
    ) -> Path:
        """Write ``artifacts/{run_id}/O20`` manifest, metrics, and log."""
        checked = _artifact_run_id(run_id)
        directory = artifacts_dir / checked / "O20"
        directory.mkdir(parents=True, exist_ok=True)
        metrics: dict[str, object] = {
            "activity_calls": first.activity_calls,
            "activity_timeout_seconds": ACTIVITY_TIMEOUT_SECONDS,
            "daily_budget_usd": DAILY_MAX_USD,
            "dlq": self.dlq_count(),
            "duplicate_records": replay.duplicate_records,
            "max_attempts": MAX_ATTEMPTS,
            "network_calls": 0,
            "parallelism": PARALLELISM,
            "published": self.published_count(),
            "records": self.record_count(),
            "records_inserted": first.records_inserted,
            "replay_activity_calls": replay.activity_calls,
            "replay_records_inserted": replay.records_inserted,
            "run_id": checked,
            "status": "success" if replay.duplicate_records == 0 else "failed",
            "task_budget_usd": TASK_MAX_USD,
            "temporal_server": False,
            "third_party_writes": 0,
            "ticket": "O20",
        }
        log_lines = [
            json.dumps(
                {
                    "at": _cell_text(row, "at"),
                    "event": "status",
                    "from": row["from_status"] if isinstance(row["from_status"], str) else None,
                    "reason": _cell_text(row, "reason"),
                    "run_id": _cell_text(row, "run_id"),
                    "task_id": _cell_text(row, "task_id"),
                    "to": _cell_text(row, "to_status"),
                },
                sort_keys=True,
            )
            for row in self._conn.execute(
                """
                SELECT run_id, task_id, from_status, to_status, reason, at
                FROM o20_audit ORDER BY id
                """
            ).fetchall()
        ]
        log_body = "".join(f"{line}\n" for line in log_lines)
        _write(directory / "metrics.json", _pretty(metrics))
        _write(directory / "log.jsonl", log_body)
        manifest = {
            "bindings": {key: list(value) for key, value in sorted(PIPELINE_BINDINGS.items())},
            "duplicate_records": replay.duplicate_records,
            "errors": [],
            "files": [
                _file_entry(directory / "metrics.json", checked),
                _file_entry(directory / "log.jsonl", checked),
            ],
            "replay_records_inserted": replay.records_inserted,
            "run_id": checked,
            "slots": [item.slot for item in first.runs],
            "status": metrics["status"],
            "ticket": "O20",
            "workflows": ["techpulse-daily-v1", "techpulse-weekly-v1"],
        }
        _write(directory / "manifest.json", _pretty(manifest))
        return directory

    def _run(self, workflow: Workflow, run_id: str, payload: bytes) -> None:
        root = hashlib.sha256(payload).hexdigest()
        seen: set[str] = set()
        steps = workflow.steps
        while not self.paused():
            progressed = False
            for step in steps:
                if step.task_id in seen or step.task_id in self._held:
                    continue
                self.open_task(run_id, step.task_id)
                state = self._require(run_id, step.task_id)
                if state.status is TaskStatus.SUCCESS:
                    self._skipped += 1
                    seen.add(step.task_id)
                    continue
                if self._finished(step, state):
                    seen.add(step.task_id)
                    continue
                if not self._deps_ready(workflow, run_id, step):
                    continue
                self._drive(workflow, run_id, step, root)
                if step.task_id in self._held:
                    continue
                progressed = True
                current = self._require(run_id, step.task_id)
                if current.status is TaskStatus.SUCCESS or self._finished(step, current):
                    seen.add(step.task_id)
                if self.paused():
                    return
            if not progressed:
                return

    def _drive(self, workflow: Workflow, run_id: str, step: Step, root: str) -> None:
        for _ in range(MAX_ATTEMPTS + 3):
            if self.paused():
                return
            state = self._require(run_id, step.task_id)
            if state.status is TaskStatus.WAITING:
                self.transition(
                    run_id, step.task_id, TaskStatus.READY, reason="dependencies satisfied"
                )
                continue
            if state.status is TaskStatus.READY:
                self._start_or_skip(workflow, run_id, step, root, state)
                if self.paused():
                    return
                if self._require(run_id, step.task_id).status is not TaskStatus.RETRYABLE_ERROR:
                    return
                continue
            if state.status is TaskStatus.RUNNING:
                self._resume_running(workflow, run_id, step, root, state)
                if self.paused():
                    return
                if self._require(run_id, step.task_id).status is not TaskStatus.RETRYABLE_ERROR:
                    return
                continue
            if state.status is TaskStatus.RETRYABLE_ERROR:
                if state.attempt >= MAX_ATTEMPTS:
                    return
                code = _text(state.result.get("code")) or "retryable_error"
                retry_after = _optional_float(state.result.get("retry_after"))
                delay = backoff_seconds(
                    state.attempt,
                    code=code,
                    retry_after=retry_after,
                    jitter=self._jitter(state.attempt, step.task_id),
                )
                self._clock.sleep(delay)
                self.transition(
                    run_id,
                    step.task_id,
                    TaskStatus.READY,
                    reason="retry",
                    attempt=state.attempt + 1,
                )
                continue
            return

    def _start_or_skip(
        self,
        workflow: Workflow,
        run_id: str,
        step: Step,
        root: str,
        state: TaskState,
    ) -> None:
        digest = self._step_hash(run_id, step, root)
        key = task_idempotency_key(step.task_id, run_id, [digest])
        cached = self._lookup(step.task_id, digest)
        if cached is not None:
            self._adopt(run_id, step, TaskStatus.READY, digest, key, cached)
            return
        if step.collector and step.source_id and not self.source_enabled(step.source_id):
            self._block_disabled(run_id, step, digest, key)
            return
        if not self._budget_allows(step):
            return
        self.transition(
            run_id,
            step.task_id,
            TaskStatus.RUNNING,
            reason="start",
            attempt=state.attempt,
            input_sha256=digest,
            idempotency_key=key,
        )
        outcome = self._invoke(workflow, run_id, step, state.attempt, digest)
        if outcome is None:
            return
        self._apply(workflow, run_id, step, state.attempt, digest, key, outcome)

    def _resume_running(
        self,
        workflow: Workflow,
        run_id: str,
        step: Step,
        root: str,
        state: TaskState,
    ) -> None:
        digest = state.input_sha256 or self._step_hash(run_id, step, root)
        key = state.idempotency_key or task_idempotency_key(step.task_id, run_id, [digest])
        cached = self._lookup(step.task_id, digest)
        if cached is not None:
            self._adopt(run_id, step, TaskStatus.RUNNING, digest, key, cached)
            return
        outcome = self._invoke(workflow, run_id, step, state.attempt, digest)
        if outcome is None:
            return
        self._apply(workflow, run_id, step, state.attempt, digest, key, outcome)

    def _invoke(
        self,
        workflow: Workflow,
        run_id: str,
        step: Step,
        attempt: int,
        digest: str,
    ) -> ActivityResult | None:
        if step.task_id == "publish":
            return self._publish_activity(workflow, run_id, digest)
        started = self._clock.now()
        timeout = self._timeout if self._timeout is not None else step.timeout_seconds
        deadline = started + timedelta(seconds=timeout)
        upstream = self._upstream(run_id, step)
        context = ActivityContext(
            task_id=step.task_id,
            run_id=run_id,
            attempt=attempt,
            input_sha256=digest,
            report_kind=workflow.report_kind,
            slot="",
            deadline=deadline,
            clock=self._clock,
            params={"report_kind": workflow.report_kind, "source_id": step.source_id},
            upstream=upstream,
        )
        activity = self._activities.get(step.task_id, default_pipeline_activity)
        self._calls += 1
        try:
            result = activity(context)
        except ActivityTimeout:
            result = ActivityResult(
                status=TaskStatus.RETRYABLE_ERROR,
                code="timeout",
                errors=("timeout",),
            )
        except TimeoutError:
            result = ActivityResult(
                status=TaskStatus.RETRYABLE_ERROR,
                code="timeout",
                errors=("timeout",),
            )
        except IllegalTransition:
            raise
        except Exception as exc:
            self.pause(f"scheduler fault: {type(exc).__name__}: {exc}"[:300])
            return None
        if self._clock.now() >= deadline:
            return ActivityResult(
                status=TaskStatus.RETRYABLE_ERROR,
                code="timeout",
                errors=("timeout",),
            )
        return result

    def _publish_activity(self, workflow: Workflow, run_id: str, digest: str) -> ActivityResult:
        """Publish only when the stored Q10 result has boolean ``qa_pass`` true."""
        qa = self._load(run_id, "Q10")
        passed = False
        if qa is not None and qa.status is TaskStatus.SUCCESS:
            passed = qa.result.get("qa_pass") is True
        if not passed:
            return ActivityResult(
                status=TaskStatus.SUCCESS,
                records=(),
                output_sha256=digest,
                qa_pass=False,
            )
        report_id = f"{workflow.report_kind}-{run_id}"
        inserted = self._insert_publication(workflow, run_id, report_id)
        self._published += inserted
        records = (report_id,) if inserted else ()
        return ActivityResult(
            status=TaskStatus.SUCCESS,
            records=records,
            output_sha256=digest,
            qa_pass=True,
        )

    def _apply(
        self,
        workflow: Workflow,
        run_id: str,
        step: Step,
        attempt: int,
        digest: str,
        key: str,
        raw: ActivityResult,
    ) -> None:
        result = classify_result(raw)
        if result.cost_usd > 0:
            self._charge(result.cost_usd)
            self._budget_hook.observe_activity(
                run_id=run_id,
                task_id=step.task_id,
                source_id=result.source_id or step.source_id or "none",
                cost_usd=result.cost_usd,
                qa_pass=result.qa_pass,
                anomaly_blocked=result.status is TaskStatus.POLICY_BLOCKED,
                data_volume=len(result.records),
                agent_role=step.group,
                day=self._day(),
            )
        output = result.output_sha256 or _hash_text("|".join(result.records))
        body: dict[str, object] = {
            "code": result.code,
            "cost_usd": result.cost_usd,
            "errors": list(result.errors),
            "output_sha256": output,
            "qa_pass": result.qa_pass,
            "records": list(result.records),
            "retry_after": result.retry_after,
            "status": result.status.value,
        }
        if result.status is TaskStatus.SUCCESS:
            self._store_success(run_id, step, attempt, digest, key, result.records, body)
            return
        if result.status is TaskStatus.FATAL_ERROR:
            self._cache(step.task_id, digest, run_id, key, result.status, body)
            self._dlq(run_id, step.task_id, attempt, digest, "fatal_error", body)
            self.transition(
                run_id,
                step.task_id,
                TaskStatus.FATAL_ERROR,
                reason="fatal_error",
                attempt=attempt,
                input_sha256=digest,
                idempotency_key=key,
                result=body,
            )
            return
        if result.status is TaskStatus.POLICY_BLOCKED:
            source_id = result.source_id or step.source_id
            if source_id:
                self._disable_source(source_id)
            self._cache(step.task_id, digest, run_id, key, result.status, body)
            self._dlq(run_id, step.task_id, attempt, digest, "policy_blocked", body)
            self.transition(
                run_id,
                step.task_id,
                TaskStatus.POLICY_BLOCKED,
                reason="policy_blocked",
                attempt=attempt,
                input_sha256=digest,
                idempotency_key=key,
                result=body,
            )
            return
        failure = {
            "code": result.code or "retryable_error",
            "errors": list(result.errors),
            "retry_after": result.retry_after,
            "status": TaskStatus.RETRYABLE_ERROR.value,
        }
        self.transition(
            run_id,
            step.task_id,
            TaskStatus.RETRYABLE_ERROR,
            reason=result.code or "retryable_error",
            attempt=attempt,
            input_sha256=digest,
            idempotency_key=key,
            result=failure,
        )
        if attempt >= MAX_ATTEMPTS:
            self._dlq(
                run_id, step.task_id, attempt, digest, result.code or "retryable_error", failure
            )

    def _store_success(
        self,
        run_id: str,
        step: Step,
        attempt: int,
        digest: str,
        key: str,
        records: Sequence[str],
        body: Mapping[str, object],
    ) -> None:
        assert_transition(TaskStatus.RUNNING, TaskStatus.SUCCESS)
        when = _iso(self._clock.now())
        payload = _json(body)
        inserted = 0
        duplicates = 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for record_key in records:
                found = self._conn.execute(
                    """
                    SELECT 1 FROM o20_records
                    WHERE task_id = ? AND input_sha256 = ? AND record_key = ?
                    """,
                    (step.task_id, digest, record_key),
                ).fetchone()
                if found is not None:
                    duplicates += 1
                    continue
                self._conn.execute(
                    """
                    INSERT INTO o20_records (task_id, input_sha256, record_key, run_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (step.task_id, digest, record_key, run_id),
                )
                inserted += 1
            self._conn.execute(
                """
                INSERT OR REPLACE INTO o20_results (
                    task_id, input_sha256, run_id, idempotency_key, status, result_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (step.task_id, digest, run_id, key, TaskStatus.SUCCESS.value, payload),
            )
            self._conn.execute(
                """
                UPDATE o20_task_state
                SET status = ?, attempt = ?, input_sha256 = ?, idempotency_key = ?,
                    result_json = ?, updated_at = ?
                WHERE run_id = ? AND task_id = ?
                """,
                (
                    TaskStatus.SUCCESS.value,
                    attempt,
                    digest,
                    key,
                    payload,
                    when,
                    run_id,
                    step.task_id,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO o20_checkpoints (
                    run_id, task_id, attempt, status, input_sha256, idempotency_key,
                    reason, result_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step.task_id,
                    attempt,
                    TaskStatus.SUCCESS.value,
                    digest,
                    key,
                    "success",
                    payload,
                    when,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO o20_audit (
                    run_id, task_id, from_status, to_status, attempt, reason, at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step.task_id,
                    TaskStatus.RUNNING.value,
                    TaskStatus.SUCCESS.value,
                    attempt,
                    "success",
                    when,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        self._inserted += inserted
        self._duplicates += duplicates

    def _adopt(
        self,
        run_id: str,
        step: Step,
        src: TaskStatus,
        digest: str,
        key: str,
        cached: _Cached,
    ) -> None:
        """Reuse a stored result. The activity is not called and no record is inserted."""
        if src is TaskStatus.READY:
            assert_transition(TaskStatus.READY, TaskStatus.RUNNING)
            assert_transition(TaskStatus.RUNNING, cached.status)
        else:
            assert_transition(src, cached.status)
        self._skipped += 1
        when = _iso(self._clock.now())
        payload = _json(cached.result)
        attempt = 1
        current = self._load(run_id, step.task_id)
        if current is not None and current.attempt:
            attempt = current.attempt
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if src is TaskStatus.READY:
                self._conn.execute(
                    """
                    UPDATE o20_task_state
                    SET status = ?, input_sha256 = ?, idempotency_key = ?,
                        result_json = ?, updated_at = ?
                    WHERE run_id = ? AND task_id = ?
                    """,
                    (TaskStatus.RUNNING.value, digest, key, payload, when, run_id, step.task_id),
                )
                self._insert_checkpoint_audit(
                    run_id,
                    step.task_id,
                    attempt,
                    TaskStatus.READY.value,
                    TaskStatus.RUNNING.value,
                    digest,
                    key,
                    "replay input_sha256",
                    payload,
                    when,
                )
            self._conn.execute(
                """
                UPDATE o20_task_state
                SET status = ?, input_sha256 = ?, idempotency_key = ?,
                    result_json = ?, updated_at = ?
                WHERE run_id = ? AND task_id = ?
                """,
                (cached.status.value, digest, key, payload, when, run_id, step.task_id),
            )
            self._insert_checkpoint_audit(
                run_id,
                step.task_id,
                attempt,
                TaskStatus.RUNNING.value,
                cached.status.value,
                digest,
                key,
                "replay input_sha256",
                payload,
                when,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _block_disabled(self, run_id: str, step: Step, digest: str, key: str) -> None:
        body: dict[str, object] = {
            "code": "policy_blocked",
            "errors": ("source disabled",),
            "qa_pass": None,
            "records": [],
            "status": TaskStatus.POLICY_BLOCKED.value,
        }
        self.transition(
            run_id,
            step.task_id,
            TaskStatus.RUNNING,
            reason="start",
            input_sha256=digest,
            idempotency_key=key,
        )
        self._cache(step.task_id, digest, run_id, key, TaskStatus.POLICY_BLOCKED, body)
        self._dlq(run_id, step.task_id, 1, digest, "policy_blocked", body)
        self.transition(
            run_id,
            step.task_id,
            TaskStatus.POLICY_BLOCKED,
            reason="source disabled",
            input_sha256=digest,
            idempotency_key=key,
            result=body,
        )

    def _deps_ready(self, workflow: Workflow, run_id: str, step: Step) -> bool:
        index = self._steps[workflow.workflow_id]
        for dep_id in step.depends:
            dep = index[dep_id]
            state = self._load(run_id, dep_id)
            if state is None or not self._dep_satisfied(dep, state):
                return False
        return True

    def _dep_satisfied(self, dep: Step, state: TaskState) -> bool:
        if state.status is TaskStatus.SUCCESS:
            return True
        settled = state.status in {TaskStatus.POLICY_BLOCKED, TaskStatus.FATAL_ERROR} or (
            state.status is TaskStatus.RETRYABLE_ERROR and state.attempt >= MAX_ATTEMPTS
        )
        return bool(dep.collector and settled)

    def _finished(self, step: Step, state: TaskState) -> bool:
        if state.status in _TERMINAL:
            return True
        return state.status is TaskStatus.RETRYABLE_ERROR and state.attempt >= MAX_ATTEMPTS

    def _step_hash(self, run_id: str, step: Step, root: str) -> str:
        upstream = [
            f"{dep}:{digest}" for dep, digest in sorted(self._upstream(run_id, step).items())
        ]
        body = json.dumps(
            {"root": root, "task_id": step.task_id, "upstream": upstream},
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _upstream(self, run_id: str, step: Step) -> dict[str, str]:
        found: dict[str, str] = {}
        for dep_id in step.depends:
            state = self._load(run_id, dep_id)
            if state is None:
                found[dep_id] = ""
                continue
            output = state.result.get("output_sha256")
            found[dep_id] = output if isinstance(output, str) and output else state.status.value
        return found

    def _lookup(self, task_id: str, digest: str) -> _Cached | None:
        row = self._conn.execute(
            """
            SELECT status, result_json FROM o20_results
            WHERE task_id = ? AND input_sha256 = ?
            """,
            (task_id, digest),
        ).fetchone()
        if row is None:
            return None
        status = TaskStatus(_cell_text(row, "status"))
        if status not in _TERMINAL:
            return None
        parsed = _object(json.loads(_cell_text(row, "result_json")))
        if parsed is None:
            return None
        return _Cached(status, parsed)

    def _cache(
        self,
        task_id: str,
        digest: str,
        run_id: str,
        key: str,
        status: TaskStatus,
        body: Mapping[str, object],
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO o20_results (
                task_id, input_sha256, run_id, idempotency_key, status, result_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, digest, run_id, key, status.value, _json(body)),
        )

    def _dlq(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        digest: str,
        reason: str,
        body: Mapping[str, object],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO o20_dlq (
                run_id, task_id, attempt, input_sha256, reason, payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, task_id, attempt, digest, reason, _json(body), _iso(self._clock.now())),
        )
        self._dlq_new += 1

    def _disable_source(self, source_id: str) -> None:
        when = _iso(self._clock.now())
        updated = self._conn.execute(
            """
            UPDATE source_registry SET enabled = 0, checked_at = ? WHERE source_id = ?
            """,
            (when, source_id),
        )
        if updated.rowcount == 0:
            self._conn.execute(
                """
                INSERT INTO source_registry (
                    source_id, source_family, base_url, policy_snapshot_id,
                    enabled, requests_per_minute, checked_at
                ) VALUES (?, 'unknown', 'not stated', 'policy-2026-10-27', 0, 10, ?)
                """,
                (source_id, when),
            )

    def _insert_publication(self, workflow: Workflow, run_id: str, report_id: str) -> int:
        found = self._conn.execute(
            "SELECT 1 FROM published_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        if found is not None:
            return 0
        self._conn.execute(
            """
            INSERT INTO published_reports (
                report_id, report_kind, content_uri, qa_manifest_uri,
                evidence_snapshot_uri, published_at, generated_by_ai
            ) VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                report_id,
                workflow.report_kind,
                f"local:{run_id}/R10/draft.md",
                f"local:{run_id}/Q10/manifest.json",
                f"local:{run_id}/E10/evidence_pack.json",
                _iso(self._clock.now()),
            ),
        )
        return 1

    def _insert_checkpoint_audit(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        src: str,
        dst: str,
        digest: str,
        key: str,
        reason: str,
        payload: str,
        when: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO o20_checkpoints (
                run_id, task_id, attempt, status, input_sha256, idempotency_key,
                reason, result_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, task_id, attempt, dst, digest, key, reason, payload, when),
        )
        self._conn.execute(
            """
            INSERT INTO o20_audit (
                run_id, task_id, from_status, to_status, attempt, reason, at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, task_id, src, dst, attempt, reason, when),
        )

    def _fire(self, workflow_id: str, slot: str, run_id: str, payload: bytes) -> tuple[str, bytes]:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO o20_fires (workflow_id, slot, run_id, payload)
            VALUES (?, ?, ?, ?)
            """,
            (workflow_id, slot, run_id, payload),
        )
        row = self._conn.execute(
            "SELECT run_id, payload FROM o20_fires WHERE workflow_id = ? AND slot = ?",
            (workflow_id, slot),
        ).fetchone()
        if row is None:
            raise sqlite3.Error("schedule fire was not stored")
        stored_id = _cell_text(row, "run_id")
        stored_payload = row["payload"]
        if not isinstance(stored_payload, bytes):
            raise sqlite3.Error("schedule payload must be bytes")
        return stored_id, stored_payload

    def _seed_sources(self) -> None:
        when = _iso(self._clock.now())
        for source_id, family, base_url, enabled in _SOURCES:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO source_registry (
                    source_id, source_family, base_url, policy_snapshot_id,
                    enabled, requests_per_minute, checked_at
                ) VALUES (?, ?, ?, 'policy-2026-10-27', ?, 10, ?)
                """,
                (source_id, family, base_url, enabled, when),
            )

    def _day(self) -> str:
        return self._clock.now().astimezone(UTC).date().isoformat()

    def _budget_allows(self, step: Step) -> bool:
        """Pause at the $25 breaker. Hold a non-P0 LLM call when the gate says so."""
        decision = self._budget_hook.decision(day=self._day())
        if decision.circuit_open or self._spent() >= DAILY_MAX_USD:
            self.pause("daily budget")
            return False
        if decision.allow_task(step.task_id):
            return True
        self._budget_hook.log_event(
            {"event": "llm_paused", "mode": decision.mode, "task_id": step.task_id}
        )
        self._held.add(step.task_id)
        return False

    def _spent(self) -> float:
        day = self._clock.now().astimezone(UTC).date().isoformat()
        row = self._conn.execute(
            "SELECT spent_usd FROM o20_budget WHERE day = ?",
            (day,),
        ).fetchone()
        if row is None:
            return 0.0
        return _cell_float(row, "spent_usd")

    def _charge(self, amount: float) -> None:
        if amount <= 0:
            return
        day = self._clock.now().astimezone(UTC).date().isoformat()
        self._conn.execute(
            """
            INSERT INTO o20_budget (day, spent_usd) VALUES (?, ?)
            ON CONFLICT(day) DO UPDATE SET spent_usd = spent_usd + excluded.spent_usd
            """,
            (day, amount),
        )

    def _load(self, run_id: str, task_id: str) -> TaskState | None:
        row = self._conn.execute(
            """
            SELECT status, attempt, input_sha256, idempotency_key, result_json
            FROM o20_task_state WHERE run_id = ? AND task_id = ?
            """,
            (run_id, task_id),
        ).fetchone()
        if row is None:
            return None
        parsed = _object(json.loads(_cell_text(row, "result_json")))
        return TaskState(
            status=TaskStatus(_cell_text(row, "status")),
            attempt=_cell_int(row, "attempt"),
            input_sha256=_cell_text(row, "input_sha256"),
            idempotency_key=_cell_text(row, "idempotency_key"),
            result={} if parsed is None else parsed,
        )

    def _require(self, run_id: str, task_id: str) -> TaskState:
        state = self._load(run_id, task_id)
        if state is None:
            raise sqlite3.Error(f"missing task {task_id}")
        return state

    def _advance_to(self, slot: datetime) -> None:
        now = self._clock.now()
        if slot <= now:
            return
        self._clock.advance((slot - now).total_seconds())

    def _run_status(self, workflow: Workflow, run_id: str) -> str:
        for step in workflow.steps:
            state = self._load(run_id, step.task_id)
            if state is None or not self._finished(step, state):
                return "incomplete"
        return "success"

    def _idle_report(self, workflow: Workflow, slot: str) -> RunReport:
        return RunReport(
            workflow_id=workflow.workflow_id,
            run_id="",
            slot=slot,
            status="paused",
            records_inserted=0,
            duplicate_records=0,
            skipped=0,
            published=0,
            dlq=0,
            activity_calls=0,
            tasks=(),
        )

    def _report(self, workflow: Workflow, run_id: str, slot: str, status: str) -> RunReport:
        tasks: list[TaskSnapshot] = []
        if run_id:
            for step in workflow.steps:
                state = self._load(run_id, step.task_id)
                if state is None:
                    continue
                tasks.append(
                    TaskSnapshot(
                        step.task_id, state.status.value, state.attempt, state.input_sha256
                    )
                )
        return RunReport(
            workflow_id=workflow.workflow_id,
            run_id=run_id,
            slot=slot,
            status=status,
            records_inserted=self._inserted,
            duplicate_records=self._duplicates,
            skipped=self._skipped,
            published=self._published,
            dlq=self._dlq_new,
            activity_calls=self._calls,
            tasks=tuple(tasks),
        )

    def _reset_counters(self) -> None:
        self._inserted = 0
        self._duplicates = 0
        self._skipped = 0
        self._published = 0
        self._dlq_new = 0
        self._calls = 0


def _zero_jitter(_attempt: int, _task_id: str) -> float:
    return 0.0


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    text = value.astimezone(UTC).isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def _json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _pretty(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        parsed[key] = item
    return parsed


def _text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _cell_text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise sqlite3.Error(f"expected text {key}")
    return value


def _cell_int(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise sqlite3.Error(f"expected integer {key}")
    return value


def _cell_float(row: sqlite3.Row, key: str) -> float:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise sqlite3.Error(f"expected number {key}")
    return float(value)


def _bit(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return False


def _count(conn: sqlite3.Connection, table: str) -> int:
    if table not in {
        "o20_records",
        "published_reports",
        "o20_dlq",
        "o20_checkpoints",
    }:
        raise ValueError("unknown table")
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    if row is None:
        return 0
    return _cell_int(row, "n")


def _artifact_run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."}:
        raise ValueError("invalid run_id")
    if any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _file_entry(path: Path, run_id: str) -> dict[str, object]:
    data = path.read_bytes()
    relative = f"artifacts/{run_id}/O20/{path.name}"
    return {"bytes": len(data), "path": relative, "sha256": hashlib.sha256(data).hexdigest()}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
