"""T20 windows and features. Expected rates are derived from fixture counts."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from techpulse.trends.features import (
    MIN_BASELINE,
    build_features,
    previous_window,
    run_features,
    window_bounds,
)

AS_OF = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
TIER_WEIGHT = {"A": 1.0, "B": 0.5, "C": 0.0}
PENALTY_UNIT = 0.10
PENALTY_CAP = 0.30


def test_windows_are_utc_natural_days() -> None:
    start, end = window_bounds(AS_OF, 7)
    previous_start, previous_end = previous_window(AS_OF, 7)
    start_28, end_28 = window_bounds(AS_OF, 28)
    assert start == datetime(2026, 10, 22, tzinfo=UTC)
    assert end == datetime(2026, 10, 29, tzinfo=UTC)
    assert previous_start == datetime(2026, 10, 15, tzinfo=UTC)
    assert previous_end == start
    assert start_28 == datetime(2026, 10, 1, tzinfo=UTC)
    assert end_28 == end
    shifted = datetime(2026, 10, 28, 16, 0, tzinfo=timezone(timedelta(hours=1)))
    assert window_bounds(shifted, 7) == (start, end)


def test_boundary_instants_follow_the_half_open_utc_window() -> None:
    records = [
        _row("in-start", "2026-10-22T00:00:00Z"),
        _row("in-end", "2026-10-28T23:59:59Z"),
        _row("prev-end", "2026-10-21T23:59:59Z"),
        _row("prev-start", "2026-10-15T00:00:00Z"),
        _row("before-prev", "2026-10-14T23:59:59Z"),
        _row("after", "2026-10-29T00:00:00Z"),
        _row("day-28", "2026-10-01T00:00:00Z"),
        _row("before-28", "2026-09-30T23:59:59Z"),
        _row("offset-in", "2026-10-29T00:30:00+01:00"),
        _row("offset-out", "2026-10-29T01:00:00+01:00"),
    ]
    row = build_features(records, as_of=AS_OF).rows[0]
    assert set(_strings(row["record_ids_7d"])) == {"in-start", "in-end", "offset-in"}
    assert set(_strings(row["record_ids_7d_previous"])) == {"prev-end", "prev-start"}
    assert "before-prev" not in _strings(row["record_ids_7d_previous"])
    assert "after" not in _strings(row["record_ids_28d"])
    assert "offset-out" not in _strings(row["record_ids_28d"])
    assert "day-28" in _strings(row["record_ids_28d"])
    assert "before-28" not in _strings(row["record_ids_28d"])


def test_components_match_counts_on_the_fixture() -> None:
    previous, current = _balanced_fixture()
    row = build_features([*previous, *current], as_of=AS_OF).rows[0]
    families = sorted({str(item["source_family"]) for item in current})
    quality_numerator = sum(TIER_WEIGHT[str(item["quality_tier"])] for item in current)
    previous_keys = {str(item["canonical_id"]) for item in previous}
    novel = sum(1 for item in current if str(item["canonical_id"]) not in previous_keys)
    days = _independent_dates([*previous, *current], 28)
    code = sum(1 for item in current if item["source_family"] == "code")

    assert row["growth_current"] == len(current)
    assert row["growth_previous"] == len(previous)
    assert row["growth_previous"] >= MIN_BASELINE
    assert row["growth_numerator"] == len(current) - len(previous)
    assert row["growth_denominator"] == len(previous)
    assert row["normalized_growth_7d"] == (len(current) - len(previous)) / len(previous)
    assert row["growth_unknown"] is False
    assert row["diversity_numerator"] == len(families)
    assert row["diversity_denominator"] == 4
    assert row["cross_source_diversity"] == len(families) / 4
    assert row["source_families"] == families
    assert row["quality_numerator"] == quality_numerator
    assert row["quality_denominator"] == len(current)
    assert row["source_quality"] == quality_numerator / len(current)
    assert row["novelty_numerator"] == novel
    assert row["novelty"] == novel / len(current)
    assert row["persistence_numerator"] == len(days)
    assert row["persistence_denominator"] == 28
    assert row["persistence_28d"] == len(days) / 28
    assert row["developer_numerator"] == code
    assert row["developer_denominator"] == len(current)
    assert row["developer_activity"] == code / len(current)
    assert row["anomaly_penalty"] == 0.0
    assert row["anomaly_reasons"] == []


def test_stored_ids_recount_to_the_same_numbers() -> None:
    previous, current = _balanced_fixture()
    records = [*previous, *current]
    row = build_features(records, as_of=AS_OF).rows[0]
    by_id = {str(item["record_id"]): item for item in records}
    current_ids = _strings(row["independent_ids_7d"])
    previous_ids = _strings(row["independent_ids_7d_previous"])
    assert current_ids == {str(item["record_id"]) for item in current}
    assert previous_ids == {str(item["record_id"]) for item in previous}
    recounted_current = len(current_ids)
    recounted_previous = len(previous_ids)
    assert (
        row["normalized_growth_7d"] == (recounted_current - recounted_previous) / recounted_previous
    )
    dates = {
        str(by_id[record_id]["published_at"])[:10]
        for record_id in _strings(row["independent_ids_28d"])
    }
    assert row["persistence_28d"] == len(dates) / 28


def test_small_baseline_does_not_emit_a_growth_rate() -> None:
    previous = [
        _row("p1", "2026-10-15T00:00:00Z"),
        _row("p2", "2026-10-16T00:00:00Z"),
    ]
    current = [
        _row("c1", "2026-10-22T00:00:00Z", source_id="openalex"),
        _row("c2", "2026-10-23T00:00:00Z", source_id="github"),
        _row("c3", "2026-10-24T00:00:00Z", source_id="hn"),
        _row("c4", "2026-10-25T00:00:00Z", source_id="rss"),
    ]
    assert len(previous) < MIN_BASELINE
    row = build_features([*previous, *current], as_of=AS_OF).rows[0]
    assert row["normalized_growth_7d"] is None
    assert row["growth_unknown"] is True
    assert row["small_sample"] is True
    assert row["missing_baseline"] is False
    assert row["growth_denominator"] == len(previous)
    assert "growth_percent" not in row
    reason = next(item for item in _reasons(row) if item["code"] == "small_base")
    assert reason["value"] == len(previous)
    assert reason["current_independent_7d"] == len(current)
    assert row["anomaly_penalty"] == PENALTY_UNIT


def test_missing_baseline_is_unknown_not_infinite_growth() -> None:
    current = [_row(f"c{index}", f"2026-10-2{index}T00:00:00Z") for index in range(2, 5)]
    row = build_features(current, as_of=AS_OF).rows[0]
    assert row["growth_previous"] == 0
    assert row["missing_baseline"] is True
    assert row["normalized_growth_7d"] is None
    assert row["growth_unknown"] is True
    reason = next(item for item in _reasons(row) if item["code"] == "small_base")
    assert reason["metric"] == "previous_independent_7d"
    assert reason["value"] == 0


def test_penalty_uses_raw_counts_and_caps_at_the_limit() -> None:
    current = [
        _row("r1", "2026-10-22T00:00:00Z", canonical_id="k1", url="https://news.example.com/a"),
        _row("r2", "2026-10-22T01:00:00Z", canonical_id="k1", url="https://blog.example.com/a"),
        _row("r3", "2026-10-23T00:00:00Z", canonical_id="k2", url="https://news.example.com/b"),
        _row("r4", "2026-10-24T00:00:00Z", canonical_id="k3", url="https://blog.example.com/b"),
        _row("r5", "2026-10-25T00:00:00Z", canonical_id="k4", url="https://other.example.com/c"),
    ]
    previous = [_row("p1", "2026-10-20T00:00:00Z", canonical_id="old")]
    for item in [*current, *previous]:
        item["source_id"] = "hn"
    raw_count = len(current)
    independent_count = len({str(item["canonical_id"]) for item in current})
    reprint = PENALTY_UNIT * (raw_count - independent_count) / raw_count
    expected = min(PENALTY_CAP, reprint + PENALTY_UNIT + PENALTY_UNIT + PENALTY_UNIT)
    row = build_features([*previous, *current], as_of=AS_OF).rows[0]
    reasons = {str(item["code"]): item for item in _reasons(row)}
    assert set(reasons) == {"reprint", "same_domain", "bot", "small_base"}
    assert reasons["reprint"]["raw_count"] == raw_count
    assert reasons["reprint"]["independent_count"] == independent_count
    assert reasons["reprint"]["value"] == (raw_count - independent_count) / raw_count
    assert reasons["same_domain"]["domain"] == "example.com"
    assert int(str(reasons["same_domain"]["host_count"])) >= 2
    assert reasons["bot"]["raw_count"] == raw_count
    assert reasons["bot"]["value"] == 1.0
    assert reasons["small_base"]["value"] == len(previous)
    assert row["anomaly_penalty"] == expected


def test_one_host_is_not_treated_as_many_sites() -> None:
    previous = [
        _row(f"p{index}", f"2026-10-1{index}T00:00:00Z", source_id="openalex")
        for index in range(5, 8)
    ]
    current = [
        _row(
            "c1",
            "2026-10-22T00:00:00Z",
            canonical_id="k1",
            url="https://example.com/a",
            source_id="openalex",
        ),
        _row(
            "c2",
            "2026-10-23T00:00:00Z",
            canonical_id="k2",
            url="https://example.com/b",
            source_id="github",
        ),
    ]
    assert len(previous) >= MIN_BASELINE
    row = build_features([*previous, *current], as_of=AS_OF).rows[0]
    assert row["anomaly_reasons"] == []
    assert row["anomaly_penalty"] == 0.0


def test_source_family_aliases() -> None:
    records = [
        _row("a", "2026-10-22T00:00:00Z", source_family="academic_papers"),
        _row("b", "2026-10-23T00:00:00Z", source_id="github", source_family=None),
        _row("c", "2026-10-24T00:00:00Z", source_family="community_discussions", source_id="hn"),
    ]
    row = build_features(records, as_of=AS_OF).rows[0]
    assert row["source_families"] == ["academic", "code", "community"]


def test_naive_timestamps_are_rejected() -> None:
    result = build_features(
        [_row("naive", "2026-10-22T00:00:00"), _row("ok", "2026-10-22T00:00:00Z")],
        as_of=AS_OF,
    )
    assert result.rejected_time == 1
    assert result.rows[0]["independent_ids_7d"] == ["ok"]


def test_run_features_writes_sqlite_and_jsonl() -> None:
    previous, current = _balanced_fixture()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = run_features(
            [*previous, *current],
            run_id="t20-run",
            artifacts_dir=root,
            as_of=AS_OF,
        )
        manifest = json.loads(
            (root / "t20-run" / "T20" / "manifest.json").read_text(encoding="utf-8")
        )
        metrics = json.loads(
            (root / "t20-run" / "T20" / "metrics.json").read_text(encoding="utf-8")
        )
        stored = (root / "t20-run" / "T20" / "features.jsonl").read_text(encoding="utf-8")
        connection = sqlite3.connect(root / "t20-run" / "T20" / "features.sqlite")
        try:
            payload = connection.execute("SELECT payload FROM trend_feature_snapshots").fetchone()
        finally:
            connection.close()
        assert result.rows[0]["topic_id"] == "topic:rag"
        assert manifest["ticket"] == "T20"
        assert manifest["min_baseline"] == MIN_BASELINE
        assert "features.jsonl" in manifest["note"]
        assert metrics["status"] == "success"
        assert metrics["unknown_growth"] == 0
        assert metrics["third_party_writes"] == 0
        assert stored.count("\n") == 1
        assert payload is not None
        assert json.loads(payload[0])["topic_id"] == "topic:rag"


def _balanced_fixture() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    source_for = {"academic": "openalex", "code": "github", "community": "hn"}
    previous = [
        _row(
            f"p{index}",
            f"2026-10-{day}T00:00:00Z",
            canonical_id=f"prev-{index}",
            source_family="academic",
            source_id="openalex",
            tier="A",
        )
        for index, day in enumerate((15, 16, 17, 18), start=1)
    ]
    current_spec = (
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
            source_family=family,
            source_id=source_for[family],
            tier=tier,
        )
        for record_id, published, canonical, family, tier in current_spec
    ]
    return previous, current


def _row(
    record_id: str,
    published_at: str,
    *,
    canonical_id: str | None = None,
    source_family: str | None = "academic",
    source_id: str = "openalex",
    tier: str = "A",
    url: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "record_id": record_id,
        "topic_id": "topic:rag",
        "published_at": published_at,
        "source_id": source_id,
        "quality_tier": tier,
        "canonical_id": canonical_id or record_id,
    }
    if source_family is not None:
        row["source_family"] = source_family
    if url is not None:
        row["canonical_url"] = url
    return row


def _independent_dates(records: list[dict[str, object]], days: int) -> set[str]:
    start, end = window_bounds(AS_OF, days)
    ordered = sorted(
        records,
        key=lambda item: (str(item["published_at"]), str(item["record_id"])),
    )
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


def _strings(value: object) -> set[str]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return {str(item) for item in value}


def _reasons(row: dict[str, object]) -> list[dict[str, object]]:
    value = row["anomaly_reasons"]
    if not isinstance(value, list):
        raise AssertionError("expected reasons")
    reasons: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise AssertionError("expected a reason object")
        reasons.append({str(key): reason for key, reason in item.items()})
    return reasons
