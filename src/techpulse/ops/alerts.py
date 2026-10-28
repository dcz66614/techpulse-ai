"""O30 alerts for P0 failure, source policy change, and stale evidence.

Each stored alert carries a run_id and a link to one SOP in ``docs/ops_slo.md``.
Summaries are redacted before they are written. Token and secret values are not
stored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from techpulse.ops.metrics import ensure_schema, log_event, redact_text

P0_FAILURE = "p0_failure"
SOURCE_POLICY_CHANGE = "source_policy_change"
STALE_EVIDENCE = "stale_evidence"

SOP_LINKS: dict[str, str] = {
    P0_FAILURE: "docs/ops_slo.md#p0-failure",
    SOURCE_POLICY_CHANGE: "docs/ops_slo.md#source-policy-change",
    STALE_EVIDENCE: "docs/ops_slo.md#stale-evidence",
}
_SEVERITY = {
    P0_FAILURE: "sev-1",
    SOURCE_POLICY_CHANGE: "sev-2",
    STALE_EVIDENCE: "sev-2",
}


@dataclass(frozen=True)
class Alert:
    """One stored alert."""

    kind: str
    run_id: str
    sop: str
    severity: str
    summary: str
    created_at: str


def raise_alert(
    conn: sqlite3.Connection,
    *,
    kind: str,
    run_id: str,
    summary: str,
) -> Alert:
    """Insert one alert. ``kind`` selects the SOP link. The caller cannot replace it."""
    if kind not in SOP_LINKS:
        raise ValueError("unknown alert kind")
    checked_run = _run_id(run_id)
    safe_summary = redact_text(summary)
    if not safe_summary:
        raise ValueError("alert summary is empty")
    ensure_schema(conn)
    created_at = _iso(datetime.now(UTC))
    sop = SOP_LINKS[kind]
    severity = _SEVERITY[kind]
    conn.execute(
        """
        INSERT INTO o30_alerts (kind, run_id, sop, severity, summary, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (kind, checked_run, sop, severity, safe_summary, created_at),
    )
    log_event(
        conn,
        {
            "event": "alert",
            "kind": kind,
            "run_id": checked_run,
            "severity": severity,
            "sop": sop,
            "summary": safe_summary,
        },
    )
    return Alert(
        kind=kind,
        run_id=checked_run,
        sop=sop,
        severity=severity,
        summary=safe_summary,
        created_at=created_at,
    )


def list_alerts(conn: sqlite3.Connection) -> tuple[Alert, ...]:
    """Return alerts in insertion order."""
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT kind, run_id, sop, severity, summary, created_at
        FROM o30_alerts ORDER BY id
        """
    ).fetchall()
    found: list[Alert] = []
    for row in rows:
        found.append(
            Alert(
                kind=_cell_text(row, "kind"),
                run_id=_cell_text(row, "run_id"),
                sop=_cell_text(row, "sop"),
                severity=_cell_text(row, "severity"),
                summary=_cell_text(row, "summary"),
                created_at=_cell_text(row, "created_at"),
            )
        )
    return tuple(found)


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."}:
        raise ValueError("invalid run_id")
    if any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


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
