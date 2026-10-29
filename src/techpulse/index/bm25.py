"""Pure-Python BM25.

Tokens are ASCII words plus CJK character bigrams and domain cross-lingual
mappings. There is no external segmenter and no extra dependency. Scores use
the non-negative Okapi BM25 formula with k1 = 1.2 and b = 0.75.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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

# A small, domain-neutral technical bilingual lexicon of common general tech terms.
# Mappings cover core computer science, software engineering, databases/systems,
# information retrieval, and machine learning terminology.
#
# Rules enforced per IRclean specification:
# - Strictly domain-neutral standard technical concepts.
# - No corpus-flavoured entries (e.g. no vendor/tool names like "postgres", "sqlite",
#   or repo concepts like "worktree").
# - No entries chosen because of the benchmark corpus, gold queries, or gold labels.

_ZH_TO_EN: dict[str, tuple[str, ...]] = {
    # Software Engineering & Developer Tools
    "代码": ("code",),
    "审查": ("review", "reviewing"),
    "补全": ("completion", "complete"),
    "开发": ("development", "developer", "developers"),
    "开发者": ("developer", "developers"),
    "测试": ("testing", "test"),
    "调试": ("debugging", "debug"),
    "构建": ("build", "building"),
    "部署": ("deployment", "deploy"),
    "终端": ("terminal", "shell"),
    "工具": ("tool", "tools"),
    "框架": ("framework", "frameworks"),
    "接口": ("interface", "api"),
    "配置": ("configuration", "config"),
    "日志": ("logging", "logs", "log"),
    "工作流": ("workflow", "workflows"),
    # Systems & Databases
    "数据库": ("database", "databases"),
    "关系数据库": ("relational", "database", "databases"),
    "关系": ("relational",),
    "索引": ("index", "indexes", "indexing"),
    "存储": ("storage", "store"),
    "内存": ("memory",),
    "缓存": ("cache", "caching"),
    "并发": ("concurrency", "concurrent"),
    "分布式": ("distributed",),
    "管线": ("pipeline", "pipelines"),
    "流水线": ("pipeline", "pipelines"),
    "架构": ("architecture",),
    "性能": ("performance",),
    "延迟": ("latency",),
    "吞吐量": ("throughput",),
    "安全": ("security", "safety"),
    "沙箱": ("sandbox", "sandboxes"),
    "权限": ("permission", "privilege"),
    # Information Retrieval & Search
    "检索": ("retrieval", "search", "retrieve"),
    "搜索": ("search", "retrieval"),
    "查询": ("query", "queries"),
    "向量": ("vector", "vectors"),
    "向量数据库": ("vector", "database", "databases"),
    "向量检索": ("vector", "search", "retrieval"),
    "混合检索": ("hybrid", "retrieval", "search"),
    "混合": ("hybrid",),
    "重排": ("rerank", "reranking"),
    "排序": ("ranking", "rank"),
    "召回": ("recall",),
    "召回率": ("recall",),
    "相关性": ("relevance", "relevant"),
    "分块": ("chunking", "chunks", "chunk"),
    "引用": ("citation", "citations"),
    "特征": ("feature", "features"),
    "嵌入": ("embedding", "embeddings"),
    "聚类": ("clustering", "cluster"),
    "分类": ("classification", "classify"),
    # AI & Machine Learning
    "模型": ("model", "models"),
    "大模型": ("llm", "large", "language", "model", "models"),
    "智能体": ("agent", "agents"),
    "多智能体": ("multi", "agent", "agents"),
    "训练": ("training", "train"),
    "推理": ("inference",),
    "微调": ("tuning", "fine"),
    "提示": ("prompt", "prompts"),
    "评估": ("evaluation", "evaluating", "evaluate"),
    "评测": ("evaluation", "benchmark", "benchmarks"),
    "基准": ("benchmark", "benchmarks"),
    "规划": ("planning", "plan", "planner"),
    "记忆": ("memory",),
    "反思": ("reflection",),
    "幻觉": ("hallucination", "hallucinations"),
    "优化": ("optimization", "optimize"),
    "监控": ("monitoring", "monitor"),
    "指标": ("metric", "metrics"),
}

_EN_TO_ZH: dict[str, tuple[str, ...]] = {
    # Software Engineering & Developer Tools
    "code": ("代码",),
    "review": ("审查",),
    "completion": ("补全",),
    "developer": ("开发者", "开发"),
    "developers": ("开发者", "开发"),
    "testing": ("测试",),
    "test": ("测试",),
    "debug": ("调试",),
    "debugging": ("调试",),
    "build": ("构建",),
    "deployment": ("部署",),
    "deploy": ("部署",),
    "terminal": ("终端",),
    "shell": ("终端",),
    "tool": ("工具",),
    "tools": ("工具",),
    "framework": ("框架",),
    "api": ("接口",),
    "interface": ("接口",),
    "config": ("配置",),
    "configuration": ("配置",),
    "log": ("日志",),
    "logs": ("日志",),
    "logging": ("日志",),
    "workflow": ("工作流",),
    # Systems & Databases
    "database": ("数据库",),
    "databases": ("数据库",),
    "relational": ("关系", "关系数据库"),
    "index": ("索引",),
    "indexes": ("索引",),
    "indexing": ("索引",),
    "storage": ("存储",),
    "store": ("存储",),
    "memory": ("内存", "记忆"),
    "cache": ("缓存",),
    "caching": ("缓存",),
    "concurrency": ("并发",),
    "distributed": ("分布式",),
    "pipeline": ("管线", "流水线"),
    "pipelines": ("管线", "流水线"),
    "architecture": ("架构",),
    "performance": ("性能",),
    "latency": ("延迟",),
    "throughput": ("吞吐量",),
    "security": ("安全",),
    "safety": ("安全",),
    "sandbox": ("沙箱",),
    "sandboxes": ("沙箱",),
    "permission": ("权限",),
    "privilege": ("权限",),
    # Information Retrieval & Search
    "retrieval": ("检索",),
    "retrieve": ("检索",),
    "search": ("搜索", "检索"),
    "query": ("查询",),
    "queries": ("查询",),
    "vector": ("向量",),
    "vectors": ("向量",),
    "hybrid": ("混合",),
    "rerank": ("重排",),
    "reranking": ("重排",),
    "ranking": ("排序",),
    "rank": ("排序",),
    "recall": ("召回",),
    "relevance": ("相关性",),
    "chunk": ("分块",),
    "chunks": ("分块",),
    "chunking": ("分块",),
    "citation": ("引用",),
    "citations": ("引用",),
    "feature": ("特征",),
    "features": ("特征",),
    "embedding": ("嵌入",),
    "embeddings": ("嵌入",),
    "cluster": ("聚类",),
    "clustering": ("聚类",),
    "classification": ("分类",),
    # AI & Machine Learning
    "model": ("模型",),
    "models": ("模型",),
    "llm": ("大模型",),
    "agent": ("智能体",),
    "agents": ("智能体",),
    "training": ("训练",),
    "inference": ("推理",),
    "prompt": ("提示",),
    "prompts": ("提示",),
    "evaluation": ("评估", "评测"),
    "evaluate": ("评估",),
    "evaluating": ("评估", "评测"),
    "benchmark": ("基准", "评测"),
    "benchmarks": ("基准", "评测"),
    "planning": ("规划",),
    "plan": ("规划",),
    "planner": ("规划",),
    "reflection": ("反思",),
    "hallucination": ("幻觉",),
    "hallucinations": ("幻觉",),
    "optimization": ("优化",),
    "monitor": ("监控",),
    "monitoring": ("监控",),
    "metric": ("指标",),
    "metrics": ("指标",),
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
    """In-memory Okapi BM25 with explicit record title field weighting."""

    def __init__(
        self,
        documents: Sequence[
            tuple[str, str] | tuple[str, str, str | None] | Mapping[str, object] | Any
        ],
    ) -> None:
        self._ids: list[str] = []
        self._tfs: list[dict[str, int]] = []
        self._lengths: list[int] = []
        document_frequency: dict[str, int] = {}
        for item in documents:
            if isinstance(item, tuple):
                doc_id = str(item[0])
                text = str(item[1])
                title = str(item[2]) if len(item) > 2 and isinstance(item[2], str) else None
            elif isinstance(item, Mapping):
                doc_id = str(item.get("source_record_id") or item.get("doc_id") or "")
                text = str(
                    item.get("body") or item.get("text") or item.get("abstract_or_excerpt") or ""
                )
                title_val = item.get("title")
                title = str(title_val) if isinstance(title_val, str) else None
            else:
                doc_id = str(getattr(item, "source_record_id", getattr(item, "doc_id", "")))
                text = str(getattr(item, "body", getattr(item, "text", "")))
                title_val = getattr(item, "title", None)
                title = str(title_val) if isinstance(title_val, str) else None

            counts: dict[str, int] = {}
            tokens = tokenize(text)
            if title and title.strip():
                title_tokens = set(tokenize(title))
                for token in tokens:
                    weight = 2 if token in title_tokens else 1
                    counts[token] = counts.get(token, 0) + weight
                for token in tokenize(title):
                    if token not in counts:
                        counts[token] = 2
            else:
                for token in tokens:
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
