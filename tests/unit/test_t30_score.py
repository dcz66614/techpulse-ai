"""T30 scores. Expected values are the section 7.3 formula on fixture counts."""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from techpulse.contracts import Trend
from techpulse.trends.features import MIN_BASELINE, PENALTY_UNIT, build_features, window_bounds
from techpulse.trends.score import PENALTY_CAP, WEIGHTS, run_score, score_trends

AS_OF = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
AS_OF_TEXT = "2026-10-28T15:00:00Z"
TIER_WEIGHT = {"A": 1.0, "B": 0.5, "C": 0.0}
SPEC_WEIGHTS = {
    "normalized_growth_7d": 0.25,
    "cross_source_diversity": 0.20,
    "source_quality": 0.20,
    "novelty": 0.15,
    "persistence_28d": 0.10,
    "developer_activity": 0.10,
}


def test_score_is_the_formula_on_fixture_counts() -> None:
    previous, current = _balanced()
    records = [*previous, *current]
    feature = build_features(records, as_of=AS_OF).rows[0]
    expected = _formula(
        growth=(len(current) - len(previous)) / len(previous),
        diversity=len({str(item["source_family"]) for item in current}) / 4,
        quality=sum(TIER_WEIGHT[str(item["quality_tier"])] for item in current) / len(current),
        novelty=_novel(previous, current) / len(current),
        persistence=len(_independent_dates(records, 28)) / 28,
        developer=sum(1 for item in current if item["source_family"] == "code") / len(current),
        penalty=0.0,
    )
    assert len(previous) >= MIN_BASELINE
    result = score_trends([feature], calibration=[_calibration("topic:rag")])
    assert result.status == "success"
    assert {trend["window_days"] for trend in result.trends} == {7, 28}
    assert all(trend["score"] == expected for trend in result.trends)
    assert all(trend["score_kind"] == "calibrated" for trend in result.trends)
    assert all(trend["experimental"] is False for trend in result.trends)
    seven = next(trend for trend in result.trends if trend["window_days"] == 7)
    Trend.model_validate({key: seven[key] for key in Trend.model_fields})


def test_stored_breakdown_recomputes_the_same_score() -> None:
    feature = build_features([*_balanced()[0], *_balanced()[1]], as_of=AS_OF).rows[0]
    result = score_trends([feature], calibration=[_calibration("topic:rag")])
    for trend in result.trends:
        breakdown = _object(trend["breakdown"])
        weights = _object(breakdown["weights"])
        components = _object(breakdown["components"])
        recomputed = 0.0
        for name, _weight in WEIGHTS:
            recomputed += _number(weights[name]) * _number(components[name])
        recomputed -= _number(breakdown["anomaly_penalty"])
        assert trend["score"] == recomputed
        assert {name: _number(weights[name]) for name, _weight in WEIGHTS} == SPEC_WEIGHTS


def test_high_confidence_needs_three_families_and_two_tier_a() -> None:
    feature = build_features([*_balanced()[0], *_balanced()[1]], as_of=AS_OF).rows[0]
    high = score_trends([feature], calibration=[_calibration("topic:rag")])
    assert {trend["confidence"] for trend in high.trends} == {"high"}
    weak = score_trends(
        [feature],
        calibration=[_calibration("topic:rag", tier_a=("ev1",), qa_pass=False)],
    )
    assert {trend["confidence"] for trend in weak.trends} == {"medium"}
    assert all(trend["confidence"] != "high" for trend in weak.trends)


def test_single_source_spike_is_never_high() -> None:
    previous = [
        _row(f"p{index}", f"2026-10-{day}T00:00:00Z", family="code", source_id="github")
        for index, day in enumerate((15, 16, 17, 18), start=1)
    ]
    current = [
        _row(f"c{index}", f"2026-10-{day}T00:00:00Z", family="code", source_id="github")
        for index, day in enumerate(range(22, 30), start=1)
    ]
    assert len(current) > len(previous)
    assert (len(current) - len(previous)) / len(previous) > 0
    feature = build_features([*previous, *current], as_of=AS_OF).rows[0]
    assert feature["source_families"] == ["code"]
    assert feature["growth_unknown"] is False
    result = score_trends([feature], calibration=[_calibration("topic:rag", families_note=True)])
    assert result.status == "success"
    assert {trend["confidence"] for trend in result.trends} == {"low"}
    assert all(trend["confidence"] != "high" for trend in result.trends)


def test_missing_calibration_is_experimental_and_low() -> None:
    feature = build_features([*_balanced()[0], *_balanced()[1]], as_of=AS_OF).rows[0]
    result = score_trends([feature])
    assert {trend["confidence"] for trend in result.trends} == {"low"}
    assert all(trend["experimental"] is True for trend in result.trends)
    assert all(trend["score_kind"] == "experimental" for trend in result.trends)
    assert all(trend["experimental_score"] == trend["score"] for trend in result.trends)


