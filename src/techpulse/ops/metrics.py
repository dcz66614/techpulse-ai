"""O30 metrics and the daily LLM budget.

Spend, token counts, latency, data volume, QA pass, and anomaly blocks are
stored in SQLite by source, task, model, and customer. The daily cap is $25.
At $20 the mode is degraded and non-essential tier-2 model work is off. At
$25 the circuit breaker opens and non-P0 LLM calls stop.

The budgeter cannot be disabled. If the metrics flag is down, or a read
fails, non-P0 LLM calls pause. Token and secret values are redacted before
any log line is stored. Counts of tokens are numbers, not credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import yaml

from techpulse.governance.policy import redact_secrets

DAILY_CAP_USD = 25.0
DEGRADE_USD = 20.0
STOP_USD = 25.0
DEFAULT_MODEL = "local/template"
TICKET = "O30"
_ALLOWED_MODELS = frozenset({DEFAULT_MODEL})
_BEARER = re.compile(r"(?i)\b(?:authorization|bearer)\b\s*[:=]?\s*\S+")
_DROP_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "authorization",
        "bearer",
        "llm_api_key",
        "github_token",
        "openai_api_key",
    }
)
_DIMENSIONS = {
    "source": "source_id",
    "task": "task_id",
    "model": "model_id",
    "customer": "customer_id",
}
_ROUTES: dict[str, dict[str, str]] = {
    "normal": {
        "tier0": DEFAULT_MODEL,
        "tier1": DEFAULT_MODEL,
        "tier2": DEFAULT_MODEL,
        "tier3": DEFAULT_MODEL,
    },
    "degraded": {
        "tier0": DEFAULT_MODEL,
        "tier1": DEFAULT_MODEL,
        "tier2_essential": DEFAULT_MODEL,
        "tier2_nonessential": "off",
        "tier3": DEFAULT_MODEL,
    },
    "stopped": {
        "non_p0_llm": "off",
        "p0": DEFAULT_MODEL,
        "ingestion": DEFAULT_MODEL,
        "alerts": DEFAULT_MODEL,
        "security": DEFAULT_MODEL,
    },
    "fail_closed": {
        "non_p0_llm": "off",
        "p0": DEFAULT_MODEL,
        "ingestion": DEFAULT_MODEL,
        "alerts": DEFAULT_MODEL,
        "security": DEFAULT_MODEL,
    },
}
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS o30_usage (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        customer_id TEXT NOT NULL,
        agent_role TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cost_usd REAL NOT NULL,
        latency_ms INTEGER NOT NULL,
        data_volume INTEGER NOT NULL,
        qa_pass INTEGER,
        anomaly_blocked INTEGER NOT NULL,
        pricing_snapshot_id TEXT NOT NULL,
        retry_of TEXT,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_o30_usage_source ON o30_usage (source_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_o30_usage_task ON o30_usage (task_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_o30_usage_model ON o30_usage (model_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_o30_usage_customer ON o30_usage (customer_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS o30_budget (
        day TEXT PRIMARY KEY,
        spent_usd REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS o30_control (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        metrics_available INTEGER NOT NULL,
        budgeter_locked INTEGER NOT NULL CHECK (budgeter_locked = 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS o30_alerts (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        run_id TEXT NOT NULL,
        sop TEXT NOT NULL,
        severity TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS o30_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        line TEXT NOT NULL
    )
    """,
)
_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS o30_budgeter_no_disable
BEFORE UPDATE OF budgeter_locked ON o30_control
WHEN NEW.budgeter_locked != 1
BEGIN
    SELECT RAISE(ABORT, 'budgeter cannot be disabled');
