"""I20 hybrid retrieval on a hand-labelled fixture.

Scores are recomputed from the returned ranking. This is not the sealed I30
test split, and it does not call a remote embedder.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from techpulse.contracts import TaskStatus
from techpulse.index.bm25 import BM25Index
from techpulse.index.embed import (
    VECTOR_DIMENSION,
    ActiveCorpus,
    CorpusDocument,
    EmbedderError,
    HashedNgramEmbedder,
    load_active_corpus,
)
from techpulse.index.hybrid import (
    BM25_K,
    DENSE_K,
    FUSE_K,
    NDCG_THRESHOLD,
    RECALL_THRESHOLD,
    RERANK_K,
    RRF_K,
    evaluate_retrieval,
    ndcg_at_k,
    percentile,
    recall_at_k,
    retrieve,
    rrf,
)
from techpulse.workflows.activities import LocalObjectStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "retrieval" / "labelled.jsonl"


def test_ndcg_and_recall_follow_the_definition() -> None:
    ranking = ["a", "b", "c", "d"]
    relevant = {"a", "c"}
    dcg = 1.0 / math.log2(2) + 0.0 / math.log2(3) + 1.0 / math.log2(4)
    ideal = 1.0 / math.log2(2) + 1.0 / math.log2(3)
    assert ndcg_at_k(ranking, relevant, 10) == pytest.approx(dcg / ideal)
    assert recall_at_k(ranking, relevant, 20) == pytest.approx(1.0)
    assert recall_at_k(["b", "a"], {"a", "c"}, 1) == pytest.approx(0.0)
    assert recall_at_k(["b", "a"], {"a", "c"}, 2) == pytest.approx(0.5)
    assert ndcg_at_k(ranking, set(), 10) == 0.0
    assert recall_at_k(ranking, set(), 20) == 0.0
    assert percentile([], 95.0) == 0.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 95.0) == pytest.approx(4.0)


def test_bm25_ranks_a_matching_document_first() -> None:
    index = BM25Index(
        (
            ("keep", "bm25 sparse retrieval index"),
            ("bread", "bake bread with flour and water"),
        )
    )
    hits = index.search("bm25 sparse retrieval")
    assert hits
    assert hits[0].doc_id == "keep"
    assert all(hit.doc_id != "bread" for hit in hits)


def test_rrf_fuses_both_lists() -> None:
    dense = ["a", "b", "c"]
    lexical = ["b", "c", "d"]

    def score(doc_id: str) -> float:
        total = 0.0
        if doc_id in dense:
            total += 1.0 / (RRF_K + dense.index(doc_id) + 1)
        if doc_id in lexical:
            total += 1.0 / (RRF_K + lexical.index(doc_id) + 1)
        return total

    present = ["a", "b", "c", "d"]
    expected = sorted(present, key=lambda doc_id: (-score(doc_id), doc_id))
    assert rrf(dense, lexical, top_k=10) == expected
    assert expected[0] == "b"


def test_small_corpus_returns_short_lists() -> None:
    corpus = _corpus(
        (
            ("a", "bm25 sparse retrieval index"),
            ("b", "coastal weather brings rain"),
            ("c", "bake bread with flour"),
        )
    )
    result = retrieve("bm25 sparse", corpus, embedder=HashedNgramEmbedder(), language="en")
    assert len(result.dense) <= min(DENSE_K, 3)
    assert len(result.bm25) <= min(BM25_K, 3)
    assert len(result.fused) <= min(FUSE_K, 3)
    assert len(result.reranked) <= min(RERANK_K, len(result.fused))
    assert result.bm25
    assert result.reranked
    assert len(result.reranked) == len(set(result.reranked))


def test_vector_store_down_marks_retrieval_degraded() -> None:
    corpus = _corpus(
        (
            ("keep", "bm25 sparse retrieval index"),
            ("bread", "bake bread with flour and water"),
        )
    )
    embedder = HashedNgramEmbedder()
    blocked = retrieve(
        "bm25 sparse retrieval",
        corpus,
        embedder=embedder,
        vector_store_available=False,
    )
    assert blocked.degraded is True
    assert blocked.dense == ()
    assert blocked.bm25
    assert blocked.bm25[0] == "keep"
    assert blocked.reranked

    class _Down:
        def __init__(self) -> None:
            self.model_id = embedder.model_id
            self.model_version = embedder.model_version
            self.dimension = embedder.dimension
            self.calls_network = False

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            raise EmbedderError("vector model failed")

    failed = retrieve("bm25 sparse retrieval", corpus, embedder=_Down())
    assert failed.degraded is True
    assert failed.dense == ()
    assert failed.bm25[0] == "keep"

    documents, queries = _tiny_rows()
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        outcome = evaluate_retrieval(
            documents,
            queries,
            run_id="i20-down",
            artifacts_dir=artifacts,
            database=root / "index.sqlite",
            store=LocalObjectStore(artifacts),
            embedder=HashedNgramEmbedder(),
            vector_store_available=False,
        )
    assert outcome.status == TaskStatus.SUCCESS
    assert outcome.retrieval_degraded is True
    assert outcome.mvp_threshold_met is False
    assert outcome.cost_usd == 0.0


def test_top10_unique_and_repeat_is_stable() -> None:
    documents, _queries = _load_labelled()
    rows = tuple(
        (str(row["source_record_id"]), str(row["title"]))
        for row in documents
        if isinstance(row.get("source_record_id"), str) and isinstance(row.get("title"), str)
    )
    corpus = _corpus(rows)
    embedder = HashedNgramEmbedder()
    first = retrieve("bm25 sparse retrieval", corpus, embedder=embedder, language="en")
    second = retrieve("bm25 sparse retrieval", corpus, embedder=embedder, language="en")
    assert first.dense == second.dense
    assert first.bm25 == second.bm25
    assert first.fused == second.fused
    assert first.reranked == second.reranked
    assert len(first.reranked) == len(set(first.reranked))
    assert len(first.dense) <= DENSE_K
    assert len(first.bm25) <= BM25_K
    assert len(first.fused) <= FUSE_K
    assert len(first.reranked) <= RERANK_K
    assert len(first.reranked) == min(RERANK_K, len(first.fused))


def test_hand_labelled_fixture_metrics_are_computed() -> None:
    raw = FIXTURE.read_bytes()
    assert len(raw) < 100_000
    documents, queries = _load_labelled()
    doc_ids = {row["source_record_id"] for row in documents}
    assert "en" in {row["language"] for row in queries}
    assert "zh" in {row["language"] for row in queries}
    for query in queries:
        relevant = query["relevant"]
        assert isinstance(relevant, list)
        for item in relevant:
            assert item in doc_ids

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "index.sqlite"
        result = evaluate_retrieval(
            documents,
            queries,
            run_id="i20-fixture",
            artifacts_dir=artifacts,
            database=database,
            store=LocalObjectStore(artifacts),
            embedder=HashedNgramEmbedder(),
        )
        manifest = _object_dict(
            json.loads(
                (artifacts / "i20-fixture" / "I20" / "manifest.json").read_text(encoding="utf-8")
            )
        )
        metrics = _object_dict(
            json.loads(
                (artifacts / "i20-fixture" / "I20" / "metrics.json").read_text(encoding="utf-8")
            )
        )
        corpus = load_active_corpus(database)

    assert result.status == TaskStatus.SUCCESS
    assert corpus.vectors_available is True
    assert manifest["eval_set"] == "hand_labelled_fixture"
    assert metrics["third_party_calls"] == 0
    assert metrics["third_party_writes"] == 0
    assert result.cost_usd == 0.0
    assert metrics["cost_usd"] == 0.0

    stored = manifest["queries"]
    assert isinstance(stored, list)
    ndcg_by_language: dict[str, list[float]] = {}
    recall_by_language: dict[str, list[float]] = {}
    latencies: list[float] = []
    any_query_degraded = False
    for item in stored:
        row = _object_dict(item)
        language = row["language"]
        assert isinstance(language, str)
        ranking = _str_list(row["system_ranking"])
        labels = set(_str_list(row["relevant"]))
        ndcg = ndcg_at_k(ranking, labels, 10)
        recall = recall_at_k(ranking, labels, 20)
        assert _float(row["ndcg_at_10"]) == pytest.approx(ndcg)
        assert _float(row["recall_at_20"]) == pytest.approx(recall)
        ndcg_by_language.setdefault(language, []).append(ndcg)
        recall_by_language.setdefault(language, []).append(recall)
        latencies.append(_float(row["latency_ms"]))
        any_query_degraded = any_query_degraded or row["degraded"] is True
        reranked = _str_list(row["reranked"])
        assert len(reranked) == len(set(reranked))
        assert len(_str_list(row["dense"])) <= DENSE_K
        assert len(_str_list(row["bm25"])) <= BM25_K
        assert len(_str_list(row["fused"])) <= FUSE_K
        assert len(reranked) <= RERANK_K

    for language, values in ndcg_by_language.items():
        assert result.ndcg_at_10[language] == pytest.approx(sum(values) / len(values))
    for language, values in recall_by_language.items():
        assert result.recall_at_20[language] == pytest.approx(sum(values) / len(values))
    assert result.p95_latency_ms == pytest.approx(percentile(latencies, 95.0))
    assert _float(metrics["p95_latency_ms"]) == pytest.approx(result.p95_latency_ms)

    blocked = _below_threshold(result.ndcg_at_10, result.recall_at_20)
    assert result.high_confidence_blocked is blocked
    assert result.retrieval_degraded is (blocked or any_query_degraded)
    assert result.mvp_threshold_met is (
        not result.retrieval_degraded and not blocked and len(stored) > 0
    )
    assert metrics["mvp_threshold_met"] is result.mvp_threshold_met
    assert metrics["retrieval_degraded"] is result.retrieval_degraded
    assert metrics["high_confidence_blocked"] is result.high_confidence_blocked


def _corpus(rows: Sequence[tuple[str, str]]) -> ActiveCorpus:
    embedder = HashedNgramEmbedder()
    vectors = embedder.embed(tuple(body for _doc_id, body in rows))
    documents: list[CorpusDocument] = []
    for (doc_id, body), vector in zip(rows, vectors, strict=True):
        documents.append(
            CorpusDocument(
                vector_id=doc_id,
                chunk_id=doc_id,
                source_id="fixture",
                source_record_id=doc_id,
                language="en",
                license_status="fulltext_allowed",
                body=body,
                content_sha256="0" * 64,
                embedding=tuple(vector),
            )
        )
    return ActiveCorpus(
        text_version="text",
        published_version="text",
        model_id=embedder.model_id,
        model_version=embedder.model_version,
        dimension=VECTOR_DIMENSION,
        vectors_available=True,
        documents=tuple(documents),
    )


def _tiny_rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    documents: list[dict[str, object]] = [
        {
            "source_id": "fixture",
            "source_record_id": "keep",
            "language": "en",
            "license_status": "fulltext_allowed",
            "title": "bm25 sparse retrieval index",
            "abstract_or_excerpt": "",
        },
        {
            "source_id": "fixture",
            "source_record_id": "bread",
            "language": "en",
            "license_status": "fulltext_allowed",
            "title": "bake bread with flour and water",
            "abstract_or_excerpt": "",
        },
    ]
    queries: list[dict[str, object]] = [
        {
            "query_id": "q-keep",
            "language": "en",
            "query": "bm25 sparse retrieval",
            "relevant": ["keep"],
        }
    ]
    return documents, queries


def _load_labelled() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    documents: list[dict[str, object]] = []
    queries: list[dict[str, object]] = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.strip() == "":
            continue
        row = _object_dict(json.loads(line))
        if row.get("kind") == "doc":
            documents.append(row)
        elif row.get("kind") == "query":
            queries.append(row)
    return documents, queries


def _object_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _str_list(value: object) -> list[str]:
    assert isinstance(value, list)
    items: list[str] = []
    for item in value:
        assert isinstance(item, str)
        items.append(item)
    return items


def _float(value: object) -> float:
    assert isinstance(value, int | float) and not isinstance(value, bool)
    return float(value)


def _below_threshold(ndcg: Mapping[str, float], recall: Mapping[str, float]) -> bool:
    languages = set(ndcg) | set(recall)
    if not languages:
        return True
    for language in languages:
        if ndcg.get(language, 0.0) < NDCG_THRESHOLD:
            return True
        if recall.get(language, 0.0) < RECALL_THRESHOLD:
            return True
    return False
