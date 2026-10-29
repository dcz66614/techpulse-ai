"""GQ extension gold set: shape, provenance, and sealed-split safety tests.

The extension queries file (``data/gold/retrieval/queries_ext.jsonl``) is a
single-annotator labelling pass with ``crosscheck: pending``. These tests
verify the file is well-formed, grounded in the real corpus, that the
dev2/heldout split assignment is the documented hash rule, that the splits
are disjoint from each other and from the shipped I30 set, and that the
reporting code path never loads or runs the sealed TEST split.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

from techpulse.evaluation.retrieval_metrics import (
    QueryJudgement,
    load_gold_queries,
)
from techpulse.evaluation.split_eval import (
    evaluate_by_split,
    format_report,
    load_reportable_queries,
    report_splits,
)

ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_DIR = ROOT / "data" / "gold" / "retrieval"
CORPUS_PATH = RETRIEVAL_DIR / "corpus.jsonl"
ORIGINAL_QUERIES_PATH = RETRIEVAL_DIR / "queries.jsonl"
EXT_QUERIES_PATH = RETRIEVAL_DIR / "queries_ext.jsonl"

TOPICS = {"ai_agents", "rag", "vector_databases", "developer_tools"}
LABELLER = "glm-5.3-flash-free (opencode)"


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_original_queries_file_is_untouched() -> None:
    queries = load_gold_queries(ORIGINAL_QUERIES_PATH)
    assert len(_jsonl(ORIGINAL_QUERIES_PATH)) == 40
    assert Counter(query.split for query in queries) == {"train": 24, "dev": 8, "test": 8}


def test_extension_file_is_well_formed() -> None:
    rows = _jsonl(EXT_QUERIES_PATH)
    assert len(rows) == 24
    assert Counter(str(row["language"]) for row in rows) == {"en": 12, "zh": 12}
    assert Counter(str(row["topic"]) for row in rows) == {
        "ai_agents": 6,
        "rag": 6,
        "vector_databases": 6,
        "developer_tools": 6,
    }
    ids = [str(row["query_id"]) for row in rows]
    assert len(set(ids)) == len(ids)
    for row in rows:
        assert row["split"] in {"dev2", "heldout"}
        assert row["labeller"] == LABELLER
        assert row["crosscheck"] == "pending"
        assert row["language"] in {"en", "zh"}
        assert row["topic"] in TOPICS
        assert isinstance(row["query"], str) and row["query"].strip()
        assert isinstance(row["justification"], str) and len(row["justification"]) > 20
        relevance = row["relevance"]
        assert isinstance(relevance, dict) and relevance
        grades = [int(grade) for grade in relevance.values()]
        assert all(0 <= grade <= 2 for grade in grades)
        assert any(grade > 0 for grade in grades)
    # The loader accepts every row: the file is valid gold JSONL.
    assert len(load_gold_queries(EXT_QUERIES_PATH)) == 24


def test_every_labelled_doc_id_exists_in_corpus() -> None:
    corpus_ids = {str(row["doc_id"]) for row in _jsonl(CORPUS_PATH)}
    assert len(corpus_ids) == 32
    for row in _jsonl(EXT_QUERIES_PATH):
        relevance = row["relevance"]
        assert isinstance(relevance, dict)
        assert set(relevance) <= corpus_ids


def test_split_assignment_follows_documented_hash_rule() -> None:
    for row in _jsonl(EXT_QUERIES_PATH):
        digest = hashlib.sha256(str(row["query"]).encode("utf-8")).hexdigest()
        expected = "dev2" if int(digest, 16) % 2 == 0 else "heldout"
        assert row["split"] == expected


def test_split_assignment_is_balanced() -> None:
    counts = Counter(str(row["split"]) for row in _jsonl(EXT_QUERIES_PATH))
    assert counts == {"dev2": 13, "heldout": 11}


def test_extension_splits_are_disjoint_from_each_other_and_from_original() -> None:
    by_split: dict[str, set[str]] = {"dev2": set(), "heldout": set()}
    for row in _jsonl(EXT_QUERIES_PATH):
        by_split[str(row["split"])].add(str(row["query_id"]))
        by_split[str(row["split"])].add(str(row["query"]))
    assert by_split["dev2"].isdisjoint(by_split["heldout"])

    original = load_gold_queries(ORIGINAL_QUERIES_PATH)
    original_ids_and_strings = {query.query_id for query in original} | {
        query.query for query in original
    }
    extension_ids_and_strings = {str(row["query_id"]) for row in _jsonl(EXT_QUERIES_PATH)} | {
        str(row["query"]) for row in _jsonl(EXT_QUERIES_PATH)
    }
    assert extension_ids_and_strings.isdisjoint(original_ids_and_strings)


def test_evaluate_by_split_rejects_sealed_test_split() -> None:
    sealed = QueryJudgement("q-test", "sealed query", "en", "rag", "test", {"d1": 2})
    with pytest.raises(ValueError, match="test"):
        evaluate_by_split(lambda _query: ["d1"], [sealed])


def test_load_reportable_queries_never_parses_sealed_test_rows() -> None:
    # The sealed test row has a relevance field that is NOT a mapping; if the
    # loader ever tried to validate it, judgement_from_row would raise
    # "relevance must be an object" and this test would fail on the regex.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "queries.jsonl"
        rows = (
            {
                "query_id": "q-sealed-test",
                "query": "sealed",
                "language": "en",
                "topic": "rag",
                "split": "test",
                "relevance": "not-a-mapping-<sealed>",
            },
            {
                "query_id": "q-train",
                "query": "train row",
                "language": "en",
                "topic": "rag",
                "split": "train",
                "relevance": {"d1": 1},
            },
            {
                "query_id": "q-dev",
                "query": "dev row",
                "language": "en",
                "topic": "rag",
                "split": "dev",
                "relevance": {"d1": 2},
            },
            {
                "query_id": "q-dev2",
                "query": "dev2 row",
                "language": "zh",
                "topic": "rag",
                "split": "dev2",
                "relevance": {"d1": 1},
            },
            {
                "query_id": "q-heldout",
                "query": "heldout row",
                "language": "en",
                "topic": "rag",
                "split": "heldout",
                "relevance": {"d1": 2},
            },
            {
                "query_id": "q-malformed-allowed-split",
                "query": "malformed",
                "language": "en",
                "topic": "rag",
                "split": "dev",
                "relevance": "malformed-not-a-mapping",
            },
        )
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="relevance must be an object") as excinfo:
            load_reportable_queries(path)
        # Line 6 is the malformed dev row. If the sealed test row (line 1)
        # had been parsed, its own relevance error would surface with line 1.
        assert str(excinfo.value).endswith("queries.jsonl:6: relevance must be an object")
        # Without the malformed trailing row, only dev/dev2/heldout survive.
        good_rows = (rows[0], rows[2], rows[3], rows[4])
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in good_rows) + "\n",
            encoding="utf-8",
        )
        loaded = load_reportable_queries(path)
        assert {query.split for query in loaded} == {"dev", "dev2", "heldout"}
        assert {query.query_id for query in loaded} == {"q-dev", "q-dev2", "q-heldout"}


def test_evaluate_by_split_groups_only_reportable_splits() -> None:
    gold = (
        QueryJudgement("a", "first", "en", "rag", "dev2", {"d1": 2}),
        QueryJudgement("b", "second", "zh", "rag", "heldout", {"d1": 1}),
    )

    def retriever(_query: str) -> Sequence[str]:
        return ("d1",)

    results = evaluate_by_split(retriever, gold)
    assert set(results) == {"dev2", "heldout"}
    assert results["dev2"].by_language["en"].ndcg_at_10 == pytest.approx(1.0)
    assert results["heldout"].by_language["zh"].queries == 1


def test_report_splits_runs_the_reportable_splits_only() -> None:
    results = report_splits(ROOT)
    assert set(results) == {"dev", "dev2", "heldout"}
    assert results["dev"].overall.queries == 8
    assert results["dev2"].overall.queries + results["heldout"].overall.queries == 24
    assert all(
        0.0 <= metric <= 1.0
        for value in results.values()
        for metric in (value.overall.ndcg_at_10, value.overall.recall_at_20)
    )
    text = format_report(results)
    assert "dev" in text and "dev2" in text and "heldout" in text
