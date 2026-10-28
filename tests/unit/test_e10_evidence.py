"""E10 claim drafts and evidence gates. Counts come from the fixture fields."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from techpulse.contracts import EvidenceItem
from techpulse.evidence import retrieve as retrieve_module
from techpulse.evidence import verify as verify_module
from techpulse.evidence.retrieve import (
    MAX_CLAIMS,
    MAX_EVIDENCE,
    draft_claims,
    retrieve_support,
)
from techpulse.evidence.verify import build_evidence_pack, run_evidence
from techpulse.index.embed import Embedder

START = "2026-10-22T00:00:00Z"
END = "2026-10-29T00:00:00Z"
AS_OF = "2026-10-28T15:00:00Z"
COLLECTED = "2026-10-27T00:00:00Z"
SUPPORT_EXCERPT = "RAG records changed from 4 to 5."


def test_claims_quote_stored_counts_and_stop_at_three() -> None:
    feature = _feature()
    seven = draft_claims(_trend(7), feature)
    twenty_eight = draft_claims(_trend(28), feature)
    assert 1 <= len(seven) <= MAX_CLAIMS
    assert [claim.template for claim in seven] == ["growth_7d", "diversity_7d", "quality_7d"]
    assert "changed from 4 to 5" in seven[0].text
    assert "source-family count 3 (academic, code, community)" in seven[1].text
    assert "2.5/3" in seven[2].text
    assert START in seven[0].text and END in seven[0].text
    assert [claim.template for claim in twenty_eight] == [
        "persistence_28d",
        "diversity_7d",
        "quality_7d",
    ]
    assert "independent records on 9 of 28" in twenty_eight[0].text
    assert "changed from" not in twenty_eight[0].text
    again = draft_claims(_trend(7), feature)
    assert [claim.text for claim in seven] == [claim.text for claim in again]


def test_unknown_growth_does_not_invent_a_rate() -> None:
    feature = _feature()
    feature["growth_unknown"] = True
    feature["growth_previous"] = 2
    feature["growth_current"] = 9
    claims = draft_claims(_trend(7), feature)
    growth = next(claim for claim in claims if claim.template == "growth_7d_unknown")
    assert "previous window has 2 independent records" in growth.text
    assert "changed from" not in growth.text
    assert "%" not in growth.text
    assert "causes" not in growth.text


def test_grade_prefers_tier_then_newer_date() -> None:
    older = _doc("openalex", "older", "academic", "A", "2026-10-22T00:00:00Z")
    newer = _doc("hn", "newer", "community", "C", "2026-10-27T00:00:00Z")
    same_tier_old = _doc("arxiv", "early", "academic", "A", "2026-10-22T00:00:00Z")
    same_tier_new = _doc("arxiv", "late", "academic", "A", "2026-10-26T00:00:00Z")
    tier_order = retrieve_support("rag", [newer, older], window_start=START, window_end=END)
    assert [item["quality_tier"] for item in tier_order.evidence] == ["A", "C"]
    date_order = retrieve_support(
        "rag",
        [same_tier_old, same_tier_new],
        window_start=START,
        window_end=END,
    )
    assert [item["source_record_id"] for item in date_order.evidence] == ["late", "early"]


def test_reprint_cluster_keeps_one_and_copies_upstream_fields() -> None:
    excerpt = "RAG survey excerpt."
    original = _doc("openalex", "paper", "academic", "A", "2026-10-22T00:00:00Z")
    original["abstract_or_excerpt"] = excerpt
    original["citation_span"] = {"field": "abstract_or_excerpt", "start": 0, "end": 3}
    original["canonical_id"] = "openalex:paper"
    copy = _doc("rss", "copy", "news", "C", "2026-10-22T00:00:00Z")
    copy["canonical_id"] = "rss:copy"
    outside = _doc("github", "old", "code", "A", "2026-10-15T00:00:00Z")
    insecure = _doc("github", "insecure", "code", "A", "2026-10-24T00:00:00Z")
    insecure["canonical_url"] = "http://example.org/rag-insecure"
    blocked = _doc("hn", "blocked", "community", "A", "2026-10-24T00:00:00Z")
    blocked["license_status"] = "blocked"
    missing_hash = _doc("openalex", "nohash", "academic", "A", "2026-10-24T00:00:00Z")
    del missing_hash["content_sha256"]
    result = retrieve_support(
        "rag",
        [copy, original, outside, insecure, blocked, missing_hash],
        window_start=START,
        window_end=END,
        clusters={"openalex:paper": "cluster-1", "rss:copy": "cluster-1"},
    )
    assert result.dropped_reprints == 1
    assert result.dropped_out_of_window == 1
    assert [item["source_record_id"] for item in result.evidence] == ["paper"]
    kept = result.evidence[0]
    assert kept["source_url"] == original["canonical_url"]
    assert kept["sha256"] == original["content_sha256"]
    assert kept["source_published_at"] == original["published_at"]
    assert kept["license_status"] == original["license_status"]
    assert kept["normalized_excerpt"] == "RAG"
    assert kept["quote_length"] == 3
    span = _object(kept["citation_span"])
    assert excerpt[int(str(span["start"])) : int(str(span["end"]))] == "RAG"
    EvidenceItem.model_validate({key: kept[key] for key in EvidenceItem.model_fields})
    urls = {item["source_url"] for item in result.evidence}
    assert insecure["canonical_url"] not in urls
    assert blocked["canonical_url"] not in urls
    assert outside["canonical_url"] not in urls
    assert all(
        url in {original["canonical_url"], copy["canonical_url"]}
        for url in urls
        if isinstance(url, str)
    )


def test_unresolved_rows_are_not_collapsed() -> None:
    left = _doc("openalex", "left", "academic", "A", "2026-10-22T00:00:00Z")
    right = _doc("rss", "right", "news", "A", "2026-10-23T00:00:00Z")
    left["canonical_id"] = "same"
    right["canonical_id"] = "same"
    left["unresolved"] = True
    right["unresolved"] = True
    result = retrieve_support("rag", [left, right], window_start=START, window_end=END)
    assert len(result.evidence) == 2
    assert result.dropped_reprints == 0


def test_family_alias_and_source_default() -> None:
    alias = _doc("openalex", "alias", "academic_papers", "B", "2026-10-22T00:00:00Z")
    implied = _doc("github", "repo", "code", "A", "2026-10-22T00:00:00Z")
    del implied["source_family"]
    result = retrieve_support("rag", [alias, implied], window_start=START, window_end=END)
    families = {item["source_record_id"]: item["source_family"] for item in result.evidence}
    assert families == {"alias": "academic", "repo": "code"}


def test_at_most_ten_evidence_items() -> None:
    records = [
        _doc("openalex", f"{index:02d}", "academic", "A", "2026-10-22T00:00:00Z")
        for index in range(1, 13)
    ]
    result = retrieve_support("rag", records, window_start=START, window_end=END)
    assert len(result.evidence) == MAX_EVIDENCE
    assert [item["source_record_id"] for item in result.evidence] == [
        f"{index:02d}" for index in range(1, 11)
    ]


def test_high_confidence_needs_the_t30_gates() -> None:
    records = _support_records()
    feature = _feature()
    high = build_evidence_pack([_trend(7)], records, features=[feature])
    trend = _trends(high.document)[0]
    assert trend["confidence"] == "high"
    assert trend["status"] == "supported"
    assert high.document["high_confidence_trend_ids"] == [trend["trend_id"]]
    assert trend["removed_from_high_confidence"] is False
    assert _number_len(trend["source_families"]) >= 3
    assert _as_int(trend["tier_a_count"]) >= 2
    assert trend["citation_chain_complete"] is True
    assert high.metrics["url_chain_complete_rate"] == 1.0
    assert high.metrics["llm_calls"] == 0
    assert high.metrics["paid_api_calls"] == 0
    weak = build_evidence_pack(
        [_trend(7)],
        [
            _doc("openalex", "a", "academic", "A", "2026-10-22T00:00:00Z", SUPPORT_EXCERPT),
            _doc("github", "b", "code", "B", "2026-10-23T00:00:00Z", SUPPORT_EXCERPT),
            _doc("hn", "c", "community", "B", "2026-10-24T00:00:00Z", SUPPORT_EXCERPT),
        ],
        features=[feature],
    )
    assert _trends(weak.document)[0]["confidence"] == "medium"
    assert weak.document["high_confidence_trend_ids"] == []
    assert _trends(weak.document)[0]["removed_from_high_confidence"] is True
    single = build_evidence_pack(
        [_trend(7)],
        [
            _doc("openalex", f"s{index}", "academic", "A", "2026-10-22T00:00:00Z", SUPPORT_EXCERPT)
            for index in range(3)
        ],
        features=[feature],
    )
    assert _trends(single.document)[0]["confidence"] == "low"
    no_contrary = build_evidence_pack(
        [_trend(7, contrary_search_executed=False)],
        records,
        features=[feature],
    )
    assert _trends(no_contrary.document)[0]["confidence"] == "medium"
    no_qa = build_evidence_pack([_trend(7, qa_pass=False)], records, features=[feature])
    assert _trends(no_qa.document)[0]["confidence"] == "medium"
    experimental = build_evidence_pack(
        [_trend(7, experimental=True, score_kind="experimental")],
        records,
        features=[feature],
    )
    assert _trends(experimental.document)[0]["confidence"] == "low"


def test_degraded_retrieval_cannot_stay_high() -> None:
    pack = build_evidence_pack(
        [_trend(7)],
        _support_records(),
        features=[_feature()],
        vector_store_available=False,
    )
    trend = _trends(pack.document)[0]
    assert trend["retrieval_degraded"] is True
    assert trend["confidence"] == "medium"
    assert trend["supporting_evidence_ids"]
    assert pack.document["high_confidence_trend_ids"] == []


def test_insufficient_evidence_leaves_the_high_list() -> None:
    bread = _doc("rss", "bread", "news", "A", "2026-10-22T00:00:00Z")
    bread["title"] = "Sourdough method"
    bread["abstract_or_excerpt"] = "Flour and water"
    bread["citation_span"] = {
        "field": "abstract_or_excerpt",
        "start": 0,
        "end": len("Flour and water"),
    }
    feature = _feature()
    missing = _trend(7)
    missing["trend_id"] = "trend:topic:missing:7:2026-10-28"
    missing["topic"] = "topic:missing"
    missing["topic_id"] = "topic:missing"
    for key in (
        "growth_current",
        "growth_previous",
        "growth_unknown",
        "window_7d_start",
        "window_7d_end",
        "diversity_numerator",
        "source_families",
        "quality_numerator",
        "quality_denominator",
        "quality_unknown",
    ):
        missing[key] = feature[key]
    kept = _trend(7)
    pack = build_evidence_pack(
        [kept, missing],
        [*_support_records(), bread],
        features=[feature],
    )
    rows = {str(row["trend_id"]): row for row in _trends(pack.document)}
    assert rows[str(missing["trend_id"])]["status"] == "insufficient_evidence"
    assert rows[str(missing["trend_id"])]["confidence"] == "low"
    assert rows[str(missing["trend_id"])]["removed_from_high_confidence"] is True
    assert rows[str(kept["trend_id"])]["confidence"] == "high"
    assert pack.status == "insufficient_evidence"
    assert pack.document["high_confidence_trend_ids"] == [kept["trend_id"]]
    assert _text(missing["trend_id"]) in _strings(pack.document["removed_from_high_confidence"])
    urls = {item["source_url"] for item in _evidence(pack.document)}
    assert bread["canonical_url"] not in urls
    supplied = {record["canonical_url"] for record in _support_records()}
    assert urls <= supplied


def test_pack_is_deterministic_and_writes_sqlite() -> None:
    trends = [_trend(7)]
    records = _support_records()
    features = [_feature()]
    first = build_evidence_pack(trends, records, features=features, run_id="e10-unit")
    second = build_evidence_pack(trends, records, features=features, run_id="e10-unit")
    assert first.document == second.document
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        written = run_evidence(
            trends,
            records,
            features=features,
            run_id="e10-unit",
            artifacts_dir=root,
        )
        directory = root / "e10-unit" / "E10"
        body = (directory / "evidence_pack.json").read_bytes()
        manifest = _object(json.loads((directory / "manifest.json").read_text(encoding="utf-8")))
        metrics = _object(json.loads((directory / "metrics.json").read_text(encoding="utf-8")))
        assert manifest["status"] == "success"
        assert manifest["evidence_pack_sha256"] == hashlib.sha256(body).hexdigest()
        assert metrics["third_party_writes"] == 0
        assert _as_int(metrics["max_claims_per_trend"]) <= MAX_CLAIMS
        assert written.status == "success"
        connection = sqlite3.connect(directory / "evidence.sqlite")
        try:
            confidence = connection.execute(
                "SELECT confidence FROM trend_scores WHERE trend_id = ?",
                (trends[0]["trend_id"],),
            ).fetchone()
            links = connection.execute(
                "SELECT relation FROM trend_evidence WHERE trend_id = ?",
                (trends[0]["trend_id"],),
            ).fetchall()
            urls = connection.execute(
                "SELECT canonical_url, content_sha256 FROM raw_records"
            ).fetchall()
        finally:
            connection.close()
        assert confidence == ("high",)
        assert links
        assert all(link[0] == "support" for link in links)
        assert all(str(url).startswith("https://") for url, _digest in urls)
        known = {str(record["content_sha256"]) for record in records}
        assert {str(digest) for _url, digest in urls} <= known


def test_network_embedder_is_refused() -> None:
    class _Remote:
        calls_network = True
        model_id = "remote"
        model_version = "1"
        dimension = 3

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            raise AssertionError(texts)

    remote: Embedder = _Remote()
    with pytest.raises(ValueError, match="network"):
        retrieve_support("rag", [_doc("openalex", "a", "academic", "A", START)], embedder=remote)


def test_modules_do_not_call_a_model_api() -> None:
    source = inspect.getsource(retrieve_module) + inspect.getsource(verify_module)
    folded = source.lower()
    assert "openai" not in folded
    assert "httpx" not in folded
    assert "embedder_from_env" not in folded


def test_generic_rag_evidence_is_unrelated_and_never_high() -> None:
    generic = [
        _doc("openalex", "paper", "academic", "A", "2026-10-22T00:00:00Z"),
        _doc("arxiv", "note", "academic", "A", "2026-10-23T00:00:00Z"),
        _doc("github", "repo", "code", "A", "2026-10-24T00:00:00Z"),
        _doc("hn", "thread", "community", "B", "2026-10-25T00:00:00Z"),
    ]
    pack = build_evidence_pack([_trend(7)], generic, features=[_feature()])
    trend = _trends(pack.document)[0]
    assert trend["status"] == "insufficient_evidence"
    assert trend["confidence"] == "low"
    assert trend["supporting_evidence_ids"] == []
    assert trend["removed_from_high_confidence"] is True
    assert pack.document["high_confidence_trend_ids"] == []
    assert pack.document["evidence"] == []
    assert _as_int(pack.metrics["unrelated_dropped"]) >= 1


def test_missing_citation_span_is_excluded_from_support_and_chain() -> None:
    valid = _doc("openalex", "paper", "academic", "A", "2026-10-22T00:00:00Z", SUPPORT_EXCERPT)
    missing = _doc(
        "github",
        "repo",
        "code",
        "A",
        "2026-10-24T00:00:00Z",
        SUPPORT_EXCERPT,
        with_span=False,
    )
    invalid = _doc(
        "hn",
        "thread",
        "community",
        "B",
        "2026-10-25T00:00:00Z",
        SUPPORT_EXCERPT,
        with_span=False,
    )
    invalid["citation_span"] = {
        "field": "abstract_or_excerpt",
        "start": 0,
        "end": len(SUPPORT_EXCERPT) + 5,
    }
    pack = build_evidence_pack(
        [_trend(7)],
        [valid, missing, invalid],
        features=[_feature()],
    )
    trend = _trends(pack.document)[0]
    evidence = {str(item["source_record_id"]): item for item in _evidence(pack.document)}
    paper = evidence["paper"]
    assert "citation_span" in paper
    assert paper.get("citation_span_missing") is not True
    support_ids = _strings(trend["supporting_evidence_ids"])
    assert _text(paper["evidence_id"]) in support_ids
    for sid in ("repo", "thread"):
        item = evidence[sid]
        assert item["citation_span_missing"] is True
        assert "citation_span" not in item
        assert _text(item["evidence_id"]) not in support_ids
    assert pack.metrics["url_chain_complete_rate"] == 1.0
    assert _as_int(pack.metrics["citation_span_missing"]) == 2
    only = build_evidence_pack([_trend(7)], [missing], features=[_feature()])
    only_trend = _trends(only.document)[0]
    assert only_trend["status"] == "insufficient_evidence"
    assert only_trend["confidence"] == "low"
    assert only_trend["supporting_evidence_ids"] == []
    assert only.document["high_confidence_trend_ids"] == []
    assert only.metrics["url_chain_complete_rate"] == 1.0


def test_empty_input_is_insufficient() -> None:
    pack = build_evidence_pack([], [])
    assert pack.status == "insufficient_evidence"
    assert pack.document["high_confidence_trend_ids"] == []
    assert pack.document["evidence"] == []


def _feature() -> dict[str, object]:
    return {
        "topic_id": "topic:rag",
        "as_of": AS_OF,
        "growth_current": 5,
        "growth_previous": 4,
        "growth_unknown": False,
        "window_7d_start": START,
        "window_7d_end": END,
        "diversity_numerator": 3,
        "source_families": ["academic", "code", "community"],
        "quality_numerator": 2.5,
        "quality_denominator": 3,
        "quality_unknown": False,
        "persistence_numerator": 9,
        "persistence_denominator": 28,
        "window_28d_start": "2026-10-01T00:00:00Z",
        "window_28d_end": END,
        "developer_numerator": 1,
        "developer_denominator": 5,
        "developer_unknown": False,
        "novelty_numerator": 4,
        "novelty_denominator": 5,
        "novelty_unknown": False,
    }


def _trend(window: int, **overrides: object) -> dict[str, object]:
    trend: dict[str, object] = {
        "trend_id": f"trend:topic:rag:{window}:2026-10-28",
        "topic_id": "topic:rag",
        "topic": "topic:rag",
        "window_days": window,
        "score": 0.5,
        "confidence": "high",
        "experimental": False,
        "score_kind": "calibrated",
        "contrary_search_executed": True,
        "qa_pass": True,
        "last_verified_at": AS_OF,
    }
    trend.update(overrides)
    return trend


def _support_records() -> list[dict[str, object]]:
    return [
        _doc("openalex", "paper", "academic", "A", "2026-10-22T00:00:00Z", SUPPORT_EXCERPT),
        _doc("arxiv", "note", "academic", "A", "2026-10-23T00:00:00Z", SUPPORT_EXCERPT),
        _doc("github", "repo", "code", "A", "2026-10-24T00:00:00Z", SUPPORT_EXCERPT),
        _doc("hn", "thread", "community", "B", "2026-10-25T00:00:00Z", SUPPORT_EXCERPT),
    ]


def _doc(
    source: str,
    sid: str,
    family: str,
    tier: str,
    published: str,
    excerpt: str | None = None,
    *,
    with_span: bool = True,
) -> dict[str, object]:
    body = excerpt if excerpt is not None else f"RAG evidence for {sid}"
    row: dict[str, object] = {
        "source_id": source,
        "source_record_id": sid,
        "source_family": family,
        "quality_tier": tier,
        "canonical_id": f"{source}:{sid}",
        "canonical_url": f"https://example.org/{source}/{sid}",
        "title": f"RAG note {sid}",
        "abstract_or_excerpt": body,
        "published_at": published,
        "collected_at": COLLECTED,
        "license_status": "metadata_only",
        "content_sha256": hashlib.sha256(f"{source}:{sid}".encode()).hexdigest(),
        "policy_snapshot_id": "fixture-2026-10-27",
    }
    if with_span:
        row["citation_span"] = {"field": "abstract_or_excerpt", "start": 0, "end": len(body)}
    return row


def _trends(document: Mapping[str, object]) -> list[dict[str, object]]:
    return _rows(document.get("trends"))


def _evidence(document: Mapping[str, object]) -> list[dict[str, object]]:
    return _rows(document.get("evidence"))


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    rows: list[dict[str, object]] = []
    for item in value:
        rows.append(_object(item))
    return rows


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object")
    return {str(key): item for key, item in value.items()}


def _number_len(value: object) -> int:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return len(value)


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssertionError("expected an int")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return [item for item in value if isinstance(item, str)]


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise AssertionError("expected a string")
    return value
