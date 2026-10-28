"""E20 contrary queries, conflict review, and the QA queue."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from techpulse.evidence import contradictions as contradiction_module
from techpulse.evidence.contradictions import (
    ContradictionReview,
    review_contradictions,
    run_contradictions,
)
from techpulse.evidence.verify import build_evidence_pack
from techpulse.index.embed import Embedder

START = "2026-10-22T00:00:00Z"
END = "2026-10-29T00:00:00Z"
AS_OF = "2026-10-28T15:00:00Z"
COLLECTED = "2026-10-27T00:00:00Z"
SUPPORT_EXCERPT = "RAG records changed from 4 to 5."


def test_every_claim_gets_negation_and_alternative_queries() -> None:
    review = _review()
    chains = _rows(review.document["chains"])
    assert chains
    templates = {chain["template"] for chain in chains}
    assert "growth_7d" in templates
    for chain in chains:
        queries = _rows(chain["queries"])
        assert [query["kind"] for query in queries] == ["negation", "alternative_explanation"]
        assert all(isinstance(query["text"], str) and query["text"] for query in queries)
    growth = next(chain for chain in chains if chain["template"] == "growth_7d")
    negation = _rows(growth["queries"])[0]["text"]
    alternative = _rows(growth["queries"])[1]["text"]
    assert negation == "topic:rag independent records did not change from 4 to 5"
    assert "alternative explanation" in str(alternative)
    assert "raw records" in str(alternative)
    assert "including reprints" in str(alternative)
    again = _review()
    assert review.document == again.document


def test_no_conflict_keeps_high_and_records_a_chain_for_every_candidate() -> None:
    review = _review()
    assert review.status == "success"
    assert review.metrics["llm_calls"] == 0
    assert review.metrics["paid_api_calls"] == 0
    assert review.metrics["high_confidence_chain_rate"] == 1.0
    assert review.metrics["candidates_with_chain"] == review.metrics["candidates"]
    assert review.document["contradictions"] == []
    assert review.document["qa_queue"] == []
    candidates = _rows(review.document["candidates"])
    high = [row for row in candidates if row["confidence"] == "high"]
    assert high
    assert all(row["contrary_search_executed"] is True for row in high)
    assert all(row["contrary_search_incomplete"] is False for row in high)
    assert all(row["major_conflict"] is False for row in high)
    growth = _chain(review.document, "growth_7d")
    assert growth["status"] == "counterevidence_not_found"
    assert growth["queries"]


def test_finished_search_satisfies_the_contrary_gate() -> None:
    pack = build_evidence_pack(
        [_trend(7, contrary_search_executed=False)],
        _support_records(),
        features=[_feature()],
    )
    assert _rows(pack.document["trends"])[0]["confidence"] == "medium"
    review = review_contradictions(pack.document, _support_records())
    assert _rows(review.document["candidates"])[0]["confidence"] == "high"
    assert _rows(review.document["candidates"])[0]["contrary_search_executed"] is True


def test_irrelevant_hits_are_not_contradictions() -> None:
    generic = _doc("rss", "generic", "news", "A", "2026-10-23T00:00:00Z", "RAG evidence for paper")
    other_topic = _doc(
        "hn",
        "other",
        "community",
        "A",
        "2026-10-24T00:00:00Z",
        "Independent records changed from 4 to 5.",
    )
    other_topic["title"] = "Weekly counts"
    clipped = _doc(
        "arxiv",
        "clip",
        "academic",
        "A",
        "2026-10-24T00:00:00Z",
        "RAG did not change from 4 to 5.",
    )
    clipped["citation_span"] = {"field": "abstract_or_excerpt", "start": 0, "end": 3}
    missing_span = _doc(
        "github",
        "nospan",
        "code",
        "A",
        "2026-10-24T00:00:00Z",
        "RAG independent records did not change from 4 to 5.",
    )
    del missing_span["citation_span"]
    review = _review([generic, other_topic, clipped, missing_span])
    urls = _urls(review.document)
    assert generic["canonical_url"] not in urls
    assert other_topic["canonical_url"] not in urls
    assert clipped["canonical_url"] not in urls
    assert missing_span["canonical_url"] not in urls
    assert review.document["contradictions"] == []
    assert _as_int(review.metrics["irrelevant_dropped"]) >= 1
    growth = _chain(review.document, "growth_7d")
    assert _as_int(growth["span_missing_dropped"]) >= 1
    assert _rows(review.document["candidates"])[0]["confidence"] == "high"


def test_opposing_conclusion_downgrades_and_copies_the_upstream_span() -> None:
    opposed = _doc(
        "arxiv",
        "opposed",
        "academic",
        "A",
        "2026-10-26T00:00:00Z",
        "RAG independent records did not change from 4 to 5.",
    )
    review = _review([opposed])
    contradictions = _rows(review.document["contradictions"])
    assert len(contradictions) == 1
    item = contradictions[0]
    assert item["source_url"] == opposed["canonical_url"]
    assert item["sha256"] == opposed["content_sha256"]
    assert item["major"] is True
    assert "opposing_conclusion" in _strings(item["kinds"])
    span = _object(item["citation_span"])
    source = str(opposed[str(span["field"])])
    assert source[int(str(span["start"])) : int(str(span["end"]))] == item["normalized_excerpt"]
    candidate = _rows(review.document["candidates"])[0]
    assert candidate["prior_confidence"] == "high"
    assert candidate["confidence"] == "medium"
    assert candidate["downgraded"] is True
    assert candidate["major_conflict"] is True
    assert candidate["removed_from_high_confidence"] is True
    assert review.document["high_confidence_trend_ids"] == []
    assert _chain(review.document, "growth_7d")["status"] == "conflict"


def test_numeric_disagreement_goes_to_the_qa_queue_without_a_downgrade() -> None:
    shifted = _doc(
        "github",
        "shifted",
        "code",
        "A",
        "2026-10-26T00:00:00Z",
        "RAG records changed from 4 to 9.",
    )
    review = _review([shifted])
    assert review.document["contradictions"] == []
    queued = _rows(review.document["qa_queue"])
    assert len(queued) == 1
    assert queued[0]["reason"] == "numeric_inconsistency"
    assert queued[0]["source_url"] == shifted["canonical_url"]
    assert _object(queued[0]["observed"]) == {"growth_previous": 4, "growth_current": 9}
    assert _rows(review.document["candidates"])[0]["confidence"] == "high"
    assert _rows(review.document["candidates"])[0]["major_conflict"] is False


def test_publication_date_conflict_downgrades_and_enters_the_qa_queue() -> None:
    early = _doc(
        "openalex",
        "early",
        "academic",
        "A",
        "2026-09-01T00:00:00Z",
        SUPPORT_EXCERPT,
    )
    review = _review([early])
    item = _rows(review.document["contradictions"])[0]
    assert "publication_date_conflict" in _strings(item["kinds"])
    assert item["source_url"] == early["canonical_url"]
    assert item["source_published_at"] == early["published_at"]
    queued = _rows(review.document["qa_queue"])
    assert [row["reason"] for row in queued] == ["date_inconsistency"]
    assert queued[0]["source_url"] == early["canonical_url"]
    assert _rows(review.document["candidates"])[0]["confidence"] == "medium"


def test_statistical_definition_is_a_major_conflict() -> None:
    defined = _doc(
        "hn",
        "defined",
        "community",
        "B",
        "2026-10-25T00:00:00Z",
        "RAG raw records including reprints changed from 4 to 5.",
    )
    review = _review([defined])
    item = _rows(review.document["contradictions"])[0]
    assert "statistical_definition" in _strings(item["kinds"])
    assert item["source_url"] == defined["canonical_url"]
    assert _rows(review.document["candidates"])[0]["confidence"] == "medium"
    assert _rows(review.document["candidates"])[0]["major_conflict"] is True


def test_same_origin_reprint_is_not_a_second_contradiction() -> None:
    left = _doc(
        "arxiv",
        "left",
        "academic",
        "A",
        "2026-10-26T00:00:00Z",
        "RAG independent records did not change from 4 to 5.",
    )
    right = _doc(
        "rss",
        "right",
        "news",
        "C",
        "2026-10-25T00:00:00Z",
        "RAG independent records did not change from 4 to 5.",
    )
    review = _review(
        [left, right],
        clusters={"arxiv:left": "cluster-opposed", "rss:right": "cluster-opposed"},
    )
    contradictions = _rows(review.document["contradictions"])
    reprints = _rows(review.document["reprints"])
    assert len(contradictions) == 1
    assert contradictions[0]["source_url"] == left["canonical_url"]
    growth_reprints = [
        row for row in reprints if row.get("claim_id") == contradictions[0]["claim_id"]
    ]
    assert len(growth_reprints) == 1
    assert growth_reprints[0]["source_url"] == right["canonical_url"]
    assert growth_reprints[0]["cluster_id"] == "cluster-opposed"
    assert _as_int(review.metrics["same_origin_reprints"]) >= 1
    assert _rows(review.document["candidates"])[0]["confidence"] == "medium"


def test_support_cluster_reprint_does_not_downgrade() -> None:
    records = _support_records()
    copy = _doc("rss", "copy", "news", "C", "2026-10-22T00:00:00Z", SUPPORT_EXCERPT)
    clusters = {"openalex:paper": "cluster-paper", "rss:copy": "cluster-paper"}
    pack = build_evidence_pack(
        [_trend(7)],
        [*records, copy],
        features=[_feature()],
        clusters=clusters,
    )
    review = review_contradictions(
        pack.document,
        [*records, copy],
        clusters=clusters,
    )
    assert review.document["contradictions"] == []
    reprints = _rows(review.document["reprints"])
    assert copy["canonical_url"] in {row["source_url"] for row in reprints}
    assert _rows(review.document["candidates"])[0]["confidence"] == "high"


def test_same_cluster_reprint_does_not_downgrade_independent_opposition_does() -> None:
    records = _support_records()
    reprint = _doc(
        "rss",
        "reprint",
        "news",
        "A",
        "2026-10-26T00:00:00Z",
        "RAG independent records did not change from 4 to 5.",
    )
    clusters = {"openalex:paper": "cluster-paper", "rss:reprint": "cluster-paper"}
    bundled = [*records, reprint]
    pack = build_evidence_pack(
        [_trend(7)],
        bundled,
        features=[_feature()],
        clusters=clusters,
    )
    evidence = _rows(pack.document["evidence"])
    reprint_item = next(item for item in evidence if item["source_url"] == reprint["canonical_url"])
    growth = next(row for row in _rows(pack.document["claims"]) if row["template"] == "growth_7d")
    assert reprint_item["evidence_id"] in _strings(growth["supporting_evidence_ids"])
    assert reprint_item["cluster_id"] == "cluster-paper"
    review = review_contradictions(pack.document, bundled, clusters=clusters)
    contradictions = _rows(review.document["contradictions"])
    reprints = _rows(review.document["reprints"])
    matched = [row for row in reprints if row["source_url"] == reprint["canonical_url"]]
    assert matched
    assert any("opposing_conclusion" in _strings(row["kinds"]) for row in matched)
    assert all(row.get("source_url") != reprint["canonical_url"] for row in contradictions)
    assert all(
        row.get("cluster_id") != "cluster-paper" or row.get("independent") is not True
        for row in contradictions
    )
    candidate = _rows(review.document["candidates"])[0]
    assert candidate["confidence"] == "high"
    assert candidate["downgraded"] is False
    assert candidate["major_conflict"] is False
    assert candidate["removed_from_high_confidence"] is False

    opposed = _doc(
        "arxiv",
        "opposed",
        "academic",
        "A",
        "2026-10-26T00:00:00Z",
        "RAG independent records did not change from 4 to 5.",
    )
    opposed_review = _review([opposed])
    opposed_rows = _rows(opposed_review.document["contradictions"])
    assert any(
        row["source_url"] == opposed["canonical_url"] and row["independent"] is True
        for row in opposed_rows
    )
    opposed_candidate = _rows(opposed_review.document["candidates"])[0]
    assert opposed_candidate["prior_confidence"] == "high"
    assert opposed_candidate["confidence"] == "medium"
    assert opposed_candidate["downgraded"] is True
    assert opposed_candidate["major_conflict"] is True


def test_retrieval_unavailable_is_incomplete_and_caps_confidence() -> None:
    opposed = _doc(
        "arxiv",
        "opposed",
        "academic",
        "A",
        "2026-10-26T00:00:00Z",
        "RAG independent records did not change from 4 to 5.",
    )
    review = _review([opposed], retrieval_available=False)
    assert review.status == "contrary_search_incomplete"
    assert review.document["contradictions"] == []
    assert review.document["qa_queue"] == []
    candidate = _rows(review.document["candidates"])[0]
    assert candidate["confidence"] == "medium"
    assert candidate["contrary_search_incomplete"] is True
    assert candidate["contrary_search_executed"] is False
    chains = _rows(review.document["chains"])
    assert chains
    assert all(chain["status"] == "contrary_search_incomplete" for chain in chains)
    assert all(len(_rows(chain["queries"])) == 2 for chain in chains)
    assert review.metrics["high_confidence_chain_rate"] == 1.0


def test_degraded_retrieval_cannot_stay_high() -> None:
    review = _review(vector_store_available=False)
    candidate = _rows(review.document["candidates"])[0]
    assert candidate["retrieval_degraded"] is True
    assert candidate["contrary_search_incomplete"] is False
    assert candidate["confidence"] == "medium"
    assert review.status == "success"
    assert _chain(review.document, "growth_7d")["status"] == "counterevidence_not_found"


def test_blocked_and_insecure_urls_are_not_copied() -> None:
    blocked = _doc(
        "hn",
        "blocked",
        "community",
        "A",
        "2026-10-24T00:00:00Z",
        "RAG independent records did not change from 4 to 5.",
    )
    blocked["license_status"] = "blocked"
    insecure = _doc(
        "github",
        "insecure",
        "code",
        "A",
        "2026-10-24T00:00:00Z",
        "RAG independent records did not change from 4 to 5.",
    )
    insecure["canonical_url"] = "http://example.org/insecure"
    review = _review([blocked, insecure])
    assert blocked["canonical_url"] not in _urls(review.document)
    assert insecure["canonical_url"] not in _urls(review.document)
    assert review.document["contradictions"] == []


def test_run_writes_artifacts_and_sqlite() -> None:
    opposed = _doc(
        "arxiv",
        "opposed",
        "academic",
        "A",
        "2026-10-26T00:00:00Z",
        "RAG independent records did not change from 4 to 5.",
    )
    pack = build_evidence_pack([_trend(7)], _support_records(), features=[_feature()])
    records = [*_support_records(), opposed]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        review = run_contradictions(
            pack.document,
            records,
            run_id="e20-unit",
            artifacts_dir=root,
        )
        directory = root / "e20-unit" / "E20"
        body = (directory / "contradictions.json").read_bytes()
        stored = _object(json.loads(body.decode("utf-8")))
        manifest = _object(json.loads((directory / "manifest.json").read_text(encoding="utf-8")))
        metrics = _object(json.loads((directory / "metrics.json").read_text(encoding="utf-8")))
        assert stored == review.document
        assert manifest["status"] == "success"
        assert manifest["contradictions_sha256"] == hashlib.sha256(body).hexdigest()
        assert metrics["third_party_writes"] == 0
        assert metrics["high_confidence_chain_rate"] == 1.0
        connection = sqlite3.connect(directory / "evidence.sqlite")
        try:
            confidence = connection.execute(
                "SELECT confidence FROM trend_scores WHERE trend_id = ?",
                (_trend(7)["trend_id"],),
            ).fetchone()
            relations = connection.execute(
                "SELECT relation, evidence_id FROM trend_evidence"
            ).fetchall()
            queued = connection.execute("SELECT reason FROM qa_queue").fetchall()
            urls = connection.execute("SELECT canonical_url FROM raw_records").fetchall()
        finally:
            connection.close()
        assert confidence == ("medium",)
        assert relations
        assert {row[0] for row in relations} == {"contradict"}
        assert queued == []
        assert all(str(url).startswith("https://") for (url,) in urls)
        assert opposed["canonical_url"] in {str(url) for (url,) in urls}
    with pytest.raises(ValueError, match="invalid run_id"):
        run_contradictions(pack.document, records, run_id="../e20", artifacts_dir=Path("."))


def test_network_embedder_is_refused() -> None:
    class _Remote:
        calls_network = True
        model_id = "remote"
        model_version = "1"
        dimension = 3

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            raise AssertionError(texts)

    pack = build_evidence_pack([_trend(7)], _support_records(), features=[_feature()])
    remote: Embedder = _Remote()
    with pytest.raises(ValueError, match="network"):
        review_contradictions(pack.document, _support_records(), embedder=remote)


def test_module_does_not_call_a_model_api() -> None:
    folded = inspect.getsource(contradiction_module).lower()
    assert "openai" not in folded
    assert "httpx" not in folded
    assert "embedder_from_env" not in folded


def _review(
    extra: Sequence[Mapping[str, object]] | None = None,
    *,
    clusters: Mapping[str, str] | None = None,
    retrieval_available: bool = True,
    vector_store_available: bool | None = None,
) -> ContradictionReview:
    records = [*_support_records(), *(extra or ())]
    pack = build_evidence_pack([_trend(7)], _support_records(), features=[_feature()])
    return review_contradictions(
        pack.document,
        records,
        clusters=clusters,
        retrieval_available=retrieval_available,
        vector_store_available=vector_store_available,
    )


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
    excerpt: str,
) -> dict[str, object]:
    return {
        "source_id": source,
        "source_record_id": sid,
        "source_family": family,
        "quality_tier": tier,
        "canonical_id": f"{source}:{sid}",
        "canonical_url": f"https://example.org/{source}/{sid}",
        "title": f"RAG note {sid}",
        "abstract_or_excerpt": excerpt,
        "published_at": published,
        "collected_at": COLLECTED,
        "license_status": "metadata_only",
        "content_sha256": hashlib.sha256(f"{source}:{sid}".encode()).hexdigest(),
        "policy_snapshot_id": "fixture-2026-10-27",
        "citation_span": {"field": "abstract_or_excerpt", "start": 0, "end": len(excerpt)},
    }


def _chain(document: object, template: str) -> dict[str, object]:
    if not isinstance(document, Mapping):
        raise AssertionError("expected a document")
    return next(row for row in _rows(document.get("chains")) if row.get("template") == template)


def _urls(document: object) -> set[object]:
    if not isinstance(document, Mapping):
        raise AssertionError("expected a document")
    found: set[object] = set()
    for key in ("contradictions", "reprints", "qa_queue"):
        for row in _rows(document.get(key)):
            found.add(row.get("source_url"))
    return found


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return [_object(item) for item in value]


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object")
    return {str(key): item for key, item in value.items()}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return [item for item in value if isinstance(item, str)]


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssertionError("expected an int")
    return value
