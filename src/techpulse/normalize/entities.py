"""D40 deterministic entity resolution and cross-source relations.

Input rows are canonical records from D30 (the ``canonical_id`` field is
optional; it is derived from identifiers when absent). The resolver creates
paper, repository, company, and technology-topic entities from explicit
structured fields and from explicit GitHub/DOI/arXiv mentions. Relationships
carry the supporting ``source_record_id``, the source field, and a confidence
band. Conflicting evidence is kept, marked ``unresolved``, and never silently
overwritten. Name-only companies and unknown topics stay ``unresolved``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from techpulse.normalize.dedup import (
    collect_identifiers,
    normalize_arxiv,
    normalize_doi,
    normalize_repo,
    title_fingerprint,
)
from techpulse.workflows.activities import LocalObjectStore, ObjectStore

PAPER = "paper"
REPOSITORY = "repository"
COMPANY = "company"
TOPIC = "topic"
PARQUET_NOTE = "pyarrow is not installed; wrote {stem}.jsonl instead of {stem}.parquet"
_ACADEMIC = {"openalex", "crossref", "arxiv"}
_COMPANY_FIELDS = (
    "company",
    "companies",
    "company_name",
    "organization",
    "organizations",
    "institution",
    "institutions",
    "issuer",
    "owner_company",
)
_TOPIC_FIELDS = ("topics", "topic", "subject_terms", "subjects", "keywords", "topic_ids")
_OWNERSHIP_FIELDS = {"company", "companies", "company_name", "issuer", "owner_company"}
_EXCLUSIVE_PREDICATES = {"owned_by"}
_GITHUB_URL = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_DOI_TEXT = re.compile(r"10\.\d{4,9}/[^\s,;)\]}]+", re.IGNORECASE)
_ARXIV_TEXT = re.compile(r"arxiv:\s*(\d{4}\.\d{4,5}|[a-z][a-z-]*/\d{7})", re.IGNORECASE)
_TOPIC_ID = re.compile(r"^[A-Z]\d{2,}$")
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

DEFAULT_TOPICS: dict[str, tuple[str, ...]] = {
    "computer-vision": ("computer vision", "vision systems", "image recognition"),
    "graph-neural-networks": ("graph neural network", "graph neural networks", "gnn"),
    "information-retrieval": ("information retrieval", "neural retrieval", "semantic search"),
    "large-language-models": ("large language model", "large language models", "llm", "llms"),
    "retrieval-augmented-generation": (
        "retrieval augmented generation",
        "retrieval-augmented generation",
        "rag",
    ),
    "robotics": ("autonomous robots", "robot", "robotics"),
    "sparse-attention": ("sparse attention",),
    "transformers": ("transformer", "transformers"),
}


@dataclass(frozen=True)
class EntityResult:
    entities: tuple[dict[str, Any], ...]
    relations: tuple[dict[str, Any], ...]
    unresolved: tuple[str, ...]
    conflicts: tuple[str, ...]
    input_rows: int = 0


@dataclass
class _Entity:
    canonical_id: str
    entity_type: str
    display_name: str
    normalized_key: str
    aliases: set[str] = field(default_factory=set)
    external_ids: set[str] = field(default_factory=set)
    source_record_ids: set[str] = field(default_factory=set)
    resolution_status: str = "resolved"


@dataclass
class _Relation:
    subject_id: str
    predicate: str
    object_id: str
    source_record_ids: set[str] = field(default_factory=set)
    source_fields: set[str] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)
    confidence: str = "low"
    status: str = "confirmed"
    conflict: bool = False


def resolve_entities(
    records: Iterable[Mapping[str, Any]],
    *,
    topics: Mapping[str, Iterable[str]] | None = None,
) -> EntityResult:
    """Resolve entities and relations from canonical D30 rows."""
    rows = [dict(record) for record in records]
    topic_map = _topic_index(DEFAULT_TOPICS if topics is None else topics)
    resolver = _Resolver(topic_map)
    return resolver.resolve(rows)


def run_entities(
    records: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    artifacts_dir: Path,
    store: ObjectStore | None = None,
    topics: Mapping[str, Iterable[str]] | None = None,
) -> EntityResult:
    """Write entities and relations under ``{run_id}/D40`` with manifest and metrics."""
    result = resolve_entities(records, topics=topics)
    object_store = LocalObjectStore(artifacts_dir) if store is None else store
    entity_body = _jsonl(result.entities)
    relation_body = _jsonl(result.relations)
    entity_name, entity_format, entity_note, entity_data = _write_table(
        object_store, run_id, "entities", result.entities, entity_body
    )
    relation_name, _, _, relation_data = _write_table(
        object_store, run_id, "relations", result.relations, relation_body
    )
    metrics = {
        "input_rows": result.input_rows,
        "entities": len(result.entities),
        "relations": len(result.relations),
        "relations_without_source_record_id": sum(
            1 for relation in result.relations if not relation["source_record_id"]
        ),
        "unresolved_entities": sum(
            1 for entity in result.entities if entity["resolution_status"] != "resolved"
        ),
        "conflict_relations": len(result.conflicts),
        "low_confidence_relations": sum(
            1 for relation in result.relations if relation["confidence"] == "low"
        ),
        "entity_types": _count_types(result.entities),
        "output_format": entity_format,
        "note": entity_note,
        "third_party_writes": 0,
        "status": "success",
    }
    manifest = {
        "ticket": "D40",
        "run_id": run_id,
        "status": "success",
        "entities": entity_name,
        "relations": relation_name,
        "entities_sha256": hashlib.sha256(entity_data).hexdigest(),
        "relations_sha256": hashlib.sha256(relation_data).hexdigest(),
        "output_format": entity_format,
        "note": entity_note,
    }
    object_store.put(f"{run_id}/D40/metrics.json", _json_bytes(metrics))
    object_store.put(f"{run_id}/D40/manifest.json", _json_bytes(manifest))
    return result


class _Resolver:
    def __init__(self, topics: Mapping[str, str]) -> None:
        self.entities: dict[str, _Entity] = {}
        self.relations: dict[tuple[str, str, str], _Relation] = {}
        self.by_external: dict[tuple[str, str], str] = {}
        self.by_repo_alias: dict[str, str] = {}
        self.by_topic_alias = topics
        self.primary: dict[str, str] = {}

    def resolve(self, rows: list[dict[str, Any]]) -> EntityResult:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(_row_canonical_id(row), []).append(row)
        for key, group in groups.items():
            self._create_primary(key, group)
        for row in rows:
            self._resolve_row(row)
        self._mark_conflicts()
        return self._result(len(rows))

    def _create_primary(self, key: str, group: list[dict[str, Any]]) -> None:
        identifiers = [collect_identifiers(row) for row in group]
        entity_type = _primary_type(key, group, identifiers)
        entity_id = _primary_id(key, entity_type, identifiers)
        titles = [_text(row, "title") for row in group]
        display = next((title for title in titles if title), key)
        if entity_type == REPOSITORY:
            repo_values = sorted({value for ids in identifiers for value in ids.get("repo", set())})
            names = [value for value in repo_values if not value.startswith("id:")]
            if names:
                display = sorted(names)[0]
            normalized = display
        else:
            normalized = title_fingerprint(display) or key
        entity = self._ensure_entity(
            entity_id, entity_type, display, normalized, resolution_status="resolved"
        )
        entity.aliases.update(title for title in titles if title)
        for row in group:
            provenance = _provenance(row)
            if provenance is not None:
                entity.source_record_ids.add(provenance)
            for listed in _listed_source_ids(row):
                entity.source_record_ids.add(listed)
        for ids in identifiers:
            for kind in ("doi", "arxiv"):
                for value in ids.get(kind, set()):
                    external = f"{kind}:{value}"
                    entity.external_ids.add(external)
                    self.by_external.setdefault((kind, value), entity_id)
            for value in ids.get("repo", set()):
                entity.external_ids.add(f"repo:{value}")
                self.by_external.setdefault(("repo", value), entity_id)
                if not value.startswith("id:"):
                    self.by_repo_alias.setdefault(value, entity_id)
        self.primary[key] = entity_id

    def _resolve_row(self, row: dict[str, Any]) -> None:
        key = _row_canonical_id(row)
        subject_id = self.primary[key]
        subject = self.entities[subject_id]
        provenance = _provenance(row)
        if provenance is None:
            return
        self._topics(row, subject, provenance)
        self._companies(row, subject, provenance)
        self._repositories(row, subject, provenance)
        self._citations(row, subject, provenance)

    def _topics(self, row: Mapping[str, Any], subject: _Entity, provenance: str) -> None:
        title = _text(row, "title") or ""
        abstract = _text(row, "abstract_or_excerpt") or ""
        for name, field_name in _topic_values(row):
            topic_id, resolved, confidence = self._ensure_topic(name, provenance)
            self._add_relation(
                subject.canonical_id,
                "about_topic",
                topic_id,
                provenance,
                field_name,
                name,
                "high" if resolved and confidence == "high" else confidence,
                "confirmed" if resolved else "unresolved",
            )
        for text, field_name, confidence in (
            (title, "title", "medium"),
            (abstract, "abstract_or_excerpt", "low"),
        ):
            for topic_id, alias in self._topic_mentions(text):
                self._add_relation(
                    subject.canonical_id,
                    "about_topic",
                    topic_id,
                    provenance,
                    field_name,
                    alias,
                    confidence,
                    "confirmed",
                )

    def _companies(self, row: Mapping[str, Any], subject: _Entity, provenance: str) -> None:
        for name, stable, field_name in _company_values(row):
            company_id, resolved = self._ensure_company(name, stable, provenance)
            if subject.entity_type == REPOSITORY and field_name in _OWNERSHIP_FIELDS:
                predicate = "owned_by"
            else:
                predicate = "affiliated_with"
            confidence = "high" if resolved and stable else "medium"
            if not resolved:
                confidence = "low"
            self._add_relation(
                subject.canonical_id,
                predicate,
                company_id,
                provenance,
                field_name,
                name,
                confidence,
                "confirmed" if resolved else "unresolved",
            )

    def _repositories(self, row: Mapping[str, Any], subject: _Entity, provenance: str) -> None:
        title = _text(row, "title") or ""
        abstract = _text(row, "abstract_or_excerpt") or ""
        mentions = _repo_values(row)
        for value, field_name, evidence in mentions:
            repo_id, _ = self._ensure_repo(value, provenance)
            if repo_id == subject.canonical_id:
                continue
            if subject.entity_type == PAPER:
                self._add_relation(
                    subject.canonical_id,
                    "references_repository",
                    repo_id,
                    provenance,
                    field_name,
                    evidence or value,
                    "high" if field_name != "text" else "medium",
                    "confirmed",
                )
            elif subject.entity_type == REPOSITORY:
                self._add_relation(
                    subject.canonical_id,
                    "related_repository",
                    repo_id,
                    provenance,
                    field_name,
                    evidence or value,
                    "low",
                    "confirmed",
                )
        if subject.entity_type == REPOSITORY:
            for kind, value, evidence in _paper_mentions(title, abstract):
                paper_id = self._ensure_paper(kind, value, provenance)
                self._add_relation(
                    subject.canonical_id,
                    "references_paper",
                    paper_id,
                    provenance,
                    "abstract_or_excerpt" if evidence in abstract else "title",
                    evidence,
                    "high",
                    "confirmed",
                )

    def _citations(self, row: Mapping[str, Any], subject: _Entity, provenance: str) -> None:
        if subject.entity_type != REPOSITORY:
            return
        for kind in ("doi", "arxiv"):
            for value in collect_identifiers(row).get(kind, set()):
                paper_id = self._ensure_paper(kind, value, provenance)
                self._add_relation(
                    subject.canonical_id,
                    "references_paper",
                    paper_id,
                    provenance,
                    kind,
                    value,
                    "high",
                    "confirmed",
                )

    def _ensure_entity(
        self,
        canonical_id: str,
        entity_type: str,
        display_name: str,
        normalized_key: str,
        *,
        resolution_status: str,
    ) -> _Entity:
        existing = self.entities.get(canonical_id)
        if existing is None:
            existing = _Entity(
                canonical_id,
                entity_type,
                display_name,
                normalized_key,
                resolution_status=resolution_status,
            )
            self.entities[canonical_id] = existing
        if display_name:
            existing.aliases.add(display_name)
        if resolution_status == "resolved":
            existing.resolution_status = "resolved"
        return existing

    def _ensure_repo(self, value: str, provenance: str) -> tuple[str, _Entity]:
        canonical_id = self.by_repo_alias.get(value) or self.by_external.get(("repo", value))
        if canonical_id is None:
            canonical_id = f"repo:{value}"
            entity = self._ensure_entity(
                canonical_id, REPOSITORY, value, value, resolution_status="resolved"
            )
            entity.external_ids.add(f"repo:{value}")
            self.by_external[("repo", value)] = canonical_id
        else:
            entity = self.entities[canonical_id]
        entity.source_record_ids.add(provenance)
        if not value.startswith("id:"):
            self.by_repo_alias.setdefault(value, canonical_id)
            entity.aliases.add(value)
        return canonical_id, entity

    def _ensure_paper(self, kind: str, value: str, provenance: str) -> str:
        existing = self.by_external.get((kind, value))
        if existing is not None:
            self.entities[existing].source_record_ids.add(provenance)
            return existing
        canonical_id = f"paper:{kind}:{value}"
        entity = self._ensure_entity(
            canonical_id, PAPER, value, value, resolution_status="resolved"
        )
        entity.external_ids.add(f"{kind}:{value}")
        entity.source_record_ids.add(provenance)
        self.by_external[(kind, value)] = canonical_id
        return canonical_id

    def _ensure_company(self, name: str, stable: str | None, provenance: str) -> tuple[str, bool]:
        normalized = title_fingerprint(name)
        if stable:
            canonical_id = f"company:id:{_slug(stable)}"
            entity = self._ensure_entity(
                canonical_id, COMPANY, name or stable, normalized, resolution_status="resolved"
            )
            resolved = True
        else:
            suffix = hashlib.sha256(provenance.encode("utf-8")).hexdigest()[:8]
            canonical_id = f"company:name:{_slug(name)}:{suffix}"
            entity = self._ensure_entity(
                canonical_id, COMPANY, name, normalized, resolution_status="unresolved"
            )
            resolved = False
        entity.source_record_ids.add(provenance)
        if name:
            entity.aliases.add(name)
        return canonical_id, resolved

    def _ensure_topic(self, name: str, provenance: str) -> tuple[str, bool, str]:
        normalized = title_fingerprint(name)
        key = self.by_topic_alias.get(normalized)
        if key is not None:
            canonical_id = f"topic:{key}"
            confidence = "high"
            status = "resolved"
        elif _TOPIC_ID.fullmatch(name.strip()):
            canonical_id = f"topic:id:{_slug(name)}"
            confidence = "high"
            status = "resolved"
        else:
            canonical_id = f"topic:name:{_slug(name)}"
            confidence = "low"
            status = "unresolved"
        entity = self._ensure_entity(
            canonical_id, TOPIC, name, normalized or canonical_id, resolution_status=status
        )
        entity.source_record_ids.add(provenance)
        return canonical_id, status == "resolved", confidence

    def _topic_mentions(self, text: str) -> list[tuple[str, str]]:
        padded = f" {title_fingerprint(text)} "
        found: list[tuple[str, str]] = []
        for alias, key in sorted(self.by_topic_alias.items(), key=lambda item: -len(item[0])):
            if len(alias) < 3:
                continue
            if f" {alias} " in padded:
                found.append((f"topic:{key}", alias))
                padded = padded.replace(f" {alias} ", " ")
        return found

    def _add_relation(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        provenance: str,
        source_field: str,
        evidence: str,
        confidence: str,
        status: str,
    ) -> None:
        if not provenance or not subject_id or not object_id:
            return
        key = (subject_id, predicate, object_id)
        relation = self.relations.get(key)
        if relation is None:
            relation = _Relation(subject_id, predicate, object_id)
            self.relations[key] = relation
        relation.source_record_ids.add(provenance)
        relation.source_fields.add(source_field)
        if evidence:
            relation.evidence.add(evidence)
        if _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[relation.confidence]:
            relation.confidence = confidence
        if status == "unresolved":
            relation.status = "unresolved"
            relation.confidence = "low"

    def _mark_conflicts(self) -> None:
        grouped: dict[tuple[str, str], list[_Relation]] = {}
        for relation in self.relations.values():
            grouped.setdefault((relation.subject_id, relation.predicate), []).append(relation)
        for (_, predicate), relations in grouped.items():
            if predicate not in _EXCLUSIVE_PREDICATES:
                continue
            if len({relation.object_id for relation in relations}) < 2:
                continue
            for relation in relations:
                relation.status = "unresolved"
                relation.confidence = "low"
                relation.conflict = True

    def _result(self, input_rows: int) -> EntityResult:
        entity_rows: list[dict[str, Any]] = []
        for entity in sorted(self.entities.values(), key=lambda item: item.canonical_id):
            entity_rows.append(
                {
                    "canonical_id": entity.canonical_id,
                    "entity_type": entity.entity_type,
                    "display_name": entity.display_name,
                    "normalized_key": entity.normalized_key,
                    "aliases": sorted(entity.aliases),
                    "external_ids": sorted(entity.external_ids),
                    "source_record_ids": sorted(entity.source_record_ids),
                    "resolution_status": entity.resolution_status,
                }
            )
        relation_rows: list[dict[str, Any]] = []
        for relation in sorted(
            self.relations.values(),
            key=lambda item: (item.subject_id, item.predicate, item.object_id),
        ):
            relation_rows.append(
                {
                    "relation_id": _relation_id(relation),
                    "subject_id": relation.subject_id,
                    "predicate": relation.predicate,
                    "object_id": relation.object_id,
                    "source_record_id": min(relation.source_record_ids),
                    "source_record_ids": sorted(relation.source_record_ids),
                    "source_fields": sorted(relation.source_fields),
                    "evidence": " | ".join(sorted(relation.evidence))[:500],
                    "confidence": relation.confidence,
                    "status": relation.status,
                    "conflict": relation.conflict,
                }
            )
        entities = tuple(entity_rows)
        relations = tuple(relation_rows)
        unresolved_entities: list[str] = [
            entity["canonical_id"]
            for entity in entities
            if entity["resolution_status"] != "resolved"
        ]
        unresolved_relations: list[str] = [
            relation["relation_id"] for relation in relations if relation["status"] == "unresolved"
        ]
        unresolved = sorted(unresolved_entities + unresolved_relations)
        conflicts = tuple(
            str(relation["relation_id"]) for relation in relations if bool(relation["conflict"])
        )
        return EntityResult(entities, relations, tuple(unresolved), conflicts, input_rows)


def _row_canonical_id(row: Mapping[str, Any]) -> str:
    value = row.get("canonical_id")
    if isinstance(value, str) and value:
        return value
    ids = collect_identifiers(row)
    for kind in ("doi", "arxiv", "repo", "source"):
        values = sorted(ids.get(kind, set()))
        if values:
            return f"{kind}:{values[0]}"
    return "record:" + (_provenance(row) or "unknown")


def _primary_type(
    key: str, group: Sequence[Mapping[str, Any]], identifiers: list[dict[str, set[str]]]
) -> str:
    if any(
        row.get("source_id") in _ACADEMIC or any(ids.get(kind) for kind in ("doi", "arxiv"))
        for row, ids in zip(group, identifiers, strict=True)
    ):
        return PAPER
    if key.startswith("repo:") or any(
        row.get("source_id") == "github" or ids.get("repo")
        for row, ids in zip(group, identifiers, strict=True)
    ):
        return REPOSITORY
    return PAPER


def _primary_id(key: str, entity_type: str, identifiers: list[dict[str, set[str]]]) -> str:
    if entity_type == REPOSITORY:
        repo_values = sorted(
            {value for ids in identifiers for value in ids.get("repo", set())},
            key=lambda value: (0 if value.startswith("id:") else 1, value),
        )
        if repo_values:
            return f"repo:{repo_values[0]}"
        if key.startswith("repo:"):
            return key
    return key


def _topic_index(topics: Mapping[str, Iterable[str]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for key, aliases in topics.items():
        for alias in aliases:
            normalized = title_fingerprint(alias)
            if normalized:
                index[normalized] = key
    return index


def _count_types(entities: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entity in entities:
        kind = str(entity["entity_type"])
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def _text(row: Mapping[str, Any], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _provenance(row: Mapping[str, Any]) -> str | None:
    source = row.get("source_id")
    record = row.get("source_record_id")
    if isinstance(source, str) and isinstance(record, str) and source and record:
        return f"{source}:{record}"
    return None


def _listed_source_ids(row: Mapping[str, Any]) -> list[str]:
    listed = row.get("source_record_ids")
    if not isinstance(listed, list):
        return []
    return [item for item in listed if isinstance(item, str)]


def _named_values(raw: object) -> list[tuple[str, str | None]]:
    if isinstance(raw, str):
        return [(raw.strip(), None)] if raw.strip() else []
    if isinstance(raw, Mapping):
        name = _first_text(raw, ("name", "display_name", "title", "label"))
        stable = _first_text(raw, ("id", "domain", "cik", "ticker", "identifier", "key"))
        if name or stable:
            return [(name or stable or "", stable)]
        return []
    if isinstance(raw, list | tuple):
        values: list[tuple[str, str | None]] = []
        for item in raw:
            values.extend(_named_values(item))
        return values
    return []


def _first_text(row: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _company_values(row: Mapping[str, Any]) -> list[tuple[str, str | None, str]]:
    values: list[tuple[str, str | None, str]] = []
    for field_name in _COMPANY_FIELDS:
        for name, stable in _named_values(row.get(field_name)):
            if name:
                values.append((name, stable, field_name))
    for name, _stable in _named_values(row.get("institution_ids")):
        values.append((name, name, "institution_ids"))
    return values


def _topic_values(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for field_name in _TOPIC_FIELDS:
        raw = row.get(field_name)
        if raw is None:
            continue
        for name, stable in _named_values(raw):
            values.append((name or stable or "", field_name))
    return values


def _repo_values(row: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    values: list[tuple[str, str, str]] = []
    for field_name in ("repo_id", "repo", "repository", "repository_id"):
        raw = row.get(field_name)
        for name, _stable in _named_values(raw):
            normalized = normalize_repo(name)
            if normalized is not None:
                values.append((normalized, field_name, name))
    canonical_url = _text(row, "canonical_url")
    if canonical_url:
        normalized = normalize_repo(canonical_url)
        if normalized is not None:
            values.append((normalized, "canonical_url", canonical_url))
    for text in (_text(row, "title") or "", _text(row, "abstract_or_excerpt") or ""):
        for match in _GITHUB_URL.finditer(text):
            normalized = normalize_repo(f"{match.group(1)}/{match.group(2)}")
            if normalized is not None:
                values.append((normalized, "text", match.group(0)))
    return _dedup_tuple(values)


def _paper_mentions(title: str, abstract: str) -> list[tuple[str, str, str]]:
    values: list[tuple[str, str, str]] = []
    for text in (title, abstract):
        for match in _DOI_TEXT.finditer(text):
            value = normalize_doi(match.group(0))
            if value is not None:
                values.append(("doi", value, match.group(0)))
        for match in _ARXIV_TEXT.finditer(text):
            value = normalize_arxiv(match.group(1))
            if value is not None:
                values.append(("arxiv", value, match.group(0)))
    return _dedup_tuple(values)


def _dedup_tuple(values: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[tuple[str, str, str]] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    slug = "-".join(part for part in re.split(r"[^0-9a-z\u4e00-\u9fff]+", normalized) if part)
    return slug[:120] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _relation_id(relation: _Relation) -> str:
    text = f"{relation.subject_id}|{relation.predicate}|{relation.object_id}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonl(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")


def _write_table(
    store: ObjectStore,
    run_id: str,
    stem: str,
    rows: Iterable[Mapping[str, Any]],
    body: bytes,
) -> tuple[str, str, str, bytes]:
    if importlib.util.find_spec("pyarrow") is None:
        name = f"{stem}.jsonl"
        store.put(f"{run_id}/D40/{name}", body)
        return name, "jsonl", PARQUET_NOTE.format(stem=stem), body
    name = f"{stem}.parquet"
    data = _parquet_bytes(rows)
    store.put(f"{run_id}/D40/{name}", data)
    return name, "parquet", "", data


def _parquet_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    import io

    pyarrow: Any = __import__("pyarrow")
    parquet: Any = __import__("pyarrow.parquet", fromlist=["parquet"])
    buffer = io.BytesIO()
    parquet.write_table(pyarrow.Table.from_pylist([dict(row) for row in rows]), buffer)
    return buffer.getvalue()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