def test_small_sample_is_experimental_even_with_calibration() -> None:
    previous = [_row("p1", "2026-10-15T00:00:00Z"), _row("p2", "2026-10-16T00:00:00Z")]
    current = [
        _row("c1", "2026-10-22T00:00:00Z", family="academic", source_id="openalex"),
        _row("c2", "2026-10-23T00:00:00Z", family="code", source_id="github"),
        _row("c3", "2026-10-24T00:00:00Z", family="community", source_id="hn"),
    ]
    assert len(previous) < MIN_BASELINE
    feature = build_features([*previous, *current], as_of=AS_OF).rows[0]
    assert feature["normalized_growth_7d"] is None
    result = score_trends([feature], calibration=[_calibration("topic:rag")])
    assert {trend["confidence"] for trend in result.trends} == {"low"}
    assert all(trend["experimental"] is True for trend in result.trends)
    records = [*previous, *current]
    growth_part = 0.0
    diversity = len({str(item["source_family"]) for item in current}) / 4
    quality = sum(TIER_WEIGHT[str(item["quality_tier"])] for item in current) / len(current)
    novelty = len(current) / len(current)
    persistence = len(_independent_dates(records, 28)) / 28
    developer = sum(1 for item in current if item["source_family"] == "code") / len(current)
    small_base = len(previous) < MIN_BASELINE and len(current) > len(previous)
    penalty = min(PENALTY_CAP, PENALTY_UNIT) if small_base else 0.0
    expected = _formula(growth_part, diversity, quality, novelty, persistence, developer, penalty)
    assert all(trend["score"] == expected for trend in result.trends)


def test_sort_is_deterministic() -> None:
    low = _hand("topic:beta", growth=0.0, families=["academic", "code"])
    high = _hand("topic:alpha", growth=1.0, families=["academic", "code", "community"])
    calibration = [
        _calibration("topic:alpha"),
        _calibration("topic:beta"),
    ]
    first = score_trends([low, high], calibration=calibration)
    second = score_trends([high, low], calibration=calibration)
    assert [trend["trend_id"] for trend in first.trends] == [
        trend["trend_id"] for trend in second.trends
    ]
    scores = [_number(trend["score"]) for trend in first.trends]
    assert scores == sorted(scores, reverse=True)
    assert [trend["topic_id"] for trend in first.trends] == [
        "topic:alpha",
        "topic:alpha",
        "topic:beta",
        "topic:beta",
    ]
    assert [trend["window_days"] for trend in first.trends] == [7, 28, 7, 28]


def test_nan_is_fatal_and_writes_no_trends() -> None:
    feature = dict(_hand("topic:rag", growth=0.5, families=["academic", "code", "news"]))
    feature["normalized_growth_7d"] = float("nan")
    feature["growth_unknown"] = False
    result = score_trends([feature], calibration=[_calibration("topic:rag")])
    assert result.status == "fatal_error"
    assert result.trends == ()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        written = run_score(
            [feature],
            run_id="t30-nan",
            artifacts_dir=root,
            calibration=[_calibration("topic:rag")],
        )
        manifest = json.loads(
            (root / "t30-nan" / "T30" / "manifest.json").read_text(encoding="utf-8")
        )
        assert written.status == "fatal_error"
        assert manifest["status"] == "fatal_error"
        assert not (root / "t30-nan" / "T30" / "trends.jsonl").exists()


def test_penalty_without_a_metric_is_fatal() -> None:
    feature = _hand("topic:rag", growth=0.5, families=["academic", "code"])
    feature["anomaly_penalty"] = 0.1
    feature["anomaly_reasons"] = [{"code": "reprint", "value": 0.1}]
    result = score_trends([feature], calibration=[_calibration("topic:rag")])
    assert result.status == "fatal_error"
    assert result.trends == ()


def test_run_score_sqlite_matches_the_formula() -> None:
    feature = build_features([*_balanced()[0], *_balanced()[1]], as_of=AS_OF).rows[0]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = run_score(
            [feature],
            run_id="t30-run",
            artifacts_dir=root,
            calibration=[_calibration("topic:rag")],
        )
        connection = sqlite3.connect(root / "t30-run" / "T30" / "trends.sqlite")
        try:
            stored = connection.execute(
                "SELECT score, features_json, confidence FROM trend_scores ORDER BY trend_id"
            ).fetchall()
        finally:
            connection.close()
        lines = (root / "t30-run" / "T30" / "trends.jsonl").read_text(encoding="utf-8")
        manifest = json.loads(
            (root / "t30-run" / "T30" / "manifest.json").read_text(encoding="utf-8")
        )
    assert result.status == "success"
    assert manifest["ticket"] == "T30"
    assert lines.count("\n") == 2
    assert len(stored) == 2
    for score, payload, confidence in stored:
        body = json.loads(payload)
        weights = body["weights"]
        components = body["components"]
        recomputed = sum(float(weights[name]) * float(components[name]) for name in SPEC_WEIGHTS)
        recomputed -= float(body["anomaly_penalty"])
        assert score == recomputed
        assert confidence == "high"
        assert math.isfinite(score)