END
"""


class BudgeterLocked(RuntimeError):
    """Raised when a caller tries to turn the budgeter off."""


class RouterRejected(ValueError):
    """Raised when the model router names a model or cap this process rejects."""


@dataclass(frozen=True)
class Usage:
    """One measured call. Token fields are counts."""

    run_id: str
    task_id: str
    source_id: str
    model_id: str
    customer_id: str
    agent_role: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    data_volume: int
    qa_pass: bool | None
    anomaly_blocked: bool
    pricing_snapshot_id: str
    retry_of: str | None = None


@dataclass(frozen=True)
class Rollup:
    """Sums for one source, task, model, or customer."""

    key: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    data_volume: int
    qa_pass: int
    qa_fail: int
    anomaly_blocked: int
    samples: int

    def to_dict(self) -> dict[str, object]:
        return {
            "anomaly_blocked": self.anomaly_blocked,
            "cost_usd": self.cost_usd,
            "data_volume": self.data_volume,
            "input_tokens": self.input_tokens,
            "key": self.key,
            "latency_ms": self.latency_ms,
            "output_tokens": self.output_tokens,
            "qa_fail": self.qa_fail,
            "qa_pass": self.qa_pass,
            "samples": self.samples,
        }


@dataclass(frozen=True)
class TaskBudgetSpec:
    """How one pipeline task sits against the budget gate."""

    priority: str
    uses_llm: bool
    tier: int
    essential: bool


@dataclass(frozen=True)
class ModelRouter:
    """The validated route table. The only model is ``local/template``."""

    default_model: str
    models: tuple[str, ...]
    routes: dict[str, dict[str, str]]
    daily_cap_usd: float
    degrade_at_usd: float
    stop_at_usd: float


@dataclass(frozen=True)
class BudgetDecision:
    """The gate for one UTC day."""

    mode: str
    spent_usd: float
    cap_usd: float
    degrade_usd: float
    circuit_open: bool
    metrics_available: bool
    budgeter_locked: bool
    day: str

    def allow(
        self,
        *,
        priority: str,
        uses_llm: bool,
        tier: int = 0,
        essential: bool = True,
    ) -> bool:
        """Return whether this call may run.

        Local non-LLM work always runs. Non-essential tier-2 work stops at
        the degrade line. Non-P0 LLM calls stop when the breaker is open or
        the metrics store is unavailable.
        """
        if not uses_llm:
            return True
        if tier >= 2 and not essential and self.mode in {"degraded", "stopped", "fail_closed"}:
            return False
        if self.mode in {"stopped", "fail_closed"} and priority != "P0":
            return False
        return True

    def allow_task(self, task_id: str) -> bool:
        spec = task_spec(task_id)
        return self.allow(
            priority=spec.priority,
            uses_llm=spec.uses_llm,
            tier=spec.tier,
            essential=spec.essential,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "budgeter_locked": self.budgeter_locked,
            "cap_usd": self.cap_usd,
            "circuit_open": self.circuit_open,
            "day": self.day,
            "degrade_usd": self.degrade_usd,
            "metrics_available": self.metrics_available,
            "mode": self.mode,
            "spent_usd": self.spent_usd,
        }


_DEFAULT_SPEC = TaskBudgetSpec("P0", False, 0, True)
_TASKS: dict[str, TaskBudgetSpec] = {
    "C21": TaskBudgetSpec("P1", False, 0, True),
    "D40": TaskBudgetSpec("P1", False, 0, True),
    "I10": TaskBudgetSpec("P0", True, 0, True),
    "I30": TaskBudgetSpec("P1", False, 0, False),
    "E10": TaskBudgetSpec("P0", True, 2, True),
    "E20": TaskBudgetSpec("P0", True, 2, True),
    "R10": TaskBudgetSpec("P0", True, 2, True),
    "R20": TaskBudgetSpec("P1", True, 2, False),
    "A20": TaskBudgetSpec("P1", False, 0, False),
    "O40": TaskBudgetSpec("P1", False, 0, False),
    "G10": TaskBudgetSpec("P1", False, 0, False),
    "G20": TaskBudgetSpec("P1", False, 0, False),
    "M70": TaskBudgetSpec("P2", True, 2, False),
}


def task_spec(task_id: str) -> TaskBudgetSpec:
    """Return the budget class for a pipeline task. Unknown tasks are non-LLM P0."""
    return _TASKS.get(task_id, _DEFAULT_SPEC)


def repository_root() -> Path:
    """Return the repository root that contains this package."""
    return Path(__file__).resolve().parents[3]


def redact_text(value: str) -> str:
    """Remove token and secret values. Counts and ordinary words stay."""
    cleaned = redact_secrets(value)
    return _BEARER.sub("[REDACTED]", cleaned)


def load_model_router(path: Path | None = None) -> ModelRouter:
    """Load ``config/model_router.yaml`` and reject anything except the local template.

    The budgeter block must say enabled, and it must say it cannot be disabled.
    A higher daily cap in the file is rejected. Paid providers are rejected.
    """
    router_path = path if path is not None else repository_root() / "config" / "model_router.yaml"
    if not router_path.is_file():
        raise RouterRejected("model router file is missing")
    loaded: object = yaml.safe_load(router_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RouterRejected("model router must be a mapping")
    document = cast(dict[object, object], loaded)
    _require_budgeter(document.get("budgeter"))
    cap = _exact_number(document.get("daily_cap_usd"), DAILY_CAP_USD, "daily_cap_usd")
    degrade = _exact_number(document.get("degrade_at_usd"), DEGRADE_USD, "degrade_at_usd")
    stop = _exact_number(document.get("stop_at_usd"), STOP_USD, "stop_at_usd")
    default_model = document.get("default_model")
    if default_model != DEFAULT_MODEL:
        raise RouterRejected("default model must be local/template")
    models = _load_models(document.get("models"))
    routes = _load_routes(document.get("routes"))
    return ModelRouter(
        default_model=DEFAULT_MODEL,
        models=models,
        routes=routes,
        daily_cap_usd=cap,
        degrade_at_usd=degrade,
        stop_at_usd=stop,
    )


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create O30 tables. The budgeter row is inserted locked and is not updated off."""
    for statement in _SCHEMA:
        conn.execute(statement)
    conn.execute(_TRIGGER)
    conn.execute(
        """
        INSERT OR IGNORE INTO o30_control (id, metrics_available, budgeter_locked)
        VALUES (1, 1, 1)
        """
    )


