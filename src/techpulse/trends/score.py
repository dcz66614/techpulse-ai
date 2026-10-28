"""T30 trend score, confidence, and false-positive suppression.

The score is the section 7.3 linear formula. Each feature is clipped to
[0, 1] and the anomaly penalty to [0, 0.30]. The same number is recovered
by applying those weights to the stored feature fields.

High confidence requires at least three source families, at least two
Tier-A evidence ids, a complete citation chain, a finished contrary search,
and a passing safety QA. One source family is never high. Missing history
or missing calibration yields an experimental score and confidence low.
A non-finite input is a fatal error and writes no trend rows.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from techpulse.contracts import Trend
from techpulse.workflows.activities import LocalObjectStore, ObjectStore

WEIGHTS: tuple[tuple[str, float], ...] = (
    ("normalized_growth_7d", 0.25),
    ("cross_source_diversity", 0.20),
    ("source_quality", 0.20),
    ("novelty", 0.15),
    ("persistence_28d", 0.10),
    ("developer_activity", 0.10),
)
PENALTY_CAP = 0.30
_UNKNOWN_FLAGS = {
    "normalized_growth_7d": "growth_unknown",
    "source_quality": "quality_unknown",
    "novelty": "novelty_unknown",
    "developer_activity": "developer_unknown",
}
_CONTRACT_FIELDS = (
    "trend_id",
    "topic",
    "window_days",
    "score",
    "confidence",
    "source_families",
    "claim",
    "supporting_evidence_ids",
    "contrary_evidence_ids",
    "numeric_checks_passed",
    "last_verified_at",
)
_DDL = """
CREATE TABLE IF NOT EXISTS trend_scores (
    trend_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    window_days INTEGER NOT NULL CHECK (window_days IN (7, 28)),
    score REAL NOT NULL,
    confidence TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    features_json TEXT NOT NULL,
    source_families TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ScoreResult:
    trends: tuple[dict[str, object], ...]
    status: str
    errors: tuple[str, ...]


def score_trends(
    features: Sequence[Mapping[str, object]],
    *,
    calibration: Sequence[Mapping[str, object]] | None = None,
) -> ScoreResult:
    """Score each feature row for the 7-day and 28-day windows."""
    by_topic = _calibration_index(calibration)
    trends: list[dict[str, object]] = []
    for row in features:
        try:
            trends.extend(_score_one(row, by_topic))
        except _Fatal as exc:
            return ScoreResult((), "fatal_error", (exc.message,))
    trends.sort(key=lambda item: (-_as_float(item["score"]), str(item["topic_id"]), _window(item)))
    return ScoreResult(tuple(trends), "success", ())


def run_score(
    features: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    artifacts_dir: Path,
    store: ObjectStore | None = None,
    database: Path | None = None,
    calibration: Sequence[Mapping[str, object]] | None = None,
) -> ScoreResult:
    """Write ``{run_id}/T30`` trends, manifest, and metrics. Scores also go to SQLite."""
    result = score_trends(features, calibration=calibration)
    object_store = LocalObjectStore(artifacts_dir) if store is None else store
    database_path = (
        database if database is not None else artifacts_dir / run_id / "T30" / "trends.sqlite"
    )
    if result.status != "success":
        metrics: dict[str, object] = {
            "trends": 0,
            "status": result.status,
            "errors": list(result.errors),
            "third_party_writes": 0,
        }
        manifest: dict[str, object] = {
            "ticket": "T30",
            "run_id": run_id,
            "status": result.status,
            "errors": list(result.errors),
            "trends": None,
        }
        object_store.put(f"{run_id}/T30/metrics.json", _json_bytes(metrics))
        object_store.put(f"{run_id}/T30/manifest.json", _json_bytes(manifest))
        return result
    _save(database_path, result.trends)
    body = _jsonl(result.trends)
    object_store.put(f"{run_id}/T30/trends.jsonl", body)
    high = sum(1 for trend in result.trends if trend.get("confidence") == "high")
    metrics = {
        "trends": len(result.trends),
        "high_confidence": high,
        "experimental": sum(1 for trend in result.trends if trend.get("experimental") is True),
        "status": "success",
        "third_party_writes": 0,
    }
    manifest = {
        "ticket": "T30",
        "run_id": run_id,
        "status": "success",
        "trends": "trends.jsonl",
        "trends_sha256": hashlib.sha256(body).hexdigest(),
        "database": database_path.name,
        "weights": {name: weight for name, weight in WEIGHTS},
        "penalty_cap": PENALTY_CAP,
    }
    object_store.put(f"{run_id}/T30/metrics.json", _json_bytes(metrics))
    object_store.put(f"{run_id}/T30/manifest.json", _json_bytes(manifest))
    return result


def _score_one(
    row: Mapping[str, object],
    calibration: Mapping[str, Mapping[str, object]] | None,
) -> list[dict[str, object]]:
    topic_id = row.get("topic_id")
    if not isinstance(topic_id, str) or not topic_id.strip():
        raise _Fatal("feature row is missing topic_id")
    as_of = row.get("as_of")
    if not isinstance(as_of, str) or len(as_of) < 10:
        raise _Fatal("feature row is missing as_of")
    components, history_gap = _components(row)
    penalty = _penalty(row)
    _require_penalty_reasons(row, penalty)
    score = sum(weight * components[name] for name, weight in WEIGHTS) - penalty
    if not math.isfinite(score):
        raise _Fatal("score is not finite")
    families = _families(row)
    evidence = calibration.get(topic_id) if calibration is not None else None
    experimental = history_gap or evidence is None
    confidence = _confidence(families, evidence, experimental)
    if confidence == "high" and (len(families) < 3 or _tier_a(evidence) < 2):
        raise _Fatal("high confidence failed its source gates")
    support, contrary = _evidence_ids(evidence)
    breakdown: dict[str, object] = {
        "weights": {name: weight for name, weight in WEIGHTS},
        "components": components,
        "anomaly_penalty": penalty,
        "anomaly_reasons": _reasons(row),
        "inputs": {name: row.get(name) for name, _weight in WEIGHTS},
    }
    trends: list[dict[str, object]] = []
    for window in (7, 28):
        trend: dict[str, object] = {
            "trend_id": f"trend:{topic_id}:{window}:{as_of[:10]}",
            "topic_id": topic_id,
            "topic": topic_id,
            "window_days": window,
            "score": score,
            "experimental_score": score if experimental else None,
            "experimental": experimental,
            "score_kind": "experimental" if experimental else "calibrated",
            "confidence": confidence,
            "source_families": families,
            "claim": _claim(topic_id, window, row),
            "supporting_evidence_ids": support,
            "contrary_evidence_ids": contrary,
            "numeric_checks_passed": True,
            "last_verified_at": as_of,
            "breakdown": breakdown,
        }
        Trend.model_validate({key: trend[key] for key in _CONTRACT_FIELDS})
        trends.append(trend)
    return trends


def _components(row: Mapping[str, object]) -> tuple[dict[str, float], bool]:
    values: dict[str, float] = {}
    history_gap = False
    for name, _weight in WEIGHTS:
        number, missing, fatal = _unit_interval(row.get(name))
        if fatal:
            raise _Fatal(f"{name} is not a finite number")
        unknown = row.get(_UNKNOWN_FLAGS.get(name, "")) is True or missing
        if unknown:
            history_gap = True
            values[name] = 0.0
        else:
            values[name] = number if number is not None else 0.0
    return values, history_gap


def _penalty(row: Mapping[str, object]) -> float:
    number, missing, fatal = _finite(row.get("anomaly_penalty"))
    if fatal or missing:
        raise _Fatal("anomaly_penalty is not a finite number")
    if number is None:
        raise _Fatal("anomaly_penalty is not a finite number")
    return min(PENALTY_CAP, max(0.0, number))


def _require_penalty_reasons(row: Mapping[str, object], penalty: float) -> None:
    reasons = _reasons(row)
    if penalty == 0.0:
        return
    if not reasons:
        raise _Fatal("anomaly penalty has no reason")
    for reason in reasons:
        if "metric" not in reason or "value" not in reason:
            raise _Fatal("anomaly penalty reason is missing its metric")
        _value, missing, fatal = _finite(reason.get("value"))
        if fatal or missing:
            raise _Fatal("anomaly penalty metric is not finite")


def _confidence(
    families: Sequence[str],
    evidence: Mapping[str, object] | None,
    experimental: bool,
) -> str:
    if experimental or evidence is None or len(families) < 2:
        return "low"
    tier_a = _tier_a(evidence)
    gates = (
        len(families) >= 3
        and tier_a >= 2
        and evidence.get("citation_chain_complete") is True
        and evidence.get("contrary_search_executed") is True
        and evidence.get("qa_pass") is True
    )
    if gates and len(families) != 1:
        return "high"
    return "medium"


def _claim(topic_id: str, window: int, row: Mapping[str, object]) -> str:
    if window == 28:
        active = _int_field(row, "persistence_numerator")
        return f"Topic {topic_id} has independent records on {active} of 28 UTC days."
    current = _int_field(row, "growth_current")
    previous = _int_field(row, "growth_previous")
    if row.get("growth_unknown") is True:
        return (
            f"Growth for {topic_id} is unknown because the previous 7-day window "
            f"has {previous} independent records."
        )
    return (
        f"Independent records for {topic_id} moved from {previous} to {current} "
        f"across the UTC 7-day window."
    )


def _families(row: Mapping[str, object]) -> list[str]:
    raw = row.get("source_families")
    if not isinstance(raw, list):
        return []
    allowed = {"academic", "code", "community", "news"}
    found: list[str] = []
    for item in raw:
        if isinstance(item, str) and item in allowed and item not in found:
            found.append(item)
    return found


def _evidence_ids(evidence: Mapping[str, object] | None) -> tuple[list[str], list[str]]:
    if evidence is None:
        return [], []
    return _string_list(evidence.get("supporting_evidence_ids")), _string_list(
        evidence.get("contrary_evidence_ids")
    )


def _tier_a(evidence: Mapping[str, object] | None) -> int:
    if evidence is None:
        return 0
    return len(_string_list(evidence.get("tier_a_evidence_ids")))


def _reasons(row: Mapping[str, object]) -> list[dict[str, object]]:
    raw = row.get("anomaly_reasons")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _Fatal("anomaly_reasons is not a list")
    reasons: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _Fatal("anomaly reason is not an object")
        reasons.append({str(key): value for key, value in item.items()})
    return reasons


def _calibration_index(
    calibration: Sequence[Mapping[str, object]] | None,
) -> dict[str, Mapping[str, object]] | None:
    if calibration is None:
        return None
    indexed: dict[str, Mapping[str, object]] = {}
    for item in calibration:
        topic_id = item.get("topic_id")
        if isinstance(topic_id, str) and topic_id:
            indexed[topic_id] = item
    return indexed


def _unit_interval(value: object) -> tuple[float | None, bool, bool]:
    if value is None:
        return None, True, False
    return _finite(value)


def _finite(value: object) -> tuple[float | None, bool, bool]:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None, False, True
    number = float(value)
    if not math.isfinite(number):
        return None, False, True
    return min(1.0, max(0.0, number)), False, False


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    found: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in found:
            found.append(item)
    return found


def _int_field(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _window(trend: Mapping[str, object]) -> int:
    value = trend.get("window_days")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


class _Fatal(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _save(database: Path, trends: Sequence[Mapping[str, object]]) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.executescript(_DDL)
        with connection:
            connection.execute("DELETE FROM trend_scores")
            for trend in trends:
                connection.execute(
                    """
                    INSERT INTO trend_scores (
                        trend_id, topic, window_days, score, confidence,
                        scored_at, features_json, source_families
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(trend["trend_id"]),
                        str(trend["topic"]),
                        int(_window(trend)),
                        _as_float(trend["score"]),
                        str(trend["confidence"]),
                        str(trend["last_verified_at"]),
                        json.dumps(trend["breakdown"], ensure_ascii=False, sort_keys=True),
                        json.dumps(trend["source_families"], ensure_ascii=False, sort_keys=True),
                    ),
                )
    finally:
        connection.close()


def _jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
