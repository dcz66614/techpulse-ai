from __future__ import annotations

import json
import math
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

from techpulse.evaluation.retrieval_metrics import (
    AggregateMetrics,
    QueryJudgement,
    evaluate,
    load_gold_queries,
    ndcg_at_k,
    recall_at_k,
)

ROOT = Path(__file__).parents[2]


def test_ndcg_at_10_hand_computed() -> None:
    relevance = {"a": 3, "b": 2, "c": 1}
    expected = (3 + 7 / math.log2(3) + 0.5) / (7 + 3 / math.log2(3) + 0.5)
    assert ndcg_at_k(("b", "a", "c"), relevance, 10) == pytest.approx(expected)
    assert ndcg_at_k(("b", "a", "c"), relevance, 10) == pytest.approx(0.8428, abs=1e-4)
    assert ndcg_at_k(("a", "b", "c"), relevance, 10) == pytest.approx(1.0)
    assert ndcg_at_k(("b", "a", "c"), relevance, 1) == pytest.approx(3 / 7)
    assert ndcg_at_k(("a", "b", "c"), {}, 10) == 0.0


def test_ndcg_ignores_duplicate_hits() -> None:
    relevance = {"a": 3, "b": 2}
    assert ndcg_at_k(("a", "a", "b"), relevance, 10) == pytest.approx(
        ndcg_at_k(("a", "b"), relevance, 10)
    )
    assert ndcg_at_k(("a", "a", "b"), relevance, 10) == pytest.approx(1.0)


def test_recall_at_20_hand_computed() -> None:
    relevance = {"a": 3, "b": 2, "d": 1}
    assert recall_at_k(("x", "a", "b", "c"), relevance, 20) == pytest.approx(2 / 3)
    assert recall_at_k(("x", "a", "b", "c"), relevance, 2) == pytest.approx(1 / 3)
    assert recall_at_k(("x", "a", "b", "c"), relevance, 0) == 0.0
    assert recall_at_k(("a", "b", "d"), relevance, 20) == pytest.approx(1.0)
    assert recall_at_k(("a",), {}, 20) == 0.0


def test_evaluate_aggregates_per_query_language_and_topic() -> None:
    gold = (
        QueryJudgement("q1", "first", "en", "rag", "train", {"a": 3, "b": 2}),
        QueryJudgement("q2", "second", "zh", "rag", "dev", {"c": 1}),
    )

    def retriever(query: str) -> Sequence[str]:
        if query == "first":
            return ("b", "a")
        return ("x", "c")

    result = evaluate(retriever, gold)
    first = (3 + 7 / math.log2(3)) / (7 + 3 / math.log2(3))
    second = 1 / math.log2(3)
    assert result.per_query[0].ndcg_at_10 == pytest.approx(first)
    assert result.per_query[1].ndcg_at_10 == pytest.approx(second)
    assert result.overall.ndcg_at_10 == pytest.approx((first + second) / 2)
    assert result.overall.recall_at_20 == pytest.approx(1.0)
    assert result.by_language["en"].queries == 1
    assert result.by_language["zh"].ndcg_at_10 == pytest.approx(second)
    assert result.by_topic["rag"].queries == 2
    assert result.per_query[0].query_id == "q1"


def test_thresholds_match_section_10() -> None:
    assert AggregateMetrics(0.75, 0.85, 1).passes_thresholds()
    assert not AggregateMetrics(0.749, 0.85, 1).passes_thresholds()
    assert not AggregateMetrics(0.75, 0.849, 1).passes_thresholds()


def test_load_gold_queries_from_tempfile() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "queries.jsonl"
        path.write_text(
            json.dumps(
                {
                    "query_id": "q1",
                    "query": "hello",
                    "language": "en",
                    "topic": "rag",
                    "split": "train",
                    "relevance": {"d1": 2},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        loaded = load_gold_queries(path)
    assert len(loaded) == 1
    assert loaded[0].query_id == "q1"
    assert loaded[0].relevance == {"d1": 2}


def test_load_gold_queries_rejects_negative_grade() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "queries.jsonl"
        path.write_text(
            json.dumps(
                {
                    "query_id": "q1",
                    "query": "hello",
                    "language": "en",
                    "topic": "rag",
                    "split": "train",
                    "relevance": {"d1": -1},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="non-negative integer"):
            load_gold_queries(path)


def test_shipped_gold_set_covers_topics_languages_and_splits() -> None:
    corpus_path = ROOT / "data" / "gold" / "retrieval" / "corpus.jsonl"
    queries_path = ROOT / "data" / "gold" / "retrieval" / "queries.jsonl"
    corpus_ids = {
        str(json.loads(line)["doc_id"])
        for line in corpus_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    queries = load_gold_queries(queries_path)
    assert len(corpus_ids) == 32
    assert len(queries) >= 40
    assert {query.language for query in queries} == {"en", "zh"}
    assert {query.topic for query in queries} == {
        "ai_agents",
        "rag",
        "vector_databases",
        "developer_tools",
    }
    assert all(any(grade > 0 for grade in query.relevance.values()) for query in queries)
    assert all(set(query.relevance) <= corpus_ids for query in queries)
    assert all(0 <= grade <= 3 for query in queries for grade in query.relevance.values())
    assert len({query.query_id for query in queries}) == len(queries)
    assert Counter(query.split for query in queries) == {"train": 24, "dev": 8, "test": 8}
    assert {query.query for query in queries if query.split == "train"}.isdisjoint(
        {query.query for query in queries if query.split == "test"}
    )