def log_event(conn: sqlite3.Connection, payload: Mapping[str, object]) -> None:
    """Append one JSON log line. Secret keys are dropped. Secret strings are redacted."""
    ensure_schema(conn)
    body = _clean_payload(payload)
    line = json.dumps(body, sort_keys=True, separators=(",", ":"))
    conn.execute(
        "INSERT INTO o30_log (at, line) VALUES (?, ?)",
        (_iso(datetime.now(UTC)), line),
    )


def route_model(
    decision: BudgetDecision,
    *,
    priority: str,
    uses_llm: bool,
    tier: int = 0,
    essential: bool = True,
) -> str | None:
    """Return ``local/template`` or ``None`` when the gate turns the call off."""
    if not decision.allow(priority=priority, uses_llm=uses_llm, tier=tier, essential=essential):
        return None
    return DEFAULT_MODEL


class MetricsStore:
    """SQLite metrics and the budget gate. Closing an owned file is optional."""

    def __init__(
        self,
        database: Path | sqlite3.Connection,
        *,
        router_path: Path | None = None,
        budgeter_enabled: bool = True,
    ) -> None:
        if budgeter_enabled is not True:
            raise BudgeterLocked("budgeter cannot be disabled")
        self._router = load_model_router(router_path)
        if isinstance(database, sqlite3.Connection):
            self._conn = database
            self._owned = False
        else:
            database.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(database)
            self._conn.row_factory = sqlite3.Row
            self._conn.isolation_level = None
            self._owned = True
        if self._conn.row_factory is not sqlite3.Row:
            self._conn.row_factory = sqlite3.Row
        ensure_schema(self._conn)

    def close(self) -> None:
        if self._owned:
            self._conn.close()

    def __enter__(self) -> MetricsStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def router(self) -> ModelRouter:
        return self._router

    def record(self, usage: Usage, *, day: str | None = None) -> BudgetDecision:
        """Insert one usage row and add its cost to the day's spend."""
        try:
            checked = _validate_usage(usage, self._router)
        except ValueError as exc:
            if str(exc).startswith("rejected secret"):
                log_event(self._conn, {"event": "rejected", "message": str(exc)})
            raise
        chosen = day if day is not None else _today()
        qa_pass = None if checked.qa_pass is None else int(checked.qa_pass)
        self._conn.execute(
            """
            INSERT INTO o30_usage (
                run_id, task_id, source_id, model_id, customer_id, agent_role,
                input_tokens, output_tokens, cost_usd, latency_ms, data_volume,
                qa_pass, anomaly_blocked, pricing_snapshot_id, retry_of, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checked.run_id,
                checked.task_id,
                checked.source_id,
                checked.model_id,
                checked.customer_id,
                checked.agent_role,
                checked.input_tokens,
                checked.output_tokens,
                checked.cost_usd,
                checked.latency_ms,
                checked.data_volume,
                qa_pass,
                int(checked.anomaly_blocked),
                checked.pricing_snapshot_id,
                checked.retry_of,
                _iso(datetime.now(UTC)),
            ),
        )
        if checked.cost_usd > 0:
            self._add_budget(checked.cost_usd, chosen)
        log_event(
            self._conn,
            {
                "anomaly_blocked": checked.anomaly_blocked,
                "cost_usd": checked.cost_usd,
                "customer_id": checked.customer_id,
                "data_volume": checked.data_volume,
                "event": "usage",
                "input_tokens": checked.input_tokens,
                "latency_ms": checked.latency_ms,
                "model_id": checked.model_id,
                "output_tokens": checked.output_tokens,
                "qa_pass": checked.qa_pass,
                "run_id": checked.run_id,
                "source_id": checked.source_id,
                "task_id": checked.task_id,
            },
        )
        return self.decision(day=chosen)

    def inject_simulated_spend(self, amount: float, *, day: str | None = None) -> BudgetDecision:
        """Add simulated dollars for one UTC day. No provider is called."""
        chosen = day if day is not None else _today()
        return self.add_spend(amount, day=chosen)

    def add_spend(self, amount: float, *, day: str) -> BudgetDecision:
        """Add dollars that were already spent. Negative amounts are rejected."""
        if isinstance(amount, bool) or not isinstance(amount, int | float):
            raise ValueError("amount must be a number")
        if amount < 0:
            raise ValueError("amount must be >= 0")
        if amount > 0:
            self._add_budget(float(amount), day)
        decision = self.decision(day=day)
        log_event(
            self._conn,
            {
                "added_usd": float(amount),
                "circuit_open": decision.circuit_open,
                "day": day,
                "event": "spend",
                "mode": decision.mode,
                "spent_usd": decision.spent_usd,
            },
        )
        return decision

    def decision(self, *, day: str | None = None) -> BudgetDecision:
        """Read the gate. A failed read fail-closes non-P0 LLM calls."""
        chosen = day if day is not None else _today()
        try:
            available = self._metrics_available()
            spent = self._spent(chosen)
        except sqlite3.Error:
            return _fail_closed(chosen)
        return _decision(spent, available, chosen)

    def rollup(self, dimension: str) -> tuple[Rollup, ...]:
        """Sum usage by source, task, model, or customer."""
        column = _DIMENSIONS.get(dimension)
        if column is None:
            raise ValueError("unknown rollup dimension")
        rows = self._conn.execute(
            f"""
            SELECT {column} AS key,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cost_usd) AS cost_usd,
                   SUM(latency_ms) AS latency_ms,
                   SUM(data_volume) AS data_volume,
                   SUM(CASE WHEN qa_pass = 1 THEN 1 ELSE 0 END) AS qa_pass,
                   SUM(CASE WHEN qa_pass = 0 THEN 1 ELSE 0 END) AS qa_fail,
                   SUM(anomaly_blocked) AS anomaly_blocked,
                   COUNT(*) AS samples
            FROM o30_usage
            GROUP BY {column}
            ORDER BY {column}
            """
        ).fetchall()
        found: list[Rollup] = []
        for row in rows:
            found.append(
                Rollup(
                    key=_cell_text(row, "key"),
                    input_tokens=_cell_int(row, "input_tokens"),
                    output_tokens=_cell_int(row, "output_tokens"),
                    cost_usd=_cell_float(row, "cost_usd"),
                    latency_ms=_cell_int(row, "latency_ms"),
                    data_volume=_cell_int(row, "data_volume"),
                    qa_pass=_cell_int(row, "qa_pass"),
                    qa_fail=_cell_int(row, "qa_fail"),
                    anomaly_blocked=_cell_int(row, "anomaly_blocked"),
                    samples=_cell_int(row, "samples"),
                )
            )
        return tuple(found)

    def set_metrics_available(self, available: bool) -> None:
        """Mark the metrics store up or down. This does not unlock the budgeter."""
        self._conn.execute(
            "UPDATE o30_control SET metrics_available = ? WHERE id = 1",
            (1 if available else 0,),
        )

    def log_event(self, payload: Mapping[str, object]) -> None:
        log_event(self._conn, payload)

    def log_text(self) -> str:
        rows = self._conn.execute("SELECT line FROM o30_log ORDER BY id").fetchall()
        return "\n".join(_cell_text(row, "line") for row in rows)

    def stored_text(self) -> str:
        """Every text column, for the secret scan."""
        parts = [self.log_text()]
        queries = (
            "SELECT run_id, task_id, source_id, model_id, customer_id, agent_role, "
            "pricing_snapshot_id, retry_of FROM o30_usage",
            "SELECT kind, run_id, sop, severity, summary FROM o30_alerts",
        )
        for query in queries:
            for row in self._conn.execute(query).fetchall():
                parts.extend(value for value in row if isinstance(value, str))
        return "\n".join(parts)

    def _metrics_available(self) -> bool:
        row = self._conn.execute(
            "SELECT metrics_available, budgeter_locked FROM o30_control WHERE id = 1"
        ).fetchone()
        if row is None:
            return False
        if _cell_int(row, "budgeter_locked") != 1:
            raise BudgeterLocked("budgeter cannot be disabled")
        return _cell_int(row, "metrics_available") == 1

    def _spent(self, day: str) -> float:
        row = self._conn.execute(
            "SELECT spent_usd FROM o30_budget WHERE day = ?",
            (day,),
        ).fetchone()
        if row is None:
            return 0.0
        return _cell_float(row, "spent_usd")

    def _add_budget(self, amount: float, day: str) -> None:
        self._conn.execute(
            """
            INSERT INTO o30_budget (day, spent_usd) VALUES (?, ?)
            ON CONFLICT(day) DO UPDATE SET spent_usd = spent_usd + excluded.spent_usd
            """,
            (day, amount),
        )


class BudgetHook:
    """The O20 runtime calls this. It has no method that disables the budgeter."""

    def __init__(self, conn: sqlite3.Connection, *, router_path: Path | None = None) -> None:
        self._store = MetricsStore(conn, router_path=router_path)

    def add_spend(self, amount: float, *, day: str) -> BudgetDecision:
        return self._store.add_spend(amount, day=day)

    def decision(self, *, day: str) -> BudgetDecision:
        try:
            return self._store.decision(day=day)
        except sqlite3.Error:
            return _fail_closed(day)

    def set_metrics_available(self, available: bool) -> None:
        self._store.set_metrics_available(available)

    def observe_activity(
        self,
        *,
        run_id: str,
        task_id: str,
        source_id: str,
        cost_usd: float,
        qa_pass: bool | None,
        anomaly_blocked: bool,
        data_volume: int,
        agent_role: str,
        day: str,
    ) -> None:
        """Record an activity that already reported a cost. Token counts stay 0."""
        self._store.record(
            Usage(
                run_id=run_id,
                task_id=task_id,
                source_id=source_id,
                model_id=DEFAULT_MODEL,
                customer_id="internal",
                agent_role=agent_role,
                input_tokens=0,
                output_tokens=0,
                cost_usd=cost_usd,
                latency_ms=0,
                data_volume=data_volume,
                qa_pass=qa_pass,
                anomaly_blocked=anomaly_blocked,
                pricing_snapshot_id="none",
            ),
            day=day,
        )

    def log_event(self, payload: Mapping[str, object]) -> None:
        self._store.log_event(payload)


def publish_o30(artifacts_dir: Path, run_id: str) -> Path:
    """Write ``artifacts/{run_id}/O30`` from a local fixture. No network calls."""
    from techpulse.ops.alerts import list_alerts

    checked = _artifact_run_id(run_id)
    directory = artifacts_dir / checked / "O30"
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "o30.sqlite"
    if database.exists():
        database.unlink()
    with MetricsStore(database) as store:
        _seed_fixture(store, checked)
        probes = _probes()
        metrics = _metrics_body(store, checked, probes)
        log_body = "".join(f"{line}\n" for line in store.log_text().splitlines())
        _write(directory / "metrics.json", _pretty(metrics))
        _write(directory / "log.jsonl", log_body)
        manifest = {
            "alerts": [
                {"kind": item.kind, "run_id": item.run_id, "sop": item.sop}
                for item in list_alerts(store.connection)
            ],
            "budgeter_locked": True,
            "default_model": DEFAULT_MODEL,
            "errors": [],
            "files": [
                _file_entry(directory / "metrics.json", checked),
                _file_entry(directory / "log.jsonl", checked),
            ],
            "paid_models": [],
            "probes": probes,
            "run_id": checked,
            "status": "success" if _probes_ok(probes) else "failed",
            "ticket": TICKET,
        }
        _write(directory / "manifest.json", _pretty(manifest))
    return directory


def _seed_fixture(store: MetricsStore, run_id: str) -> None:
    rows = (
        Usage(
            run_id=run_id,
            task_id="C10",
            source_id="openalex",
            model_id=DEFAULT_MODEL,
            customer_id="internal",
            agent_role="CollectorAgent",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            latency_ms=12,
            data_volume=40,
            qa_pass=None,
            anomaly_blocked=False,
            pricing_snapshot_id="none",
        ),
        Usage(
            run_id=run_id,
            task_id="R10",
            source_id="none",
            model_id=DEFAULT_MODEL,
            customer_id="internal",
            agent_role="WriterAgent",
            input_tokens=120,
            output_tokens=40,
            cost_usd=0.0,
            latency_ms=30,
            data_volume=8,
            qa_pass=True,
            anomaly_blocked=False,
            pricing_snapshot_id="none",
        ),
        Usage(
            run_id=run_id,
            task_id="C20",
            source_id="github",
            model_id=DEFAULT_MODEL,
            customer_id="cust-fixture",
            agent_role="CollectorAgent",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            latency_ms=8,
            data_volume=15,
            qa_pass=False,
            anomaly_blocked=True,
            pricing_snapshot_id="none",
        ),
    )
    for usage in rows:
        store.record(usage, day="2026-10-28")
    from techpulse.ops.alerts import raise_alert

    raise_alert(
        store.connection,
        kind="p0_failure",
        run_id=run_id,
        summary="P0 step failed; checkpoint kept",
    )
    raise_alert(
        store.connection,
        kind="source_policy_change",
        run_id=run_id,
        summary="source github policy snapshot changed",
    )
    raise_alert(
        store.connection,
        kind="stale_evidence",
        run_id=run_id,
        summary="evidence snapshot is stale",
    )


def _probes() -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for amount in (20.0, 25.0):
        decision = _probe_amount(amount)
        found.append(
            {
                "allow_non_p0_llm": decision.allow(priority="P1", uses_llm=True, tier=1),
                "circuit_open": decision.circuit_open,
                "injected_usd": amount,
                "mode": decision.mode,
                "tier2_nonessential": decision.allow(
                    priority="P1", uses_llm=True, tier=2, essential=False
                ),
            }
        )
    closed = _probe_unavailable()
    found.append(
        {
            "allow_non_p0_llm": closed.allow(priority="P1", uses_llm=True, tier=1),
            "allow_p0_llm": closed.allow(priority="P0", uses_llm=True, tier=1),
            "circuit_open": closed.circuit_open,
            "injected_usd": 0.0,
            "mode": closed.mode,
            "tier2_nonessential": closed.allow(
                priority="P1", uses_llm=True, tier=2, essential=False
            ),
        }
    )
    return found


def _probe_amount(amount: float) -> BudgetDecision:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        store = MetricsStore(conn)
        return store.inject_simulated_spend(amount, day="2026-10-28")
    finally:
        conn.close()


def _probe_unavailable() -> BudgetDecision:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        store = MetricsStore(conn)
        store.set_metrics_available(False)
        return store.decision(day="2026-10-28")
    finally:
        conn.close()


def _probes_ok(probes: list[dict[str, object]]) -> bool:
    if len(probes) != 3:
        return False
    degrade, stop, closed = probes
    return (
        degrade.get("mode") == "degraded"
        and degrade.get("circuit_open") is False
        and degrade.get("tier2_nonessential") is False
        and stop.get("mode") == "stopped"
        and stop.get("circuit_open") is True
        and stop.get("allow_non_p0_llm") is False
        and closed.get("mode") == "fail_closed"
        and closed.get("allow_non_p0_llm") is False
        and closed.get("allow_p0_llm") is True
    )


def _metrics_body(
    store: MetricsStore,
    run_id: str,
    probes: list[dict[str, object]],
) -> dict[str, object]:
    decision = store.decision(day="2026-10-28")
    return {
        "budgeter_locked": decision.budgeter_locked,
        "daily_cap_usd": DAILY_CAP_USD,
        "degrade_usd": DEGRADE_USD,
        "default_model": DEFAULT_MODEL,
        "mode": decision.mode,
        "models": list(store.router.models),
        "network_calls": 0,
        "paid_apis": [],
        "probes": probes,
        "rollups": {
            name: [item.to_dict() for item in store.rollup(name)]
            for name in ("source", "task", "model", "customer")
        },
        "run_id": run_id,
        "spent_usd": decision.spent_usd,
        "stop_usd": STOP_USD,
        "ticket": TICKET,
    }


def _decision(spent: float, available: bool, day: str) -> BudgetDecision:
    if not available:
        mode = "fail_closed"
    elif spent >= STOP_USD:
        mode = "stopped"
    elif spent >= DEGRADE_USD:
        mode = "degraded"
    else:
        mode = "normal"
    return BudgetDecision(
        mode=mode,
        spent_usd=spent,
        cap_usd=DAILY_CAP_USD,
        degrade_usd=DEGRADE_USD,
        circuit_open=mode == "stopped",
        metrics_available=available,
        budgeter_locked=True,
        day=day,
    )


def _fail_closed(day: str) -> BudgetDecision:
    return _decision(0.0, False, day)


def _validate_usage(usage: Usage, router: ModelRouter) -> Usage:
    run_id = _plain_id(usage.run_id, "run_id")
    task_id = _plain_id(usage.task_id, "task_id")
    source_id = _plain_id(usage.source_id, "source_id")
    model_id = _plain_id(usage.model_id, "model_id")
    customer_id = _plain_id(usage.customer_id, "customer_id")
    agent_role = _plain_id(usage.agent_role, "agent_role")
    pricing = _plain_id(usage.pricing_snapshot_id, "pricing_snapshot_id")
    if model_id not in router.models:
        raise RouterRejected("model is not allowed")
    retry_of = usage.retry_of
    if retry_of is not None:
        retry_of = _plain_id(retry_of, "retry_of")
    if usage.qa_pass is not None and not isinstance(usage.qa_pass, bool):
        raise ValueError("qa_pass must be a boolean or null")
    if not isinstance(usage.anomaly_blocked, bool):
        raise ValueError("anomaly_blocked must be a boolean")
    cost = usage.cost_usd
    if isinstance(cost, bool) or not isinstance(cost, int | float) or float(cost) < 0:
        raise ValueError("cost_usd must be >= 0")
    return Usage(
        run_id=run_id,
        task_id=task_id,
        source_id=source_id,
        model_id=model_id,
        customer_id=customer_id,
        agent_role=agent_role,
        input_tokens=_count(usage.input_tokens, "input_tokens"),
        output_tokens=_count(usage.output_tokens, "output_tokens"),
        cost_usd=float(cost),
        latency_ms=_count(usage.latency_ms, "latency_ms"),
        data_volume=_count(usage.data_volume, "data_volume"),
        qa_pass=usage.qa_pass,
        anomaly_blocked=usage.anomaly_blocked,
        pricing_snapshot_id=pricing,
        retry_of=retry_of,
    )


def _plain_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    if _looks_secret(value):
        log_note = f"rejected secret in {field}"
        raise ValueError(log_note)
    return value.strip()


def _looks_secret(value: str) -> bool:
    if redact_text(value) != value:
        return True
    return False


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _clean_payload(payload: Mapping[str, object]) -> dict[str, object]:
    clean: dict[str, object] = {}
    for key, value in payload.items():
        if _secret_key(key):
            continue
        if isinstance(value, str):
            clean[key] = redact_text(value)
        elif isinstance(value, bool | int | float) or value is None:
            clean[key] = value
        else:
            clean[key] = redact_text(str(value))
    return clean


def _secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _DROP_KEYS:
        return True
    return lowered.endswith(("_token", "_secret", "_api_key"))


def _require_budgeter(value: object) -> None:
    if not isinstance(value, dict):
        raise BudgeterLocked("budgeter cannot be disabled")
    block = cast(dict[object, object], value)
    if block.get("enabled") is not True or block.get("disable_allowed") is not False:
        raise BudgeterLocked("budgeter cannot be disabled")


def _exact_number(value: object, expected: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RouterRejected(f"{field} must match the fixed cap")
    if float(value) != expected:
        raise RouterRejected(f"{field} must match the fixed cap")
    return expected


def _load_models(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != 1:
        raise RouterRejected("router must list only local/template")
    item = value[0]
    if not isinstance(item, dict):
        raise RouterRejected("router must list only local/template")
    model = cast(dict[object, object], item)
    if (
        model.get("id") != DEFAULT_MODEL
        or model.get("provider") != "local"
        or model.get("paid") is not False
        or model.get("default") is not True
        or model.get("cloud_llm") is not False
    ):
        raise RouterRejected("router must list only local/template")
    return (DEFAULT_MODEL,)


def _load_routes(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise RouterRejected("routes are missing")
    document = cast(dict[object, object], value)
    parsed: dict[str, dict[str, str]] = {}
    if set(document) != set(_ROUTES):
        raise RouterRejected("routes do not match the fixed table")
    for mode, expected in _ROUTES.items():
        raw = document.get(mode)
        if not isinstance(raw, dict):
            raise RouterRejected("routes do not match the fixed table")
        block = cast(dict[object, object], raw)
        current: dict[str, str] = {}
        if set(block) != set(expected):
            raise RouterRejected("routes do not match the fixed table")
        for key, leaf in expected.items():
            found = block.get(key)
            if found != leaf or found not in _ALLOWED_MODELS | {"off"}:
                raise RouterRejected("routes do not match the fixed table")
            if not isinstance(found, str):
                raise RouterRejected("routes do not match the fixed table")
            current[key] = found
        parsed[mode] = current
    return parsed


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _iso(value: datetime) -> str:
    text = value.astimezone(UTC).isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def _cell_text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise sqlite3.Error(f"expected text {key}")
    return value


def _cell_int(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool):
        raise sqlite3.Error(f"expected integer {key}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise sqlite3.Error(f"expected integer {key}")


def _cell_float(row: sqlite3.Row, key: str) -> float:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise sqlite3.Error(f"expected number {key}")
    return float(value)


def _artifact_run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."}:
        raise ValueError("invalid run_id")
    if any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _file_entry(path: Path, run_id: str) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "bytes": len(data),
        "path": f"artifacts/{run_id}/O30/{path.name}",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _pretty(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
