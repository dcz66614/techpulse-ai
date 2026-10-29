"""Per-split retrieval reporting for the GQ gold-set extension.

Read-only entry point: it builds the existing hybrid retriever over the
I30 corpus in memory and reports nDCG@10 / Recall@20 per split for the
reportable evaluation splits ``dev``, ``dev2``, and ``heldout``. It never
runs, loads, or validates the sealed ``test`` split: rows with a sealed
split value are skipped during loading, before any label is parsed, and
:meth:`evaluate_by_split` rejects them if one is ever passed in.

Thresholds from I30 section 10 are intentionally NOT applied here. The
extension splits are not used to tune anything; this module only reports
numbers.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from techpulse.evaluation.retrieval_metrics import (
    EvaluationResult,
    QueryJudgement,
    evaluate,
    judgement_from_row,
)
from techpulse.index.embed import ActiveCorpus, CorpusDocument, HashedNgramEmbedder
from techpulse.index.hybrid import retrieve, system_ranking

REPORTABLE_SPLITS: frozenset[str] = frozenset({"dev", "dev2", "heldout"})
SEALED_TEST_SPLIT = "test"

CORPUS_RELPATH = Path("data") / "gold" / "retrieval" / "corpus.jsonl"
BASE_QUERIES_RELPATH = Path("data") / "gold" / "retrieval" / "queries.jsonl"
EXT_QUERIES_RELPATH = Path("data") / "gold" / "retrieval" / "queries_ext.jsonl"


@dataclass(frozen=True)
class SplitReport:
    """Metrics for one named split."""

    split: str
    result: EvaluationResult


def load_reportable_queries(*paths: Path) -> tuple[QueryJudgement, ...]:
    """Load gold queries while skipping sealed or non-reportable split rows.

    Rows whose ``split`` is not one of :data:`REPORTABLE_SPLITS` (including
    the sealed test split) are filtered out from the raw JSON before labels
    are validated, so their queries and relevance grades are never loaded.
    """
    judgements: list[QueryJudgement] = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            split_value = raw.get("split")
            if not isinstance(split_value, str) or split_value not in REPORTABLE_SPLITS:
                continue
            judgements.append(judgement_from_row(raw, path, line_number))
    return tuple(judgements)


def evaluate_by_split(
    retriever: Callable[[str], Sequence[str]],
    gold: Iterable[QueryJudgement],
) -> dict[str, EvaluationResult]:
    """Group ``gold`` by split and evaluate, refusing any other split value.

    Raises ``ValueError`` if a judgement carries a split outside
    :data:`REPORTABLE_SPLITS` (for example, the sealed test split).
    """
    grouped: dict[str, list[QueryJudgement]] = {}
    for judgement in gold:
        if judgement.split not in REPORTABLE_SPLITS:
            raise ValueError(
                f"sealed or unreportable split {judgement.split!r} must never be loaded or run"
            )
        grouped.setdefault(judgement.split, []).append(judgement)
    ordered = sorted(REPORTABLE_SPLITS & set(grouped))
    return {split: evaluate(retriever, grouped[split]) for split in ordered}


def hybrid_retriever_for_corpus(root: Path) -> Callable[[str], Sequence[str]]:
    """Build the existing in-memory hybrid retriever used by the V90 gate.

    Same construction as ``go_nogo._measure_i30_dev``: hashed n-gram
    embedder plus ``techpulse.index.hybrid.retrieve`` with the system
    ranking. Nothing is written; no index artifacts are created.
    """
    corpus = _active_corpus(root)
    return lambda query: system_ranking(retrieve(query, corpus, embedder=HashedNgramEmbedder()))


def report_splits(root: Path) -> dict[str, EvaluationResult]:
    """Report metrics per reportable split for the existing hybrid retriever."""
    retriever = hybrid_retriever_for_corpus(root)
    gold = load_reportable_queries(
        root / BASE_QUERIES_RELPATH,
        root / EXT_QUERIES_RELPATH,
    )
    return evaluate_by_split(retriever, gold)


def format_report(results: Mapping[str, EvaluationResult]) -> str:
    """Render a text table of per-split metrics."""
    lines: list[str] = [
        "split   queries  nDCG@10  Recall@20   en(ndcg/recall)      zh(ndcg/recall)"
    ]
    for split in sorted(results):
        result = results[split]
        en = result.by_language.get("en")
        zh = result.by_language.get("zh")
        en_text = "n/a" if en is None else f"{en.ndcg_at_10:.4f}/{en.recall_at_20:.4f}"
        zh_text = "n/a" if zh is None else f"{zh.ndcg_at_10:.4f}/{zh.recall_at_20:.4f}"
        lines.append(
            f"{split:<6}  {result.overall.queries:>7}  "
            f"{result.overall.ndcg_at_10:.4f}   {result.overall.recall_at_20:.4f}   "
            f"{en_text:<19}  {zh_text}"
        )
    return "\n".join(lines)


def _active_corpus(root: Path) -> ActiveCorpus:
    corpus_path = root / CORPUS_RELPATH
    docs = [
        json.loads(line)
        for line in corpus_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not docs:
        raise ValueError(f"{corpus_path}: corpus is empty")
    embedder = HashedNgramEmbedder()
    bodies = [f"{d.get('title', '')} {d.get('text', '')}" for d in docs]
    vectors = embedder.embed(bodies)
    corpus_docs: list[CorpusDocument] = []
    for d, b, v in zip(docs, bodies, vectors, strict=True):
        corpus_docs.append(
            CorpusDocument(
                vector_id=d["doc_id"],
                chunk_id=d["doc_id"],
                source_id=d.get("source", ""),
                source_record_id=d["doc_id"],
                language=d.get("language", "en"),
                license_status="fulltext_allowed",
                body=b,
                content_sha256="0" * 64,
                embedding=tuple(v),
                title=d.get("title"),
            )
        )
    return ActiveCorpus(
        text_version="1.0",
        published_version="1.0",
        model_id=embedder.model_id,
        model_version=embedder.model_version,
        dimension=embedder.dimension,
        documents=tuple(corpus_docs),
        vectors_available=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Print the per-split report for this repository."""
    root = Path(argv[0] if argv else ".").resolve()
    print(format_report(report_splits(root)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
