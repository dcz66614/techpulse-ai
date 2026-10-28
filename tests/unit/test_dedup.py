from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from techpulse.normalize.dedup import (
    deduplicate_records,
    normalize_arxiv,
    normalize_doi,
    normalize_repo,
    pairwise_metrics,
    run_dedup,
)


def test_gold_pairwise_precision_and_recall() -> None:
    path = Path(__file__).parents[2] / "data" / "gold" / "dedup_gold.jsonl"
    gold = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    result = deduplicate_records(gold)
    labels = {f"{row['source_id']}:{row['source_record_id']}": row["cluster"] for row in gold}
    metrics = pairwise_metrics(result.rows, labels)
    print(
        f"D30 gold precision={metrics.precision:.4f} recall={metrics.recall:.4f} "
        f"TP={metrics.true_positives} FP={metrics.false_positives} FN={metrics.false_negatives}"
    )
    assert metrics.precision >= 0.98
    assert metrics.recall >= 0.95


def test_identifier_priority_and_reversible_mapping() -> None:
    rows = [
        {
            "source_id": "crossref",
            "source_record_id": "1",
            "title": "A DOI paper",
            "doi": "DOI:10.1000/A",
        },
        {
            "source_id": "arxiv",
            "source_record_id": "2401.00001v3",
            "title": "An arXiv paper",
        },
        {
            "source_id": "github",
            "source_record_id": "83294721",
            "title": "A repository",
            "repo_id": "https://github.com/Org/Repo.git",
        },
    ]
    result = deduplicate_records(rows)
    assert result.rows[0]["canonical_id"] == "doi:10.1000/a"
    assert result.rows[1]["canonical_id"] == "arxiv:2401.00001"
    assert result.rows[2]["canonical_id"] == "repo:id:83294721"
    assert result.rows[0]["source_record_ids"] == ["crossref:1"]
    assert result.rows[0]["source_record_id"] == "1"
    assert all(row["source_record_ids"] for row in result.rows)


def test_shared_identifier_merges_and_keeps_every_original_row() -> None:
    rows = [
        {
            "source_id": "crossref",
            "source_record_id": "1",
            "title": "Efficient Retrieval",
            "doi": "https://doi.org/10.1000/A",
        },
        {
            "source_id": "openalex",
            "source_record_id": "W1",
            "title": "Efficient Retrieval",
            "doi": "10.1000/a",
        },
    ]
    result = deduplicate_records(rows)
    assert {row["canonical_id"] for row in result.rows} == {"doi:10.1000/a"}
    assert result.rows[0]["source_record_ids"] == ["crossref:1", "openalex:W1"]
    assert result.rows[0]["source_record_id"] == "1"
    assert result.rows[1]["source_record_id"] == "W1"


def test_conflicting_identifiers_are_unresolved_not_merged() -> None:
    rows = _records(("crossref", "1", "10.1000/a"), ("crossref", "2", "10.1000/b"))
    rows[0]["title"] = rows[1]["title"] = "Same title"
    result = deduplicate_records(rows)
    assert result.rows[0]["canonical_id"] != result.rows[1]["canonical_id"]
    assert all(row["unresolved"] for row in result.rows)
    assert result.candidates[0].reason == "unresolved"
    assert result.candidates[0].score == 100.0


def test_title_fingerprint_merges_only_without_conflicting_external_ids() -> None:
    rows = [
        {"source_id": "rss", "source_record_id": "1", "title": "Graph Search for  Retrieval!"},
        {"source_id": "hn", "source_record_id": "2", "title": "graph search for retrieval"},
        {
            "source_id": "arxiv",
            "source_record_id": "2401.00003",
            "title": "Sparse Attention for Language Models",
        },
        {
            "source_id": "crossref",
            "source_record_id": "10.1000/x",
            "title": "Sparse Attention for Language Models",
            "doi": "10.1000/x",
        },
    ]
    result = deduplicate_records(rows)
    assert result.rows[0]["canonical_id"] == result.rows[1]["canonical_id"]
    assert result.rows[2]["canonical_id"] != result.rows[3]["canonical_id"]
    assert result.rows[0]["canonical_id"] != result.rows[2]["canonical_id"]
    assert result.rows[2]["unresolved"] and result.rows[3]["unresolved"]


def test_source_record_id_is_the_last_identifier() -> None:
    rows = [
        {"source_id": "rss", "source_record_id": "r1", "title": "One"},
        {"source_id": "rss", "source_record_id": "r1", "title": "One updated"},
        {"source_id": "hn", "source_record_id": "r1", "title": "Two"},
    ]
    result = deduplicate_records(rows)
    assert result.rows[0]["canonical_id"] == result.rows[1]["canonical_id"]
    assert result.rows[2]["canonical_id"] != result.rows[0]["canonical_id"]


def test_local_object_store_artifacts(tmp_path: Path) -> None:
    result = run_dedup(
        _records(("crossref", "1", "10.1000/a")), run_id="test", artifacts_dir=tmp_path
    )
    assert len(result.rows) == 1
    manifest = json.loads((tmp_path / "test/D30/manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((tmp_path / "test/D30/metrics.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert manifest["output_format"] == "jsonl"
    assert metrics["input_rows"] == 1
    assert metrics["third_party_writes"] == 0
    assert (tmp_path / "test/D30/dedup.jsonl").is_file()


def test_normalizers_reject_invalid_ids() -> None:
    assert normalize_doi("not-a-doi") is None
    assert normalize_doi("doi: 10.1000/ABC.") == "10.1000/abc"
    assert normalize_arxiv("not-an-arxiv-id") is None
    assert normalize_arxiv("arXiv:2401.12345v2") == "2401.12345"
    assert normalize_arxiv("https://arxiv.org/pdf/2401.00001v2.pdf") == "2401.00001"
    assert normalize_arxiv("hep-th/9901001v2") == "hep-th/9901001"
    assert normalize_repo("https://example.com/a/b") is None
    assert normalize_repo(83294721) == "id:83294721"
    assert normalize_repo("github:Org/Repo.git") == "org/repo"


def _records(*items: tuple[str, str, str]) -> list[dict[str, Any]]:
    result = []
    for source, record_id, identifier in items:
        row: dict[str, Any] = {
            "source_id": source,
            "source_record_id": record_id,
            "title": "Fixture",
        }
        key = "doi" if source == "crossref" else "arxiv_id" if source == "arxiv" else "repo_id"
        row[key] = identifier
        result.append(row)
    return result
