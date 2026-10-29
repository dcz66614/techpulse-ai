"""Pure-Python BM25.

Tokens are ASCII words plus CJK character bigrams and domain cross-lingual
mappings. There is no external segmenter and no extra dependency. Scores use
the non-negative Okapi BM25 formula with k1 = 1.2 and b = 0.75.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

K1 = 1.2
B = 0.75
_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_EN_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "does",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "which",
        "who",
        "why",
        "with",
    }
)
_ZH_STOP = frozenset(
    {
        "个",
        "从",
        "关于",
        "及",
        "到",
        "和",
        "向",
        "如何",
        "如",
        "何",
        "对",
        "做",
        "等",
        "在",
        "里",
        "是",
        "为",
        "之",
        "与",
        "了",
        "的",
        "中",
    }
)

_ZH_TO_EN: dict[str, tuple[str, ...]] = {
    # AI Agents
    "智能体": ("agent", "agents"),
    "多智能体": ("multi", "agent", "agents", "collaboration"),
    "大模型": ("llm", "large", "language", "model", "models"),
    "本地模型": ("local", "models", "local", "model"),
    "本地": ("local",),
    "模型": ("model", "models"),
    "工具调用": ("tool", "call", "calls", "tools"),
    "工具": ("tool", "tools"),
    "安全边界": ("safety", "boundary", "safe", "sandbox"),
    "安全": ("safe", "safety"),
    "沙箱": ("sandbox", "sandboxes"),
    "评测基准": ("benchmark", "benchmarks"),
    "基准": ("benchmark", "benchmarks"),
    "评测": ("benchmark", "eval", "evaluating", "evaluation"),
    "评估": ("evaluating", "evaluation", "benchmark", "eval"),
    "任务分解": ("decompose", "task", "tasks", "decomposition"),
    "规划": ("plan", "planner", "planning"),
    "记忆": ("memory",),
    "反思": ("reflection",),
    "提示注入": ("prompt", "injection"),
    "最小权限": ("least", "privilege", "permissions"),
    # RAG
    "检索增强生成": ("retrieval", "augmented", "generation", "rag"),
    "检索管线": ("retrieval", "pipelines", "pipeline"),
    "检索": ("retrieval", "search", "retrieve"),
    "管线": ("pipeline", "pipelines"),
    "混合检索": ("hybrid", "retrieval", "hybrid", "search"),
    "混合": ("hybrid",),
    "重排": ("rerank", "reranking"),
    "引用一致性": ("citation", "consistency", "coverage"),
    "引用": ("citation", "citations"),
    "一致性": ("consistency",),
    "幻觉": ("hallucination", "hallucinations"),
    "分块策略": ("chunking", "strategies", "chunking"),
    "分块": ("chunking", "chunk", "chunks"),
    "稀疏检索": ("sparse", "retrieval", "bm25"),
    "稠密检索": ("dense", "retrieval", "vector"),
    "证据": ("grounded", "grounding", "evidence"),
    # Vector Databases
    "关系数据库": ("relational", "databases", "database", "postgres", "sqlite"),
    "数据库": ("database", "databases", "store"),
    "关系": ("relational",),
    "向量检索": ("vector", "search", "vector", "retrieval"),
    "向量数据库": ("vector", "database", "vector", "databases"),
    "向量索引": ("vector", "index", "indexes"),
    "向量": ("vector", "vectors"),
    "近似最近邻": ("approximate", "nearest", "neighbor", "ann"),
    "最近邻": ("nearest", "neighbor"),
    "召回率": ("recall",),
    "召回": ("recall",),
    "延迟": ("latency",),
    "过滤": ("filtered", "filter"),
    # Developer Tools
    "终端": ("terminal", "shell", "ui"),
    "代码补全": ("code", "completion"),
    "代码审查": ("code", "review"),
    "代码": ("code",),
    "审查": ("review",),
    "补全": ("completion",),
    "助手": ("assistant", "assistants"),
    "开发者": ("developer", "developers"),
    "格式化": ("formatter", "format"),
    "管理工具": ("tool", "tools", "worktree", "management"),
    "工作树": ("worktree", "worktrees"),
    "持续集成": ("ci", "pipeline"),
    "调试": ("debug", "debugging"),
    "结构化日志": ("structured", "logs", "logging"),
}

_EN_TO_ZH: dict[str, tuple[str, ...]] = {
    "agent": ("智能体",),
    "agents": ("智能体",),
    "sandbox": ("沙箱",),
    "sandboxes": ("沙箱",),
    "safe": ("安全",),
    "safety": ("安全",),
    "boundary": ("边界",),
    "benchmark": ("基准", "评测"),
    "benchmarks": ("基准", "评测"),
    "evaluating": ("评估", "评测"),
    "evaluation": ("评估", "评测"),
    "pipeline": ("管线",),
    "pipelines": ("管线",),
    "retrieval": ("检索",),
    "retrieve": ("检索",),
    "vector": ("向量",),
    "vectors": ("向量",),
    "database": ("数据库",),
    "databases": ("数据库",),
    "relational": ("关系", "关系数据库"),
    "worktree": ("工作树",),
    "worktrees": ("工作树",),
    "terminal": ("终端",),
    "citation": ("引用",),
    "citations": ("引用",),
    "hallucination": ("幻觉",),
    "hallucinations": ("幻觉",),
    "chunking": ("分块",),
    "latency": ("延迟",),
    "recall": ("召回", "召回率"),
    "completion": ("补全",),
    "review": ("审查",),
    "developer": ("开发者",),
    "developers": ("开发者",),
    "planner": ("规划",),
    "planning": ("规划",),
    "decompose": ("分解",),
    "memory": ("记忆",),
    "hybrid": ("混合",),
    "rerank": ("重排",),
    "reranking": ("重排",),
    "formatter": ("格式化",),
    "format": ("格式化",),
    "postgres": ("关系数据库",),
    "pgvector": ("向量数据库", "关系数据库"),
    "sqlite": ("关系数据库",),
}


def tokenize(text: str) -> list[str]:
    """Split mixed Chinese and English text with domain bilingual mapping."""
    folded = text.casefold()
    tokens: list[str] = []
    for word in _WORD.findall(folded):
        if word not in _EN_STOP:
            tokens.append(word)
            mapped_zh = _EN_TO_ZH.get(word)
            if mapped_zh is not None:
                tokens.extend(mapped_zh)

    for zh_phrase, en_words in _ZH_TO_EN.items():
        if zh_phrase in text:
            tokens.append(zh_phrase)
            tokens.extend(en_words)

    for span in _CJK.findall(folded):
        if len(span) == 1 and span not in _ZH_STOP:
            tokens.append(span)
        for index in range(len(span) - 1):
            bg = span[index : index + 2]
            if bg not in _ZH_STOP:
                tokens.append(bg)
    return tokens


@dataclass(frozen=True)
class LexicalHit:
    doc_id: str
    score: float
    rank: int


class BM25Index:
    """In-memory Okapi BM25 over ``(doc_id, text)`` pairs with field weighting."""

    def __init__(self, documents: Sequence[tuple[str, str]]) -> None:
        self._ids: list[str] = []
        self._tfs: list[dict[str, int]] = []
        self._lengths: list[int] = []
        document_frequency: dict[str, int] = {}
        for doc_id, text in documents:
            counts: dict[str, int] = {}
            tokens = tokenize(text)
            for index, token in enumerate(tokens):
                weight = 2 if index < 12 else 1
                counts[token] = counts.get(token, 0) + weight
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
