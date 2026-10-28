from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from techpulse.normalize.entities import resolve_entities, run_entities

PAPER_ID = "doi:10.1000/a"


def test_paper_repository_company_relations_have_provenance() -> None:
    rows = [_paper(), _repo()]
    result = resolve_entities(rows)
    entities = {entity["canonical_id"]: entity for entity in result.entities}
    assert entities[PAPER_ID]["entity_type"] == "paper"
    assert entities["repo:id:83294721"]["entity_type"] == "repository"
    assert entities["company:id:acme-example"]["entity_type"] == "company"
    assert entities["company:id:acme-example"]["aliases"] == ["Acme Labs"]

    relations = {
        (row["subject_id"], row["predicate"], row["object_id"]) for row in result.relations
    }
    assert (PAPER_ID, "references_repository", "repo:id:83294721") in relations
    assert (PAPER_ID, "affiliated_with", "company:id:acme-example") in relations
    assert (PAPER_ID, "about_topic", "topic:information-retrieval") in relations
    assert ("repo:id:83294721", "owned_by", "company:id:acme-example") in relations
    assert ("repo:id:83294721", "references_paper", "paper:arxiv:2401.12345") in relations

    for relation in result.relations:
        assert relation["source_record_id"]
        assert relation["source_record_ids"]
        assert relation["confidence"] in {"high", "medium", "low"}
        assert relation["status"] in {"confirmed", "unresolved"}


def test_repository_rename_keeps_the_stable_identity_and_aliases() -> None:
    rows = [
        _repo(source_id="github", record_id="83294721", name="acme/atlas"),
        _repo(source_id="hn", record_id="9001", name="acme/atlas-ai"),
    ]
    result = resolve_entities(rows)
    repositories = [entity for entity in result.entities if entity["entity_type"] == "repository"]
    assert len(repositories) == 1
    repository = repositories[0]
    assert repository["canonical_id"] == "repo:id:83294721"
    assert set(repository["aliases"]) == {"acme/atlas", "acme/atlas-ai"}
    assert repository["source_record_ids"] == ["github:83294721", "hn:9001"]


def test_same_name_companies_with_different_domains_do_not_merge() -> None:
    rows = [
        _paper(record_id="W1", paper_id="doi:10.1000/a", company_domain="acme.example"),
        _paper(record_id="W2", paper_id="doi:10.1000/b", company_domain="acme.other"),
    ]
    result = resolve_entities(rows)
    companies = [entity for entity in result.entities if entity["entity_type"] == "company"]
    assert {entity["canonical_id"] for entity in companies} == {
        "company:id:acme-example",
        "company:id:acme-other",
    }
    assert all(entity["display_name"] == "Acme Labs" for entity in companies)


def test_ownership_conflict_is_unresolved_not_overwritten() -> None:
    rows = [
        _repo(company_name="Acme Labs", company_domain="acme.example"),
        _repo(
            source_id="hn",
            record_id="9001",
            company_name="Other Corp",
            company_domain="other.example",
        ),
    ]
    result = resolve_entities(rows)
    ownership = [relation for relation in result.relations if relation["predicate"] == "owned_by"]
    assert len(ownership) == 2
    assert {relation["object_id"] for relation in ownership} == {
        "company:id:acme-example",
        "company:id:other-example",
    }
    assert all(relation["status"] == "unresolved" for relation in ownership)
    assert all(relation["conflict"] for relation in ownership)
    assert all(relation["confidence"] == "low" for relation in ownership)
    assert set(result.conflicts) == {relation["relation_id"] for relation in ownership}


def test_name_only_company_and_unknown_topic_stay_unresolved() -> None:
    rows = [
        _paper(
            company={"name": "Unknown Labs"},
            topics=["information retrieval", "a topic not in the dictionary"],
        )
    ]
    result = resolve_entities(rows)
    entities = {entity["canonical_id"]: entity for entity in result.entities}
    company = next(
        entity
        for entity in result.entities
        if entity["entity_type"] == "company" and entity["display_name"] == "Unknown Labs"
    )
    topic = entities["topic:name:a-topic-not-in-the-dictionary"]
    assert company["resolution_status"] == "unresolved"
    assert topic["resolution_status"] == "unresolved"
    low = [
        relation
        for relation in result.relations
        if relation["object_id"] in {company["canonical_id"], topic["canonical_id"]}
    ]
    assert low
    assert all(relation["status"] == "unresolved" for relation in low)
    assert all(relation["confidence"] == "low" for relation in low)
    assert company["canonical_id"] in result.unresolved
    assert topic["canonical_id"] in result.unresolved


def test_same_name_without_ids_are_not_merged() -> None:
    rows = [
        _paper(record_id="W1", paper_id="doi:10.1000/a", company="Acme Labs"),
        _paper(record_id="W2", paper_id="doi:10.1000/b", company="Acme Labs"),
    ]
    result = resolve_entities(rows)
    companies = [entity for entity in result.entities if entity["entity_type"] == "company"]
    assert len(companies) == 2
    assert {entity["canonical_id"] for entity in companies} != {"company:name:acme-labs"}
    assert all(entity["resolution_status"] == "unresolved" for entity in companies)
    assert {
        relation["object_id"]
        for relation in result.relations
        if relation["predicate"] == "affiliated_with"
    } == {entity["canonical_id"] for entity in companies}


def test_run_entities_writes_artifacts_and_metrics(tmp_path: Path) -> None:
    result = run_entities([_paper(), _repo()], run_id="test", artifacts_dir=tmp_path)
    manifest = json.loads((tmp_path / "test/D40/manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((tmp_path / "test/D40/metrics.json").read_text(encoding="utf-8"))
    assert result.input_rows == 2
    assert manifest["status"] == "success"
    assert manifest["output_format"] == "jsonl"
    assert metrics["input_rows"] == 2
    assert metrics["relations"] == len(result.relations)
    assert metrics["relations_without_source_record_id"] == 0
    assert metrics["third_party_writes"] == 0
    assert (tmp_path / "test/D40/entities.jsonl").is_file()
    assert (tmp_path / "test/D40/relations.jsonl").is_file()
    persisted = [
        json.loads(line)
        for line in (tmp_path / "test/D40/relations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert persisted
    assert all(row["source_record_id"] for row in persisted)


def _paper(
    *,
    record_id: str = "W1",
    paper_id: str = PAPER_ID,
    company_domain: str | None = "acme.example",
    company: object | None = None,
    topics: list[str] | None = None,
) -> dict[str, Any]:
    if company is None:
        company = {"name": "Acme Labs", "domain": company_domain}
    return {
        "source_id": "openalex",
        "source_record_id": record_id,
        "canonical_id": paper_id,
        "title": "Efficient Neural Retrieval",
        "abstract_or_excerpt": "Code at https://github.com/acme/atlas",
        "doi": paper_id.removeprefix("doi:"),
        "company": company,
        "topics": ["information retrieval"] if topics is None else topics,
    }


def _repo(
    *,
    source_id: str = "github",
    record_id: str = "83294721",
    name: str = "acme/atlas",
    company_name: str = "Acme Labs",
    company_domain: str = "acme.example",
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_record_id": record_id,
        "canonical_id": "repo:id:83294721",
        "title": name,
        "canonical_url": f"https://github.com/{name}",
        "repo_id": "83294721",
        "abstract_or_excerpt": "Implements arXiv:2401.12345",
        "company": {"name": company_name, "domain": company_domain},
    }
