"""E20 review of an E10 pack built from the local evidence fixture."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from techpulse.evidence.contradictions import run_contradictions
from techpulse.evidence.retrieve import clusters_from_dedup
from techpulse.evidence.verify import run_evidence
from techpulse.normalize.dedup import deduplicate_records
from techpulse.trends.features import build_features
from techpulse.trends.score import score_trends

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "evidence" / "records.jsonl"
AS_OF = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
LIMIT = 100_000


def test_fixture_is_a_small_local_file() -> None:
    assert FIXTURE.is_file()
    assert FIXTURE.stat().st_size < LIMIT


def test_review_records_a_chain_for_every_candidate() -> None:
    records = [
        _object(json.loads(line))
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    clusters = clusters_from_dedup(deduplicate_records(records).rows)
    features = build_features(records, as_of=AS_OF)
    scored = score_trends(features.rows, calibration=[_calibration("topic:rag")])
    trends: list[dict[str, object]] = []
    for trend in scored.trends:
        row = dict(trend)
        if row.get("topic") == "topic:rag":
            row["contrary_search_executed"] = True
            row["qa_pass"] = True
        trends.append(row)
    missing = _missing(next(row for row in features.rows if row["topic_id"] == "topic:rag"))
    trends.append(missing)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pack = run_evidence(
            trends,
            records,
            features=list(features.rows),
            clusters=clusters,
            run_id="e20-e10",
            artifacts_dir=root,
        )
        review = run_contradictions(
            pack.document,
            records,
            clusters=clusters,
            run_id="e20-fixture",
            artifacts_dir=root,
        )
        directory = root / "e20-fixture" / "E20"
        stored = _object(
            json.loads((directory / "contradictions.json").read_text(encoding="utf-8"))
        )
        manifest = _object(json.loads((directory / "manifest.json").read_text(encoding="utf-8")))
        metrics = _object(json.loads((directory / "metrics.json").read_text(encoding="utf-8")))
        assert stored == review.document
        assert manifest["ticket"] == "E20"
        assert metrics["llm_calls"] == 0
        assert metrics["paid_api_calls"] == 0
        assert metrics["third_party_writes"] == 0
        assert metrics["candidates_with_chain"] == metrics["candidates"]
        assert metrics["high_confidence_chain_rate"] == 1.0
        supplied = {
            str(record["canonical_url"])
            for record in records
            if isinstance(record.get("canonical_url"), str)
        }
        candidates = {str(row["trend_id"]): row for row in _rows(stored.get("candidates"))}
        chains = _rows(stored.get("chains"))
        assert len(chains) >= len(candidates)
        for row in candidates.values():
            owned = [chain for chain in chains if chain.get("trend_id") == row["trend_id"]]
            assert owned
            assert all(_rows(chain.get("queries")) for chain in owned)
        seven = next(
            row
            for row in candidates.values()
            if row.get("topic") == "topic:rag" and row.get("window_days") == 7
        )
        assert seven["confidence"] == "high"
        assert seven["contrary_search_executed"] is True
        assert seven["contrary_search_incomplete"] is False
        assert seven["major_conflict"] is False
        absent = candidates[str(missing["trend_id"])]
        assert absent["confidence"] == "low"
        assert absent["status"] == "insufficient_evidence"
        for row in _rows(stored.get("contradictions")) + _rows(stored.get("reprints")):
            assert row["source_url"] in supplied
        for row in _rows(stored.get("contradictions")):
            match = next(
                record for record in records if record.get("canonical_url") == row["source_url"]
            )
            span = _object(row["citation_span"])
            source = str(match[str(span["field"])])
            start = int(str(span["start"]))
            end = int(str(span["end"]))
            assert source[start:end] == row["normalized_excerpt"]
        assert _as_int(metrics["same_origin_reprints"]) >= 1
        connection = sqlite3.connect(directory / "evidence.sqlite")
        try:
            confidence = connection.execute(
                "SELECT confidence FROM trend_scores WHERE trend_id = ?",
                (seven["trend_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert confidence == ("high",)


def _missing(feature: Mapping[str, object]) -> dict[str, object]:
    return {
        "trend_id": "trend:topic:quartz:7:2026-10-28",
        "topic_id": "topic:quartz",
        "topic": "topic:quartz",
        "window_days": 7,
        "score": 0.4,
        "confidence": "high",
        "experimental": False,
        "score_kind": "calibrated",
        "contrary_search_executed": True,
        "qa_pass": True,
        "last_verified_at": "2026-10-28T15:00:00Z",
        "growth_current": 4,
        "growth_previous": 4,
        "growth_unknown": False,
        "window_7d_start": feature["window_7d_start"],
        "window_7d_end": feature["window_7d_end"],
        "diversity_numerator": 3,
        "source_families": ["academic", "code", "community"],
        "quality_numerator": 2,
        "quality_denominator": 2,
        "quality_unknown": False,
    }


def _calibration(topic_id: str) -> dict[str, object]:
    return {
        "topic_id": topic_id,
        "tier_a_evidence_ids": ["ev1", "ev2"],
        "supporting_evidence_ids": ["ev1", "ev2"],
        "contrary_evidence_ids": [],
        "citation_chain_complete": True,
        "contrary_search_executed": True,
        "qa_pass": True,
    }


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return [_object(item) for item in value]


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object")
    return {str(key): item for key, item in value.items()}


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssertionError("expected an int")
    return value
