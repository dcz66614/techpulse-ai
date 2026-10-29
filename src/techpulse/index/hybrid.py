"""Dense plus BM25 retrieval with RRF and reranking.

Dense top-100 and BM25 top-100 are fused with reciprocal rank fusion to
top-50, then the reranker keeps top-10. A corpus smaller than those cuts
returns every row it has. If the vector index is unavailable, retrieval
falls back to BM25 and sets ``retrieval_degraded``.

nDCG@10 and Recall@20 are computed from the ranking. They are not constants.
The numeric bars 0.75 and 0.85 are the spec thresholds. ``mvp_threshold_met``
is that comparison on the labelled rows passed in, which for the shipped
fixture is a small hand-labelled set, not a sealed test split.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from techpulse.contracts import TaskStatus
from techpulse.index.bm25 import BM25Index
from techpulse.index.embed import (
    ActiveCorpus,
    CorpusDocument,
    Embedder,
    EmbedderDisabled,
    EmbedderError,
    HashedNgramEmbedder,
    build_index,
    load_active_corpus,
)
from techpulse.index.rerank import OverlapReranker, RerankCandidate, Reranker
from techpulse.workflows.activities import ObjectStore

DENSE_K = 100
BM25_K = 100
FUSE_K = 50
RERANK_K = 10
RRF_K = 60
NDCG_AT = 10
RECALL_AT = 20
NDCG_THRESHOLD = 0.75
RECALL_THRESHOLD = 0.85


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    language: str
    dense: tuple[str, ...]
    bm25: tuple[str, ...]
    fused: tuple[str, ...]
    reranked: tuple[str, ...]
    degraded: bool
    latency_ms: float


@dataclass(frozen=True)
class RetrievalEval:
    status: TaskStatus
    run_id: str
    retrieval_degraded: bool
    high_confidence_blocked: bool
    mvp_threshold_met: bool
    ndcg_at_10: dict[str, float]
    recall_at_20: dict[str, float]
    p95_latency_ms: float
    cost_usd: float
    errors: tuple[str, ...]


def ndcg_at_k(ranked_ids: Sequence[str], relevant: set[str], k: int) -> float:
    """Binary nDCG. Empty relevance returns 0."""
    if k <= 0 or not relevant:
        return 0.0
    dcg = 0.0
    for index, doc_id in enumerate(ranked_ids[:k], start=1):
        if doc_id in relevant:
            dcg += 1.0 / math.log2(index + 1)
    ideal = 0.0
    for index in range(1, min(k, len(relevant)) + 1):
        ideal += 1.0 / math.log2(index + 1)
    if ideal == 0.0:
        return 0.0
    return dcg / ideal


def recall_at_k(ranked_ids: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of relevant ids present in the first ``k`` ranks."""
    if k <= 0 or not relevant:
        return 0.0
    hits = sum(1 for doc_id in ranked_ids[:k] if doc_id in relevant)
    return hits / len(relevant)


