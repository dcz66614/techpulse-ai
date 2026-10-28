"""E10 pack on a small local fixture. Retrieval stays inside the process."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

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


def test_pack_uses_d30_clusters_and_t30_gates() -> None:
    records = [
        _object(json.loads(line))
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    deduped = deduplicate_records(records)
    clusters = clusters_from_dedup(deduped.rows)
    assert clusters["openalex:W-rag-a1"] == clusters["rss:item-dup"]
    assert clusters["openalex:W-rag-a1"].startswith("doi:")
    features = build_features(records, as_of=AS_OF)
    feature = next(row for row in features.rows if row["topic_id"] == "topic:rag")
    scored = score_trends(features.rows, calibration=[_calibration("topic:rag")])
    assert scored.status == "success"
    trends: list[dict[str, object]] = []
    for trend in scored.trends:
        row = dict(trend)
        if row.get("topic") == "topic:rag":
            row["contrary_search_executed"] = True
            row["qa_pass"] = True
        trends.append(row)
    missing = _missing(feature)
    trends.append(missing)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pack = run_evidence(
            trends,
            records,
            features=list(features.rows),
            clusters=clusters,
            run_id="e10-fixture",
            artifacts_dir=root,
        )
        directory = root / "e10-fixture" / "E10"
        stored = _object(json.loads((directory / "evidence_pack.json").read_text(encoding="utf-8")))
        manifest = _object(json.loads((directory / "manifest.json").read_text(encoding="utf-8")))
        metrics = _object(json.loads((directory / "metrics.json").read_text(encoding="utf-8")))
        assert stored == pack.document
        assert manifest["status"] == "insufficient_evidence"
        assert metrics["llm_calls"] == 0
        assert metrics["paid_api_calls"] == 0
        assert metrics["third_party_writes"] == 0
        assert metrics["url_chain_complete_rate"] == 1.0
        assert _as_int(metrics["max_claims_per_trend"]) <= 3
        rows = {str(row["trend_id"]): row for row in _rows(stored.get("trends"))}
        seven = next(
            row
            for row in rows.values()
            if row.get("topic") == "topic:rag" and row.get("window_days") == 7
        )
        assert seven["confidence"] == "high"
        assert seven["status"] == "supported"
        assert _list_len(seven["source_families"]) >= 3
        assert _as_int(seven["tier_a_count"]) >= 2
        assert _text(seven["trend_id"]) in _strings(stored["high_confidence_trend_ids"])
        absent = rows[str(missing["trend_id"])]
        assert absent["status"] == "insufficient_evidence"
        assert absent["confidence"] == "low"
        assert absent["removed_from_high_confidence"] is True
        assert _text(absent["trend_id"]) not in _strings(stored["high_confidence_trend_ids"])
        growth = next(
            claim
            for claim in _rows(stored.get("claims"))
            if claim.get("trend_id") == seven["trend_id"] and claim.get("template") == "growth_7d"
        )
        assert str(feature["growth_previous"]) in str(growth["text"])
        assert str(feature["growth_current"]) in str(growth["text"])
        evidence = {str(item["evidence_id"]): item for item in _rows(stored.get("evidence"))}
        urls = {str(item["source_url"]) for item in evidence.values()}
        by_record = {
            str(record["source_record_id"]): record
            for record in records
            if isinstance(record.get("source_record_id"), str)
        }
        assert by_record["W-rag-a1"]["canonical_url"] in urls
        assert by_record["item-dup"]["canonical_url"] not in urls
        assert by_record["insecure"]["canonical_url"] not in urls
        assert by_record["blocked"]["canonical_url"] not in urls
        supplied = {
            str(record["canonical_url"])
            for record in records
            if isinstance(record.get("canonical_url"), str)
        }
        assert urls <= supplied
        for item in evidence.values():
            match = next(
                record for record in records if record.get("canonical_url") == item["source_url"]
            )
            assert item["sha256"] == match["content_sha256"]
            assert item["license_status"] == match["license_status"]
            assert item["source_published_at"] == match["published_at"]
            span = _object(item["citation_span"])
            field = str(span["field"])
            source = str(match[field])
            start = int(str(span["start"]))
            end = int(str(span["end"]))
            assert source[start:end] == item["normalized_excerpt"]
            assert item["quote_length"] == len(str(item["normalized_excerpt"]))
        growth_urls = {
            str(evidence[str(evidence_id)]["source_url"])
            for evidence_id in _strings(growth.get("supporting_evidence_ids"))
        }
        assert by_record["W-prev-1"]["canonical_url"] not in growth_urls
        connection = sqlite3.connect(directory / "evidence.sqlite")
        try:
            confidence = connection.execute(
                "SELECT confidence FROM trend_scores WHERE trend_id = ?",
                (seven["trend_id"],),
            ).fetchone()
            removed = connection.execute(
                "SELECT confidence FROM trend_scores WHERE trend_id = ?",
                (missing["trend_id"],),
            ).fetchone()
            relations = connection.execute(
                "SELECT relation FROM trend_evidence WHERE trend_id = ?",
                (seven["trend_id"],),
            ).fetchall()
        finally:
            connection.close()
        assert confidence == ("high",)
        assert removed == ("low",)
        assert relations
        assert all(row[0] == "support" for row in relations)


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


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return [item for item in value if isinstance(item, str)]


def _list_len(value: object) -> int:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return len(value)


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssertionError("expected an int")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise AssertionError("expected a string")
    return value


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object")
    return {str(key): item for key, item in value.items()}