def _formula(
    growth: float,
    diversity: float,
    quality: float,
    novelty: float,
    persistence: float,
    developer: float,
    penalty: float,
) -> float:
    parts = {
        "normalized_growth_7d": growth,
        "cross_source_diversity": diversity,
        "source_quality": quality,
        "novelty": novelty,
        "persistence_28d": persistence,
        "developer_activity": developer,
    }
    total = sum(SPEC_WEIGHTS[name] * min(1.0, max(0.0, value)) for name, value in parts.items())
    return total - min(PENALTY_CAP, max(0.0, penalty))


def _novel(previous: list[dict[str, object]], current: list[dict[str, object]]) -> int:
    seen = {str(item["canonical_id"]) for item in previous}
    return sum(1 for item in current if str(item["canonical_id"]) not in seen)


def _independent_dates(records: list[dict[str, object]], days: int) -> set[str]:
    start, end = window_bounds(AS_OF, days)
    ordered = sorted(records, key=lambda item: (str(item["published_at"]), str(item["record_id"])))
    seen: set[str] = set()
    dates: set[str] = set()
    for item in ordered:
        published = datetime.fromisoformat(str(item["published_at"]).replace("Z", "+00:00"))
        if not start <= published < end:
            continue
        key = str(item["canonical_id"])
        if key in seen:
            continue
        seen.add(key)
        dates.add(published.date().isoformat())
    return dates


def _balanced() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    source_for = {"academic": "openalex", "code": "github", "community": "hn"}
    previous = [
        _row(
            f"p{index}",
            f"2026-10-{day}T00:00:00Z",
            canonical_id=f"prev-{index}",
            family="academic",
            source_id="openalex",
        )
        for index, day in enumerate((15, 16, 17, 18), start=1)
    ]
    spec = (
        ("c1", "2026-10-22T00:00:00Z", "new-1", "academic", "A"),
        ("c2", "2026-10-23T00:00:00Z", "new-2", "code", "A"),
        ("c3", "2026-10-24T00:00:00Z", "new-3", "community", "B"),
        ("c4", "2026-10-25T00:00:00Z", "prev-1", "academic", "A"),
        ("c5", "2026-10-26T00:00:00Z", "new-5", "code", "A"),
        ("c6", "2026-10-27T00:00:00Z", "new-6", "academic", "A"),
    )
    current = [
        _row(
            record_id,
            published,
            canonical_id=canonical,
            family=family,
            source_id=source_for[family],
            tier=tier,
        )
        for record_id, published, canonical, family, tier in spec
    ]
    return previous, current


def _row(
    record_id: str,
    published_at: str,
    *,
    canonical_id: str | None = None,
    family: str = "academic",
    source_id: str = "openalex",
    tier: str = "A",
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "topic_id": "topic:rag",
        "published_at": published_at,
        "source_id": source_id,
        "source_family": family,
        "quality_tier": tier,
        "canonical_id": canonical_id or record_id,
    }


def _hand(topic_id: str, *, growth: float, families: list[str]) -> dict[str, object]:
    return {
        "topic_id": topic_id,
        "as_of": AS_OF_TEXT,
        "normalized_growth_7d": growth,
        "growth_unknown": False,
        "growth_current": 4,
        "growth_previous": 4,
        "cross_source_diversity": len(families) / 4,
        "source_families": families,
        "source_quality": 1.0,
        "quality_unknown": False,
        "novelty": 0.5,
        "novelty_unknown": False,
        "persistence_28d": 0.25,
        "persistence_numerator": 7,
        "developer_activity": 0.25,
        "developer_unknown": False,
        "anomaly_penalty": 0.0,
        "anomaly_reasons": [],
    }


def _calibration(
    topic_id: str,
    *,
    tier_a: tuple[str, ...] = ("ev1", "ev2"),
    qa_pass: bool = True,
    families_note: bool = False,
) -> dict[str, object]:
    del families_note
    return {
        "topic_id": topic_id,
        "tier_a_evidence_ids": list(tier_a),
        "supporting_evidence_ids": ["ev1", "ev2", "ev3"],
        "contrary_evidence_ids": ["ev4"],
        "citation_chain_complete": True,
        "contrary_search_executed": True,
        "qa_pass": qa_pass,
    }


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AssertionError("expected a number")
    return float(value)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object")
    return {str(key): item for key, item in value.items()}
