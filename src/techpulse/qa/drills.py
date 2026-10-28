"""Q30 regression, capacity, cost, and disaster-recovery drills.

Synthetic records are built in memory for one daily slot. The O20
``LocalOrchestrator`` runs them. Collector and later template steps do not
call a model, so stored activity results have no token field and the token
total is the sum of those fields (zero). D10 archives the batch through
``archive_collector_ndjson``.

Faults are injected in-process: one HTTP 429, one ``sqlite3.OperationalError``
before the archive commit, and one object-store ``OSError``. The restore
check byte-copies the closed SQLite files and the artifact tree. O40 owns
the scheduled backup scripts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from techpulse.contracts import TaskStatus
from techpulse.workflows.activities import (
    ActivityContext,
    ActivityResult,
    LocalObjectStore,
    archive_collector_ndjson,
)
from techpulse.workflows.orchestrator import (
    DAILY_MAX_USD,
    LocalOrchestrator,
    ManualClock,
    RunReport,
    iter_slots,
    pipeline_task_ids,
)

CAPACITY_N = 10_000
FAST_N = 40
Q30_MAX_USD = 8.0
DRILL_DAY = datetime(2026, 10, 27, tzinfo=UTC)
_WORKFLOW_ID = "techpulse-daily-v1"
_SOURCES = ("openalex", "crossref", "arxiv", "github", "hn")
_COLLECTORS = frozenset({"C10", "C11", "C12", "C20", "C21"})
_ORCH_TABLES = (
    "o20_audit",
    "o20_checkpoints",
    "o20_dlq",
    "o20_records",
    "o20_results",
    "published_reports",
)
_CATALOG_TABLES = ("raw_dlq", "raw_idempotency", "raw_records")
_ALLOWED_TABLES = frozenset(_ORCH_TABLES) | frozenset(_CATALOG_TABLES)
_RELEASE_FILES = ("qa_release.json", "metrics.json")


@dataclass(frozen=True)
class SyntheticRecord:
    """One deterministic raw record. It is not read from a fixture file."""

    source_id: str
    source_record_id: str
    canonical_url: str
    title: str
    abstract_or_excerpt: str
    published_at: str
    collected_at: str
    language: str
    license_status: str
    content_sha256: str
    policy_snapshot_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "abstract_or_excerpt": self.abstract_or_excerpt,
            "canonical_url": self.canonical_url,
            "collected_at": self.collected_at,
            "content_sha256": self.content_sha256,
            "language": self.language,
            "license_status": self.license_status,
            "policy_snapshot_id": self.policy_snapshot_id,
            "published_at": self.published_at,
            "schema_version": "1.0",
            "source_id": self.source_id,
            "source_record_id": self.source_record_id,
            "title": self.title,
        }


@dataclass(frozen=True)
class RestoreCheck:
    """Count and sha256 of the SQLite files and the artifact tree."""

    database_rows: int
    restored_rows: int
    database_sha256: str
    restored_sha256: str
    artifact_files: int
    restored_artifact_files: int
    artifact_sha256: str
    restored_artifact_sha256: str
    tables: tuple[tuple[str, int], ...]
    restored_tables: tuple[tuple[str, int], ...]

    @property
    def match(self) -> bool:
        return (
            self.database_rows == self.restored_rows
            and self.database_sha256 == self.restored_sha256
            and self.artifact_files == self.restored_artifact_files
            and self.artifact_sha256 == self.restored_artifact_sha256
            and self.tables == self.restored_tables
        )


@dataclass(frozen=True)
class Q30Report:
    """Measured outcome of one Q30 drill."""

    run_id: str
    slot: str
    record_count: int
    raw_rows: int
    orchestrator_rows: int
    batch_sha256: str
    tokens: int
    latency_seconds: float
    replay_latency_seconds: float
    activity_latency_seconds: float
    retry_attempts: int
    retry_backoff_seconds: float
    backoff_seconds: tuple[float, ...]
    replay_backoff_seconds: float
    cost_usd: float
    budget_spent_usd: float
    published_reports: int
    duplicate_public_reports: int
    replay_records_inserted: int
    replay_duplicate_records: int
    replay_activity_calls: int
    activity_calls: int
    resumes: int
    paused_published: tuple[int, ...]
    faults: Mapping[str, int]
    step_calls: Mapping[str, int]
    restore: RestoreCheck
    status: str
    errors: tuple[str, ...]
    rolled_back: bool
    sealed_i30_used: bool
    network_calls: int
    generated_at: str


class _FaultyStore:
    """Object store that fails ``put`` a fixed number of times, then writes."""

    def __init__(self, root: Path, failures: int) -> None:
        self._inner = LocalObjectStore(root)
        self._remaining = failures
        self.failures_raised = 0

    def put(self, key: str, body: bytes) -> str:
        if self._remaining > 0:
            self._remaining -= 1
            self.failures_raised += 1
            raise OSError("object store unavailable")
        return self._inner.put(key, body)

    def get(self, uri: str) -> bytes:
        return self._inner.get(uri)


class _DrillActivity:
    """Template activity for one synthetic day, with three injected faults."""

    def __init__(
        self,
        records: Sequence[SyntheticRecord],
        ndjson: bytes,
        catalog: Path,
        store: _FaultyStore,
        pipeline: Path,
        archived_at: datetime,
    ) -> None:
        self._records = tuple(records)
        self._ndjson = ndjson
        self._catalog = catalog
        self._store = store
        self._pipeline = pipeline
        self._archived_at = archived_at
        self._rate_limits = 1
        self._db_faults = 1
        self._keys = {
            source: tuple(
                record.source_record_id for record in self._records if record.source_id == source
            )
            for source in _SOURCES
        }
        self.faults: dict[str, int] = {
            "database_disconnect": 0,
            "http_429": 0,
            "object_store": 0,
        }
        self.step_calls: dict[str, int] = {}
        self.step_seconds: dict[str, float] = {}
        self.activity_seconds = 0.0
        self.archived_inserted = 0

    def __call__(self, context: ActivityContext) -> ActivityResult:
        started = time.perf_counter()
        try:
            return self._once(context)
        finally:
            elapsed = time.perf_counter() - started
            self.activity_seconds += elapsed
            task_id = context.task_id
            self.step_calls[task_id] = self.step_calls.get(task_id, 0) + 1
            self.step_seconds[task_id] = self.step_seconds.get(task_id, 0.0) + elapsed

    def _once(self, context: ActivityContext) -> ActivityResult:
        if context.task_id == "C10" and self._rate_limits > 0:
            self._rate_limits -= 1
            self.faults["http_429"] += 1
            return ActivityResult(
                status=TaskStatus.RETRYABLE_ERROR,
                code="http_429",
                errors=("http_429",),
            )
        if context.task_id == "D10" and self._db_faults > 0:
            self._db_faults -= 1
            self.faults["database_disconnect"] += 1
            raise sqlite3.OperationalError("database disconnected")
        if context.task_id in _COLLECTORS:
            return self._collector(context)
        if context.task_id == "D10":
            return self._archive(context.run_id)
        if context.task_id == "Q10":
            return ActivityResult(
                status=TaskStatus.SUCCESS,
                qa_pass=True,
                output_sha256=context.input_sha256,
            )
        return ActivityResult(status=TaskStatus.SUCCESS, output_sha256=context.input_sha256)

    def _collector(self, context: ActivityContext) -> ActivityResult:
        source_id = context.params.get("source_id")
        if not isinstance(source_id, str) or source_id not in self._keys:
            return ActivityResult(
                status=TaskStatus.FATAL_ERROR,
                code="fatal_error",
                errors=("fatal_error: collector has no synthetic source",),
            )
        keys = self._keys[source_id]
        digest = hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()
        return ActivityResult(status=TaskStatus.SUCCESS, records=keys, output_sha256=digest)

    def _archive(self, run_id: str) -> ActivityResult:
        archived = archive_collector_ndjson(
            self._ndjson,
            run_id=run_id,
            database=self._catalog,
            store=self._store,
            artifacts_dir=self._pipeline,
            archived_at=self._archived_at,
        )
        self.faults["object_store"] = self._store.failures_raised
        if archived.status is not TaskStatus.SUCCESS:
            code = archived.status.value
            return ActivityResult(status=archived.status, code=code, errors=archived.errors)
        if archived.inserted != len(self._records):
            return ActivityResult(
                status=TaskStatus.FATAL_ERROR,
                code="fatal_error",
                errors=(f"fatal_error: archived {archived.inserted} of {len(self._records)}",),
            )
        self.archived_inserted = archived.inserted
        keys = tuple(record.source_record_id for record in self._records)
        return ActivityResult(
            status=TaskStatus.SUCCESS,
            records=keys,
            output_sha256=archived.object_sha256,
        )


def synthetic_records(n: int, *, day: datetime = DRILL_DAY) -> tuple[SyntheticRecord, ...]:
    """Build ``n`` raw records for ``day``. The same inputs always return the same rows."""
    if n < 1:
        raise ValueError("n must be positive")
    stamp = _stamp(_day_start(day))
    rows: list[SyntheticRecord] = []
    for index in range(n):
        source_id = _SOURCES[index % len(_SOURCES)]
        source_record_id = f"q30-{index:05d}"
        identity = f"{source_id}|{source_record_id}|{stamp}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        rows.append(
            SyntheticRecord(
                source_id=source_id,
                source_record_id=source_record_id,
                canonical_url=f"https://example.org/q30/{source_id}/{source_record_id}",
                title=f"Q30 synthetic {source_record_id}",
                abstract_or_excerpt="synthetic capacity record",
                published_at=stamp,
                collected_at=stamp,
                language="en",
                license_status="metadata_only",
                content_sha256=digest,
                policy_snapshot_id="q30-drill",
            )
        )
    return tuple(rows)


def batch_sha256(records: Sequence[SyntheticRecord]) -> str:
    """Identity of a synthetic batch. The line format is source, id, and content hash."""
    payload = "\n".join(
        f"{record.source_id}|{record.source_record_id}|{record.content_sha256}"
        for record in records
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_q30_drill(
    *,
    n: int,
    work: Path,
    release_root: Path | None = None,
    day: datetime = DRILL_DAY,
) -> Q30Report:
    """Replay one daily slot of ``n`` synthetic records, then the same slot again.

    The window is the UTC calendar day of ``day``. It must contain the daily
    08:00 slot and no weekly slot. The long capacity run passes ``CAPACITY_N``.
    """
    if n < 1:
        raise ValueError("n must be positive")
    day_start = _day_start(day)
    slots = iter_slots(day_start, day_start + timedelta(hours=24))
    if len(slots) != 1 or slots[0][0] != _WORKFLOW_ID:
        raise ValueError("Q30 drill expects one daily slot in the window")
    slot = slots[0][1]
    records = synthetic_records(n, day=day_start)
    ndjson = _ndjson(records)
    work.mkdir(parents=True, exist_ok=True)
    catalog = work / "catalog.sqlite"
    pipeline = work / "pipeline"
    pipeline.mkdir(parents=True, exist_ok=True)
    store = _FaultyStore(pipeline, failures=1)
    activity = _DrillActivity(records, ndjson, catalog, store, pipeline, slot)
    clock = ManualClock(day_start)
    started = time.perf_counter()
    with LocalOrchestrator(
        work / "orch.sqlite",
        clock=clock,
        activities={task_id: activity for task_id in pipeline_task_ids()},
    ) as orch:
        first, resumes, paused_published, activity_calls = _settle(orch, slot)
        latency = time.perf_counter() - started
        backoff = tuple(clock.slept)
        activity_latency = activity.activity_seconds
        faults = dict(activity.faults)
        step_calls = dict(activity.step_calls)
        replay_mark = len(clock.slept)
        replay_started = time.perf_counter()
        replay = orch.simulate(day_start, hours=24)
        replay_latency = time.perf_counter() - replay_started
        replay_backoff = tuple(clock.slept[replay_mark:])
        published = orch.published_count()
        orchestrator_rows = orch.record_count()
        run_id = first.run_id
    raw_rows = _scalar(catalog, "SELECT COUNT(*) FROM raw_records")
    cost_usd, tokens = _sum_stored_results(work / "orch.sqlite")
    budget_spent = _budget_spent(work / "orch.sqlite")
    report_ids = _report_ids(work / "orch.sqlite")
    duplicate_public = len(report_ids) - len(set(report_ids))
    restore = backup_restore(
        work / "orch.sqlite",
        catalog,
        pipeline,
        work / "backup",
        work / "restored",
    )
    errors = _errors(
        record_count=n,
        raw_rows=raw_rows,
        replay_inserted=replay.records_inserted,
        replay_duplicates=replay.duplicate_records,
        published=published,
        paused_published=paused_published,
        duplicate_public=duplicate_public,
        cost_usd=cost_usd,
        budget_spent=budget_spent,
        tokens=tokens,
        latency=latency,
        replay_latency=replay_latency,
        restore=restore,
        run_status=first.status,
        faults=faults,
        archived=activity.archived_inserted,
        resumes=resumes,
        replay_activity_calls=replay.activity_calls,
        replay_backoff_seconds=float(sum(replay_backoff, 0.0)),
        orchestrator_rows=orchestrator_rows,
    )
    status = "pass" if not errors else "fail"
    rolled_back = False
    if status != "pass":
        _roll_back(work / "orch.sqlite")
        rolled_back = True
    generated_at = _iso(datetime.now(UTC))
    report = Q30Report(
        run_id=run_id,
        slot=first.slot,
        record_count=n,
        raw_rows=raw_rows,
        orchestrator_rows=orchestrator_rows,
        batch_sha256=batch_sha256(records),
        tokens=tokens,
        latency_seconds=latency,
        replay_latency_seconds=replay_latency,
        activity_latency_seconds=activity_latency,
        retry_attempts=_retry_attempts(step_calls),
        retry_backoff_seconds=float(sum(backoff, 0.0)),
        backoff_seconds=backoff,
        replay_backoff_seconds=float(sum(replay_backoff, 0.0)),
        cost_usd=cost_usd,
        budget_spent_usd=budget_spent,
        published_reports=published,
        duplicate_public_reports=duplicate_public,
        replay_records_inserted=replay.records_inserted,
        replay_duplicate_records=replay.duplicate_records,
        replay_activity_calls=replay.activity_calls,
        activity_calls=activity_calls,
        resumes=resumes,
        paused_published=paused_published,
        faults=faults,
        step_calls=step_calls,
        restore=restore,
        status=status,
        errors=tuple(errors),
        rolled_back=rolled_back,
        sealed_i30_used=False,
        network_calls=0,
        generated_at=generated_at,
    )
    if release_root is not None:
        write_q30_artifacts(release_root, report)
    return report


def backup_restore(
    orchestrator_db: Path,
    catalog_db: Path,
    artifacts: Path,
    backup: Path,
    restored: Path,
) -> RestoreCheck:
    """Copy the closed databases and artifact files, then compare count and sha256."""
    source_tables = _combined_counts(orchestrator_db, catalog_db)
    _copy_file(orchestrator_db, backup / "orch.sqlite")
    _copy_file(catalog_db, backup / "catalog.sqlite")
    _copy_tree(artifacts, backup / "pipeline")
    source_db_sha = _bundle_sha(
        (
            ("catalog.sqlite", _sha256_file(catalog_db)),
            ("orch.sqlite", _sha256_file(orchestrator_db)),
        )
    )
    source_files, source_tree = _tree_signature(artifacts)
    _copy_tree(backup, restored)
    restored_orch = restored / "orch.sqlite"
    restored_catalog = restored / "catalog.sqlite"
    restored_artifacts = restored / "pipeline"
    restored_db_sha = _bundle_sha(
        (
            ("catalog.sqlite", _sha256_file(restored_catalog)),
            ("orch.sqlite", _sha256_file(restored_orch)),
        )
    )
    restored_files, restored_tree = _tree_signature(restored_artifacts)
    restored_tables = _combined_counts(restored_orch, restored_catalog)
    table_rows = tuple(sorted(source_tables.items()))
    restored_rows = tuple(sorted(restored_tables.items()))
    return RestoreCheck(
        database_rows=sum(source_tables.values()),
        restored_rows=sum(restored_tables.values()),
        database_sha256=source_db_sha,
        restored_sha256=restored_db_sha,
        artifact_files=source_files,
        restored_artifact_files=restored_files,
        artifact_sha256=source_tree,
        restored_artifact_sha256=restored_tree,
        tables=table_rows,
        restored_tables=restored_rows,
    )


def write_q30_artifacts(root: Path, report: Q30Report) -> Path:
    """Write ``artifacts/{run_id}/Q30`` qa_release, metrics, and manifest."""
    directory = root / report.run_id / "Q30"
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "qa_release.json", _pretty(_qa_release(report)))
    _write(directory / "metrics.json", _pretty(_metrics(report)))
    manifest: dict[str, object] = {
        "errors": list(report.errors),
        "files": [_file_entry(directory / name, report.run_id) for name in _RELEASE_FILES],
        "run_id": report.run_id,
        "status": report.status,
        "ticket": "Q30",
    }
    _write(directory / "manifest.json", _pretty(manifest))
    return directory


def _settle(
    orch: LocalOrchestrator,
    slot: datetime,
) -> tuple[RunReport, int, tuple[int, ...], int]:
    """Run the slot, and resume once after the injected database disconnect."""
    paused_published: list[int] = []
    calls = 0
    resumes = 0
    report = orch.execute(_WORKFLOW_ID, slot=slot)
    calls += report.activity_calls
    while report.status == "paused" and resumes < 3:
        reason = orch.pause_reason()
        if not reason.startswith("scheduler fault:"):
            break
        paused_published.append(orch.published_count())
        orch.resume()
        resumes += 1
        report = orch.execute(_WORKFLOW_ID, slot=slot)
        calls += report.activity_calls
    return report, resumes, tuple(paused_published), calls


def _retry_attempts(step_calls: Mapping[str, int]) -> int:
    return sum(max(0, count - 1) for count in step_calls.values())


def _errors(
    *,
    record_count: int,
    raw_rows: int,
    replay_inserted: int,
    replay_duplicates: int,
    published: int,
    paused_published: Sequence[int],
    duplicate_public: int,
    cost_usd: float,
    budget_spent: float,
    tokens: int,
    latency: float,
    replay_latency: float,
    restore: RestoreCheck,
    run_status: str,
    faults: Mapping[str, int],
    archived: int,
    resumes: int,
    replay_activity_calls: int,
    replay_backoff_seconds: float,
    orchestrator_rows: int,
) -> list[str]:
    errors: list[str] = []
    if run_status != "success":
        errors.append("daily slot did not succeed")
    if raw_rows != record_count or archived != record_count:
        errors.append("archived row count does not match the synthetic day")
    if orchestrator_rows != (record_count * 2) + published:
        errors.append("orchestrator record ledger does not match the synthetic day")
    if resumes != 1:
        errors.append("database disconnect did not pause the scheduler once")
    if replay_inserted != 0 or replay_duplicates != 0 or replay_activity_calls != 0:
        errors.append("replay inserted, duplicated, or called an activity")
    if replay_backoff_seconds != 0:
        errors.append("replay waited on a retry")
    if published != 1 or any(count != 0 for count in paused_published):
        errors.append("faults published a report or the slot did not publish once")
    if duplicate_public != 0:
        errors.append("duplicate public reports")
    if cost_usd < 0 or cost_usd > Q30_MAX_USD or budget_spent > DAILY_MAX_USD:
        errors.append("budget exceeded")
    if tokens != 0:
        errors.append("template steps recorded tokens")
    if not math.isfinite(latency) or latency < 0:
        errors.append("latency was not measured")
    if not math.isfinite(replay_latency) or replay_latency < 0:
        errors.append("replay latency was not measured")
    if not restore.match:
        errors.append("restore count or sha256 mismatch")
    expected = {"database_disconnect": 1, "http_429": 1, "object_store": 1}
    if dict(faults) != expected:
        errors.append("injected faults did not each fire once")
    return errors


def _roll_back(database: Path) -> None:
    """Pause the drill database and drop public reports from this failed run."""
    conn = sqlite3.connect(database)
    try:
        conn.execute("DELETE FROM published_reports")
        conn.execute(
            "UPDATE o20_control SET paused = 1, reason = ? WHERE id = 1",
            ("q30 gate failed",),
        )
        conn.commit()
    finally:
        conn.close()


def _qa_release(report: Q30Report) -> dict[str, object]:
    return {
        "budget_max_usd": Q30_MAX_USD,
        "cost_usd": report.cost_usd,
        "daily_max_usd": DAILY_MAX_USD,
        "duplicate_public_reports": report.duplicate_public_reports,
        "errors": list(report.errors),
        "latency_seconds": report.latency_seconds,
        "published_reports": report.published_reports,
        "record_count": report.record_count,
        "replay_records_inserted": report.replay_records_inserted,
        "restore_match": report.restore.match,
        "retry_attempts": report.retry_attempts,
        "retry_backoff_seconds": report.retry_backoff_seconds,
        "rolled_back": report.rolled_back,
        "run_id": report.run_id,
        "slot": report.slot,
        "status": report.status,
        "ticket": "Q30",
        "tokens": report.tokens,
        "workflow_id": _WORKFLOW_ID,
    }


def _metrics(report: Q30Report) -> dict[str, object]:
    restore = report.restore
    return {
        "activity_calls": report.activity_calls,
        "activity_latency_seconds": report.activity_latency_seconds,
        "batch_sha256": report.batch_sha256,
        "budget_spent_usd": report.budget_spent_usd,
        "cost_usd": report.cost_usd,
        "duplicate_public_reports": report.duplicate_public_reports,
        "errors": list(report.errors),
        "faults": dict(report.faults),
        "generated_at": report.generated_at,
        "latency_seconds": report.latency_seconds,
        "network_calls": report.network_calls,
        "orchestrator_rows": report.orchestrator_rows,
        "paused_published": list(report.paused_published),
        "published_reports": report.published_reports,
        "raw_rows": report.raw_rows,
        "record_count": report.record_count,
        "replay_activity_calls": report.replay_activity_calls,
        "replay_backoff_seconds": report.replay_backoff_seconds,
        "replay_duplicate_records": report.replay_duplicate_records,
        "replay_latency_seconds": report.replay_latency_seconds,
        "replay_records_inserted": report.replay_records_inserted,
        "restore": {
            "artifact_files": restore.artifact_files,
            "artifact_sha256": restore.artifact_sha256,
            "database_rows": restore.database_rows,
            "database_sha256": restore.database_sha256,
            "match": restore.match,
            "restored_artifact_files": restore.restored_artifact_files,
            "restored_artifact_sha256": restore.restored_artifact_sha256,
            "restored_database_rows": restore.restored_rows,
            "restored_database_sha256": restore.restored_sha256,
            "restored_tables": _tables(restore.restored_tables),
            "tables": _tables(restore.tables),
        },
        "resumes": report.resumes,
        "retry_attempts": report.retry_attempts,
        "retry_backoff_seconds": report.retry_backoff_seconds,
        "retry_sleep_seconds": list(report.backoff_seconds),
        "rolled_back": report.rolled_back,
        "run_id": report.run_id,
        "sealed_i30_used": report.sealed_i30_used,
        "slot": report.slot,
        "status": report.status,
        "step_calls": dict(report.step_calls),
        "template_steps": True,
        "ticket": "Q30",
        "token_source": "sum of tokens fields on stored activity results",
        "tokens": report.tokens,
        "workflow_id": _WORKFLOW_ID,
    }


def _tables(rows: Sequence[tuple[str, int]]) -> dict[str, int]:
    return dict(rows)


def _file_entry(path: Path, run_id: str) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "bytes": len(data),
        "path": f"artifacts/{run_id}/Q30/{path.name}",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _combined_counts(orchestrator_db: Path, catalog_db: Path) -> dict[str, int]:
    counts = {
        f"orch.{name}": value
        for name, value in _count_tables(orchestrator_db, _ORCH_TABLES).items()
    }
    counts.update(
        {
            f"catalog.{name}": value
            for name, value in _count_tables(catalog_db, _CATALOG_TABLES).items()
        }
    )
    return counts


def _count_tables(database: Path, tables: Sequence[str]) -> dict[str, int]:
    conn = sqlite3.connect(database)
    try:
        counts: dict[str, int] = {}
        for table in tables:
            if table not in _ALLOWED_TABLES:
                raise ValueError("unknown table")
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if row is None:
                raise sqlite3.Error(table)
            value = row[0]
            if isinstance(value, bool) or not isinstance(value, int):
                raise sqlite3.Error(table)
            counts[table] = value
        return counts
    finally:
        conn.close()


def _tree_signature(root: Path) -> tuple[int, str]:
    lines: list[str] = []
    count = 0
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            count += 1
            relative = path.relative_to(root).as_posix()
            lines.append(f"{relative}:{_sha256_file(path)}")
    payload = "\n".join(lines).encode("utf-8")
    return count, hashlib.sha256(payload).hexdigest()


def _copy_tree(src: Path, dest: Path) -> None:
    """Copy a backup directory, including sqlite files and the pipeline tree."""
    if not src.exists():
        dest.mkdir(parents=True, exist_ok=True)
        return
    for path in sorted(src.rglob("*")):
        target = dest / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if path.is_file():
            _copy_file(path, target)


def _bundle_sha(parts: Sequence[tuple[str, str]]) -> str:
    body = "\n".join(f"{name}:{digest}" for name, digest in parts)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as source, dest.open("wb") as target:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())


def _sum_stored_results(database: Path) -> tuple[float, int]:
    """Cost is the sum of stored ``cost_usd``. Tokens are the sum of stored token fields."""
    conn = sqlite3.connect(database)
    try:
        rows = conn.execute("SELECT result_json FROM o20_results").fetchall()
    finally:
        conn.close()
    cost = 0.0
    tokens = 0
    for row in rows:
        payload = row[0]
        if not isinstance(payload, str):
            raise sqlite3.Error("result_json must be text")
        loaded: object = json.loads(payload)
        parsed = _object(loaded)
        if parsed is None:
            continue
        amount = parsed.get("cost_usd")
        if isinstance(amount, bool) or not isinstance(amount, int | float):
            continue
        cost += float(amount)
        if "tokens" not in parsed:
            continue
        raw = parsed["tokens"]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError("tokens field is not an integer")
        tokens += raw
    return cost, tokens


def _budget_spent(database: Path) -> float:
    conn = sqlite3.connect(database)
    try:
        row = conn.execute("SELECT COALESCE(SUM(spent_usd), 0) FROM o20_budget").fetchone()
    finally:
        conn.close()
    if row is None:
        return 0.0
    value = row[0]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise sqlite3.Error("spent_usd must be a number")
    return float(value)


def _report_ids(database: Path) -> tuple[str, ...]:
    conn = sqlite3.connect(database)
    try:
        rows = conn.execute("SELECT report_id FROM published_reports ORDER BY report_id").fetchall()
    finally:
        conn.close()
    found: list[str] = []
    for row in rows:
        value = row[0]
        if not isinstance(value, str):
            raise sqlite3.Error("report_id must be text")
        found.append(value)
    return tuple(found)


def _scalar(database: Path, sql: str) -> int:
    if sql != "SELECT COUNT(*) FROM raw_records":
        raise ValueError("unexpected count query")
    conn = sqlite3.connect(database)
    try:
        row = conn.execute(sql).fetchone()
    finally:
        conn.close()
    if row is None:
        raise sqlite3.Error("count returned no row")
    value = row[0]
    if isinstance(value, bool) or not isinstance(value, int):
        raise sqlite3.Error("count must be an integer")
    return value


def _ndjson(records: Sequence[SyntheticRecord]) -> bytes:
    lines = [
        json.dumps(record.as_dict(), separators=(",", ":"), sort_keys=True) for record in records
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _day_start(day: datetime) -> datetime:
    if day.tzinfo is None:
        raise ValueError("day requires a timezone")
    utc = day.astimezone(UTC)
    return datetime(utc.year, utc.month, utc.day, tzinfo=UTC)


def _stamp(day_start: datetime) -> str:
    return _iso(day_start.replace(hour=8))


def _iso(value: datetime) -> str:
    text = value.astimezone(UTC).isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def _object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        parsed[key] = item
    return parsed


def _pretty(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
