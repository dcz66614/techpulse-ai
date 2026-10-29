"""Deterministic reranking.

``Reranker`` is the replaceable interface. ``OverlapReranker`` scores the
share of query tokens that appear in the candidate, interpolated with
fused rank to preserve high-confidence retrieval. Ties break by document id.
It does not call a network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from techpulse.index.bm25 import tokenize


@dataclass(frozen=True)
class RerankCandidate:
    doc_id: str
    text: str
    fused_rank: int


class Reranker(Protocol):
    def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        top_k: int = 10,
    ) -> list[RerankCandidate]: ...


class OverlapReranker:
    """Token-overlap baseline with fusion rank interpolation.

    A short candidate list is returned whole.
    """

    def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        top_k: int = 10,
    ) -> list[RerankCandidate]:
        if top_k <= 0 or not candidates:
            return []
        query_tokens = set(tokenize(query))
        scored: list[tuple[float, int, str, RerankCandidate]] = []
        for candidate in candidates:
            document_tokens = set(tokenize(candidate.text))
            if not query_tokens or not document_tokens:
                overlap = 0.0
            else:
                overlap = len(query_tokens & document_tokens) / len(query_tokens)
            score = (1.0 / (10.0 + candidate.fused_rank)) + overlap * 0.1
            scored.append((score, candidate.fused_rank, candidate.doc_id, candidate))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        limit = min(top_k, len(scored))
        return [item[3] for item in scored[:limit]]
