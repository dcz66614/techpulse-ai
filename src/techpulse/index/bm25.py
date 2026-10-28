"""Pure-Python BM25.

Tokens are ASCII words plus CJK character unigrams and bigrams. There is no
segmenter and no extra dependency. Scores use the non-negative Okapi BM25
formula with k1 = 1.5 and b = 0.75.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

K1 = 1.5
B = 0.75
_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_STOP = frozenset({"a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "with"})


def tokenize(text: str) -> list[str]:
    """Split mixed Chinese and English text without a dictionary."""
    folded = text.casefold()
    tokens: list[str] = []
    for word in _WORD.findall(folded):
        if word not in _STOP:
            tokens.append(word)
    for span in _CJK.findall(folded):
        tokens.extend(span)
        tokens.extend(span[index : index + 2] for index in range(len(span) - 1))
    return tokens


@dataclass(frozen=True)
class LexicalHit:
    doc_id: str
    score: float
    rank: int


class BM25Index:
    """In-memory Okapi BM25 over ``(doc_id, text)`` pairs."""

    def __init__(self, documents: Sequence[tuple[str, str]]) -> None:
        self._ids: list[str] = []
        self._tfs: list[dict[str, int]] = []
        self._lengths: list[int] = []
        document_frequency: dict[str, int] = {}
        for doc_id, text in documents:
            counts: dict[str, int] = {}
            for token in tokenize(text):
                counts[token] = counts.get(token, 0) + 1
            self._ids.append(doc_id)
            self._tfs.append(counts)
            length = sum(counts.values())
            self._lengths.append(length)
            for token in counts:
                document_frequency[token] = document_frequency.get(token, 0) + 1
        self._df = document_frequency
        total = len(self._lengths)
        self._avgdl = 0.0 if total == 0 else sum(self._lengths) / total
        self._n = total

    def search(self, query: str, top_k: int = 100) -> list[LexicalHit]:
        """Return up to ``top_k`` hits. A short corpus returns fewer rows."""
        if top_k <= 0 or self._n == 0 or self._avgdl == 0.0:
            return []
        query_counts: dict[str, int] = {}
        for token in tokenize(query):
            query_counts[token] = query_counts.get(token, 0) + 1
        if not query_counts:
            return []
        scored: list[tuple[float, str]] = []
        for index, doc_id in enumerate(self._ids):
            score = self._score(index, query_counts)
            if score > 0.0:
                scored.append((score, doc_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        limit = min(top_k, len(scored))
        return [
            LexicalHit(doc_id=doc_id, score=score, rank=rank)
            for rank, (score, doc_id) in enumerate(scored[:limit], start=1)
        ]

    def _score(self, index: int, query_counts: dict[str, int]) -> float:
        total = 0.0
        counts = self._tfs[index]
        length = self._lengths[index]
        normalizer = K1 * (1.0 - B + B * length / self._avgdl)
        for term, query_tf in query_counts.items():
            term_frequency = counts.get(term, 0)
            if term_frequency == 0:
                continue
            document_frequency = self._df.get(term, 0)
            idf = math.log(1.0 + (self._n - document_frequency + 0.5) / (document_frequency + 0.5))
            numerator = term_frequency * (K1 + 1.0)
            total += idf * (numerator / (term_frequency + normalizer)) * query_tf
        return total
