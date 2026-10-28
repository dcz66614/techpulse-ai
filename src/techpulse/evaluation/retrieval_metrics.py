"""Retrieval evaluation for the frozen I30 gold set."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NDCG_AT_10_THRESHOLD = 0.75
RECALL_AT_20_THRESHOLD = 0.85


@dataclass(frozen=True)
class QueryJudgement:
    """One query and its graded relevance labels keyed by document id."""

    query_id: str
    query: str
    language: str
    topic: str
    split: str
    relevance: Mapping[str, int]


@dataclass(frozen=True)
class AggregateMetrics:
    """Macro-averaged retrieval metrics for a group of queries."""

    ndcg_at_10: float
    recall_at_20: float
    queries: int

    def passes_thresholds(self) -> bool:
        return (
            self.ndcg_at_10 >= NDCG_AT_10_THRESHOLD and self.recall_at_20 >= RECALL_AT_20_THRESHOLD
        )


@dataclass(frozen=True)
class QueryMetrics:
    """Retrieval metrics for one query."""

    query_id: str
    language: str
    topic: str
    ndcg_at_10: float
    recall_at_20: float


@dataclass(frozen=True)
class EvaluationResult:
    """Overall, per-language, per-topic, and per-query evaluation output."""

    overall: AggregateMetrics
    by_language: Mapping[str, AggregateMetrics]
    by_topic: Mapping[str, AggregateMetrics]
    per_query: tuple[QueryMetrics, ...]


def dcg_at_k(relevances: Sequence[int], k: int) -> float:
    """Discounted cumulative gain with exponential gain and log2 discount."""
    if k <= 0:
        return 0.0
    total = 0.0
    for rank, grade in enumerate(relevances[:k], start=1):
        total += (2**grade - 1) / math.log2(rank + 1)
    return total


def ndcg_at_k(
    ranked_ids: Sequence[str],
    relevance: Mapping[str, int],
    k: int,
) -> float:
    """Normalised DCG at k using graded relevance; missing ids grade 0.

    Duplicate document ids in the ranked list are ignored after their first
    occurrence so a retriever cannot earn extra credit by repeating a hit.
    """
    if k <= 0:
        return 0.0
    unique_ids = _dedupe(ranked_ids)
    actual = dcg_at_k([relevance.get(doc_id, 0) for doc_id in unique_ids], k)
    ideal_grades = sorted((grade for grade in relevance.values() if grade > 0), reverse=True)
    ideal = dcg_at_k(ideal_grades, k)
    if ideal == 0.0:
        return 0.0
    return actual / ideal


def recall_at_k(
    ranked_ids: Sequence[str],
    relevance: Mapping[str, int],
    k: int,
) -> float:
    """Fraction of graded-relevant documents found in the first k results."""
    relevant = {doc_id for doc_id, grade in relevance.items() if grade > 0}
    if not relevant:
        return 0.0
    retrieved = set(ranked_ids[:k]) if k > 0 else set()
    return len(retrieved & relevant) / len(relevant)


def evaluate(
    retriever: Callable[[str], Sequence[str]],
    gold: Iterable[QueryJudgement],
) -> EvaluationResult:
    """Evaluate any ``query -> ranked document ids`` callable against gold.

    The caller chooses the split. This function does not know or enforce the
    test split, so callers can pass only the train or dev judgements.
    """
    per_query_list: list[QueryMetrics] = []
    for judgement in gold:
        ranked_ids = retriever(judgement.query)
        per_query_list.append(
            QueryMetrics(
                query_id=judgement.query_id,
                language=judgement.language,
                topic=judgement.topic,
                ndcg_at_10=ndcg_at_k(ranked_ids, judgement.relevance, 10),
                recall_at_20=recall_at_k(ranked_ids, judgement.relevance, 20),
            )
        )
    per_query = tuple(per_query_list)
    return EvaluationResult(
        overall=_aggregate(per_query),
        by_language=_group(per_query, lambda metric: metric.language),
        by_topic=_group(per_query, lambda metric: metric.topic),
        per_query=per_query,
    )


def load_gold_queries(path: Path) -> tuple[QueryJudgement, ...]:
    """Load and validate the I30 JSONL query file."""
    queries: list[QueryJudgement] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        relevance_raw = raw.get("relevance")
        if not isinstance(relevance_raw, dict):
            raise ValueError(f"{path}:{line_number}: relevance must be an object")
        relevance: dict[str, int] = {}
        for doc_id, grade in relevance_raw.items():
            if not isinstance(doc_id, str) or not doc_id:
                raise ValueError(f"{path}:{line_number}: document ids must be non-empty strings")
            if isinstance(grade, bool) or not isinstance(grade, int) or grade < 0:
                raise ValueError(
                    f"{path}:{line_number}: grade for {doc_id!r} must be a non-negative integer"
                )
            relevance[doc_id] = grade
        queries.append(
            QueryJudgement(
                query_id=_require_text(raw, "query_id", path, line_number),
                query=_require_text(raw, "query", path, line_number),
                language=_require_text(raw, "language", path, line_number),
                topic=_require_text(raw, "topic", path, line_number),
                split=_require_text(raw, "split", path, line_number),
                relevance=relevance,
            )
        )
    return tuple(queries)


def _dedupe(ranked_ids: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for doc_id in ranked_ids:
        if doc_id not in seen:
            seen.add(doc_id)
            unique.append(doc_id)
    return unique


def _aggregate(metrics: Sequence[QueryMetrics]) -> AggregateMetrics:
    if not metrics:
        return AggregateMetrics(ndcg_at_10=0.0, recall_at_20=0.0, queries=0)
    count = len(metrics)
    return AggregateMetrics(
        ndcg_at_10=sum(metric.ndcg_at_10 for metric in metrics) / count,
        recall_at_20=sum(metric.recall_at_20 for metric in metrics) / count,
        queries=count,
    )


def _group(
    metrics: Sequence[QueryMetrics],
    key: Callable[[QueryMetrics], str],
) -> dict[str, AggregateMetrics]:
    grouped: dict[str, list[QueryMetrics]] = {}
    for metric in metrics:
        grouped.setdefault(key(metric), []).append(metric)
    return {name: _aggregate(values) for name, values in sorted(grouped.items())}


def _require_text(
    row: Mapping[str, Any],
    key: str,
    path: Path,
    line_number: int,
) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}:{line_number}: {key} must be a non-empty string")
    return value