def percentile(values: Sequence[float], percent: float) -> float:
    """Nearest-rank percentile. An empty sample is 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(percent / 100.0 * len(ordered))
    index = min(len(ordered), max(rank, 1)) - 1
    return ordered[index]


def dense_search(
    query_vector: Sequence[float],
    documents: Sequence[CorpusDocument],
    top_k: int,
) -> list[str]:
    """Cosine rank. Vectors are L2-normalised, so the score is a dot product."""
    if top_k <= 0:
        return []
    scored: list[tuple[float, str]] = []
    for document in documents:
        embedding = document.embedding
        if embedding is None or len(embedding) != len(query_vector):
            continue
        score = _dot(query_vector, embedding)
        scored.append((score, document.source_record_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    limit = min(top_k, len(scored))
    return [doc_id for _score, doc_id in scored[:limit]]


def rrf(
    dense_ids: Sequence[str],
    bm25_ids: Sequence[str],
    top_k: int,
    rrf_k: int = RRF_K,
) -> list[str]:
    """Reciprocal rank fusion. Ranks are 1-based. Ties break by document id."""
    if top_k <= 0:
        return []
    scores: dict[str, float] = {}
    for rank, doc_id in enumerate(dense_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rrf_k + rank)
    for rank, doc_id in enumerate(bm25_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rrf_k + rank)
    ordered = sorted(scores, key=lambda doc_id: (-scores[doc_id], doc_id))
    return ordered[: min(top_k, len(ordered))]


def system_ranking(result: RetrievalResult) -> list[str]:
    """Reranked ids, then any fused ids the reranker did not keep."""
    ordered: list[str] = []
    for doc_id in result.reranked:
        if doc_id not in ordered:
            ordered.append(doc_id)
    for doc_id in result.fused:
        if doc_id not in ordered:
            ordered.append(doc_id)
    return ordered


def retrieve(
    query: str,
    corpus: ActiveCorpus,
    *,
    embedder: Embedder,
    language: str = "und",
    reranker: Reranker | None = None,
    vector_store_available: bool | None = None,
    dense_k: int = DENSE_K,
    bm25_k: int = BM25_K,
    fuse_k: int = FUSE_K,
    rerank_k: int = RERANK_K,
) -> RetrievalResult:
    """Run one query. Cuts shrink to the corpus size. Failures use BM25 only."""
    started = time.perf_counter()
    chosen = OverlapReranker() if reranker is None else reranker
    documents = corpus.documents
    limit_dense = min(dense_k, len(documents))
    limit_bm25 = min(bm25_k, len(documents))
    available = (
        corpus.vectors_available if vector_store_available is None else vector_store_available
    )
    if available and (
        embedder.model_id != corpus.model_id or embedder.model_version != corpus.model_version
    ):
        available = False
    dense_ids: list[str] = []
    degraded = not available
    if available and limit_dense > 0:
        try:
            vectors = embedder.embed((query,))
        except (EmbedderDisabled, EmbedderError):
            degraded = True
            vectors = []
        if (
            not degraded
            and len(vectors) == 1
            and len(vectors[0]) == corpus.dimension
            and corpus.dimension > 0
        ):
            dense_ids = dense_search(vectors[0], documents, limit_dense)
        else:
            degraded = True
            dense_ids = []
    lexical = BM25Index(
        tuple(
            (document.source_record_id, document.body, getattr(document, "title", None))
            for document in documents
        )
    ).search(query, limit_bm25)
    bm25_ids = [hit.doc_id for hit in lexical]
    fuse_limit = min(fuse_k, len(documents))
    if degraded:
        fused_ids = bm25_ids[:fuse_limit]
    else:
        fused_ids = rrf(dense_ids, bm25_ids, fuse_limit)
    by_id = {document.source_record_id: document for document in documents}
    candidates: list[RerankCandidate] = []
    for rank, doc_id in enumerate(fused_ids, start=1):
        document = by_id.get(doc_id)
        if document is None:
            continue
        candidates.append(RerankCandidate(doc_id=doc_id, text=document.body, fused_rank=rank))
    reranked_hits = chosen.rerank(query, candidates, min(rerank_k, len(candidates)))
    reranked: list[str] = []
    for hit in reranked_hits:
        if hit.doc_id not in reranked:
            reranked.append(hit.doc_id)
    elapsed = (time.perf_counter() - started) * 1000.0
    return RetrievalResult(
        query=query,
        language=language,
        dense=tuple(dense_ids),
        bm25=tuple(bm25_ids),
        fused=tuple(fused_ids),
        reranked=tuple(reranked),
        degraded=degraded,
        latency_ms=elapsed,
    )


def evaluate_retrieval(
    documents: Sequence[Mapping[str, object]],
    queries: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    artifacts_dir: Path,
    database: Path,
    store: ObjectStore,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    vector_store_available: bool | None = None,
) -> RetrievalEval:
    """Index ``documents``, score ``queries``, and write the I20 artifacts.

    The default embedder is the local hashed baseline. This function does not
    call ``embedder_from_env`` and does not open a network connection.
    """
    checked = _run_id(run_id)
    directory = artifacts_dir / checked / "I20"
    chosen = HashedNgramEmbedder() if embedder is None else embedder
    indexed = build_index(
        documents,
        run_id=checked,
        artifacts_dir=artifacts_dir,
        database=database,
        store=store,
        embedder=chosen,
        origin="I20",
    )
    if indexed.status != TaskStatus.SUCCESS:
        return _emit(
            directory,
            checked,
            indexed.status,
            retrieval_degraded=True,
            high_confidence_blocked=True,
            mvp_threshold_met=False,
            ndcg={},
            recall={},
            p95_latency_ms=0.0,
            cost_usd=0.0,
            errors=indexed.errors or ("fatal_error: index was not built",),
            queries=[],
            index_version=indexed.index_version,
        )
    parsed = _parse_queries(queries)
    corpus = load_active_corpus(database)
    rows: list[dict[str, object]] = []
    latencies: list[float] = []
    per_language_ndcg: dict[str, list[float]] = {}
    per_language_recall: dict[str, list[float]] = {}
    any_degraded = indexed.degraded or not corpus.vectors_available
    for item in parsed:
        result = retrieve(
            item.text,
            corpus,
            embedder=chosen,
            language=item.language,
            reranker=reranker,
            vector_store_available=vector_store_available,
        )
        ranking = system_ranking(result)
        ndcg = ndcg_at_k(ranking, set(item.relevant), NDCG_AT)
        recall = recall_at_k(ranking, set(item.relevant), RECALL_AT)
        any_degraded = any_degraded or result.degraded
        latencies.append(result.latency_ms)
        per_language_ndcg.setdefault(item.language, []).append(ndcg)
        per_language_recall.setdefault(item.language, []).append(recall)
        rows.append(
            {
                "bm25": list(result.bm25),
                "degraded": result.degraded,
                "dense": list(result.dense),
                "fused": list(result.fused),
                "language": item.language,
                "latency_ms": result.latency_ms,
                "ndcg_at_10": ndcg,
                "query_id": item.query_id,
                "recall_at_20": recall,
                "relevant": list(item.relevant),
                "reranked": list(result.reranked),
                "system_ranking": ranking,
            }
        )
    ndcg_means = {language: _mean(values) for language, values in per_language_ndcg.items()}
    recall_means = {language: _mean(values) for language, values in per_language_recall.items()}
    blocked = _below_threshold(ndcg_means, recall_means)
    retrieval_degraded = any_degraded or blocked
    mvp_threshold_met = not retrieval_degraded and not blocked and bool(parsed)
    p95 = percentile(latencies, 95.0)
    return _emit(
        directory,
        checked,
        TaskStatus.SUCCESS,
        retrieval_degraded=retrieval_degraded,
        high_confidence_blocked=blocked,
        mvp_threshold_met=mvp_threshold_met,
        ndcg=ndcg_means,
        recall=recall_means,
        p95_latency_ms=p95,
        cost_usd=0.0,
        errors=(),
        queries=rows,
        index_version=indexed.index_version,
    )


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


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


@dataclass(frozen=True)
class _Query:
    query_id: str
    language: str
    text: str
    relevant: tuple[str, ...]


def _parse_queries(queries: Sequence[Mapping[str, object]]) -> list[_Query]:
    parsed: list[_Query] = []
    for query in queries:
        query_id = query.get("query_id")
        language = query.get("language")
        text = query.get("query")
        relevant = query.get("relevant")
        if (
            not isinstance(query_id, str)
            or not isinstance(language, str)
            or not isinstance(text, str)
        ):
            continue
        if not isinstance(relevant, list):
            continue
        ids = tuple(item for item in relevant if isinstance(item, str))
        parsed.append(_Query(query_id, language, text, ids))
    return parsed


def _emit(
    directory: Path,
    run_id: str,
    status: TaskStatus,
    *,
    retrieval_degraded: bool,
    high_confidence_blocked: bool,
    mvp_threshold_met: bool,
    ndcg: dict[str, float],
    recall: dict[str, float],
    p95_latency_ms: float,
    cost_usd: float,
    errors: tuple[str, ...],
    queries: list[dict[str, object]],
    index_version: str | None,
) -> RetrievalEval:
    languages: dict[str, object] = {}
    for language in sorted(set(ndcg) | set(recall)):
        count = sum(1 for query in queries if query.get("language") == language)
        languages[language] = {
            "ndcg_at_10": ndcg.get(language, 0.0),
            "queries": count,
            "recall_at_20": recall.get(language, 0.0),
        }
    manifest: dict[str, object] = {
        "cost_usd": cost_usd,
        "errors": list(errors),
        "eval_set": "hand_labelled_fixture",
        "high_confidence_blocked": high_confidence_blocked,
        "index_version": index_version,
        "languages": languages,
        "mvp_threshold_met": mvp_threshold_met,
        "p95_latency_ms": p95_latency_ms,
        "queries": queries,
        "retrieval_degraded": retrieval_degraded,
        "run_id": run_id,
        "status": status.value,
        "thresholds": {"ndcg_at_10": NDCG_THRESHOLD, "recall_at_20": RECALL_THRESHOLD},
        "ticket": "I20",
    }
    metrics: dict[str, object] = {
        "cost_usd": cost_usd,
        "eval_set": "hand_labelled_fixture",
        "high_confidence_blocked": high_confidence_blocked,
        "languages": languages,
        "mvp_threshold_met": mvp_threshold_met,
        "p95_latency_ms": p95_latency_ms,
        "retrieval_degraded": retrieval_degraded,
        "status": status.value,
        "third_party_calls": 0,
        "third_party_writes": 0,
    }
    _write_bytes(directory / "manifest.json", _pretty(manifest))
    _write_bytes(directory / "metrics.json", _pretty(metrics))
    log = {
        "event": "i20_retrieve",
        "high_confidence_blocked": high_confidence_blocked,
        "mvp_threshold_met": mvp_threshold_met,
        "retrieval_degraded": retrieval_degraded,
        "run_id": run_id,
        "status": status.value,
    }
    _write_bytes(directory / "log.jsonl", (json.dumps(log, sort_keys=True) + "\n").encode("utf-8"))
    return RetrievalEval(
        status=status,
        run_id=run_id,
        retrieval_degraded=retrieval_degraded,
        high_confidence_blocked=high_confidence_blocked,
        mvp_threshold_met=mvp_threshold_met,
        ndcg_at_10=dict(ndcg),
        recall_at_20=dict(recall),
        p95_latency_ms=p95_latency_ms,
        cost_usd=cost_usd,
        errors=errors,
    )


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(one * other for one, other in zip(left, right, strict=True))


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."} or any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _pretty(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
