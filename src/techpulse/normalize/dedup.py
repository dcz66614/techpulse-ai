"""D30 deterministic cross-source deduplication and near-duplicate merging.

The input boundary is an iterable of mappings in the D20/D21 record shape;
optional ``doi``, ``arxiv_id`` and ``repo_id`` fields supply external ids.
Identifiers are tried in the order DOI, arXiv id, GitHub repository id, then
the source's own stable record id. Title fingerprints only produce candidates.
Two records with conflicting identifiers of the same kind are never forced
together; they are reported as ``unresolved``. Every input row is kept in the
output mapping so a merge can be reversed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz.fuzz import token_sort_ratio

from techpulse.workflows.activities import LocalObjectStore, ObjectStore

TEXT_THRESHOLD = 95.0
PARQUET_NOTE = "pyarrow is not installed; wrote dedup.jsonl instead of dedup.parquet"
_DOI = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
_ARXIV = re.compile(
    r"^(?:\d{4}\.\d{4,5}|[a-z][a-z-]*(?:\.[a-z]{2})?/\d{7})(?:v\d+)?$",
    re.IGNORECASE,
)
_REPO = re.compile(
    r"^(?:https?://(?:www\.)?github\.com/)?"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_DOI_HOST = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
_ARXIV_HOST = re.compile(
    r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MatchCandidate:
    left: str
    right: str
    score: float
    reason: str


@dataclass(frozen=True)
class PairwiseMetrics:
    precision: float
    recall: float
    true_positives: int
    false_positives: int
    false_negatives: int


@dataclass(frozen=True)
class DedupResult:
    rows: tuple[dict[str, Any], ...]
    candidates: tuple[MatchCandidate, ...]


def normalize_doi(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    text = _DOI_HOST.sub("", text)
    text = text.rstrip(".;,)]}")
    if not _DOI.fullmatch(text):
        return None
    return text.lower()


def normalize_arxiv(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    text = re.sub(r"^arxiv:\s*", "", text, flags=re.IGNORECASE)
    text = _ARXIV_HOST.sub("", text)
    text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^arxiv:\s*", "", text, flags=re.IGNORECASE)
    match = _ARXIV.fullmatch(text)
    if match is None:
        return None
    return re.sub(r"v\d+$", "", text, flags=re.IGNORECASE).lower()


def normalize_repo(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"id:{value}"
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return f"id:{text}"
    text = re.sub(r"^github:\s*", "", text, flags=re.IGNORECASE)
    match = _REPO.fullmatch(text)
    if match is None:
        return None
    owner, name = match.groups()
    return f"{owner.lower()}/{name.lower()}"


def title_fingerprint(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join("".join(char if char.isalnum() else " " for char in normalized).split())


def record_id(row: Mapping[str, Any]) -> str:
    """Stable source-local identity for one input row."""
    source, record = row.get("source_id"), row.get("source_record_id")
    if isinstance(source, str) and isinstance(record, str) and source and record:
        return f"{source}:{record}"
    raise ValueError("D30 records require string source_id and source_record_id")


def collect_identifiers(row: Mapping[str, Any]) -> dict[str, set[str]]:
    """Every usable identifier on a row, keyed by kind."""
    found: dict[str, set[str]] = {}
    for kind, keys, normalizer in (
        ("doi", ("doi", "DOI"), normalize_doi),
        ("arxiv", ("arxiv_id", "arxiv", "arxivId"), normalize_arxiv),
        ("repo", ("repo_id", "repo", "repository", "repository_id"), normalize_repo),
    ):
        for key in keys:
            value = normalizer(row.get(key))
            if value is not None:
                found.setdefault(kind, set()).add(value)
    canonical_url = row.get("canonical_url")
    if isinstance(canonical_url, str):
        for kind, normalizer in (
            ("doi", normalize_doi),
            ("arxiv", normalize_arxiv),
            ("repo", normalize_repo),
        ):
            value = normalizer(canonical_url)
            if value is not None:
                found.setdefault(kind, set()).add(value)
    source = row.get("source_id")
    if isinstance(source, str):
        source_record = row.get("source_record_id")
        if source in {"crossref", "openalex"}:
            value = normalize_doi(source_record)
            if value is not None:
                found.setdefault("doi", set()).add(value)
        elif source == "arxiv":
            value = normalize_arxiv(source_record)
            if value is not None:
                found.setdefault("arxiv", set()).add(value)
        elif source == "github":
            value = normalize_repo(source_record)
            if value is not None:
                found.setdefault("repo", set()).add(value)
    if isinstance(source, str) and isinstance(row.get("source_record_id"), str):
        found.setdefault("source", set()).add(f"{source}:{row['source_record_id']}")
    return found


def deduplicate_records(
    records: Iterable[Mapping[str, Any]], *, text_threshold: float = TEXT_THRESHOLD
) -> DedupResult:
    """Return one output row per input row, each with a canonical id."""
    rows = [dict(record) for record in records]
    identifiers = [collect_identifiers(row) for row in rows]
    parents = list(range(len(rows)))
    candidates: list[MatchCandidate] = []
    unresolved: set[int] = set()

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def merge(first: int, second: int) -> None:
        for kind, values in identifiers[second].items():
            identifiers[first].setdefault(kind, set()).update(values)

    def join(left: int, right: int) -> bool:
        first, second = root(left), root(right)
        if first == second:
            return True
        if _incompatible(identifiers[first], identifiers[second]):
            return False
        parents[second] = first
        merge(first, second)
        return True

    keys: dict[tuple[str, str], int] = {}
    for index, ids in enumerate(identifiers):
        for kind, values in sorted(ids.items()):
            for value in sorted(values):
                key = (kind, value)
                previous = keys.get(key)
                if previous is None:
                    keys[key] = index
                else:
                    join(previous, index)

    fingerprints = [title_fingerprint(row.get("title")) for row in rows]
    for left in range(len(rows)):
        if not fingerprints[left]:
            continue
        for right in range(left + 1, len(rows)):
            if not fingerprints[right]:
                continue
            score = float(token_sort_ratio(fingerprints[left], fingerprints[right]))
            if score < text_threshold:
                continue
            if root(left) == root(right):
                reason = "linked"
            elif join(left, right):
                reason = "text"
            else:
                reason = "unresolved"
                unresolved.update((left, right))
            candidates.append(
                MatchCandidate(record_id(rows[left]), record_id(rows[right]), score, reason)
            )

    groups: dict[int, list[int]] = {}
    for index in range(len(rows)):
        groups.setdefault(root(index), []).append(index)
    canonical = {
        member: _canonical_id(identifiers, members)
        for members in groups.values()
        for member in members
    }
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        group = groups[root(index)]
        mapped = dict(row)
        mapped["canonical_id"] = canonical[index]
        mapped["source_record_ids"] = sorted({record_id(rows[member]) for member in group})
        mapped["unresolved"] = index in unresolved
        output.append(mapped)
    return DedupResult(tuple(output), tuple(candidates))


def pairwise_metrics(
    rows: Sequence[Mapping[str, Any]], gold_clusters: Mapping[str, str]
) -> PairwiseMetrics:
    """Pairwise P/R against hand labels keyed by ``source_id:source_record_id``."""
    true_positives = false_positives = false_negatives = 0
    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            first, second = record_id(rows[left]), record_id(rows[right])
            predicted = rows[left].get("canonical_id") == rows[right].get("canonical_id")
            actual = gold_clusters.get(first) == gold_clusters.get(second)
            if predicted and actual:
                true_positives += 1
            elif predicted:
                false_positives += 1
            elif actual:
                false_negatives += 1
    precision = (
        true_positives / (true_positives + false_positives)
        if true_positives + false_positives
        else 1.0
    )
    recall = (
        true_positives / (true_positives + false_negatives)
        if true_positives + false_negatives
        else 1.0
    )
    return PairwiseMetrics(precision, recall, true_positives, false_positives, false_negatives)


def run_dedup(
    records: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    artifacts_dir: Path,
    store: ObjectStore | None = None,
    text_threshold: float = TEXT_THRESHOLD,
) -> DedupResult:
    """Write dedup rows plus manifest and metrics under ``{run_id}/D30``."""
    result = deduplicate_records(records, text_threshold=text_threshold)
    object_store = LocalObjectStore(artifacts_dir) if store is None else store
    body = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in result.rows
    )
    name, output_format, note, data = _write_rows(
        object_store, run_id, "dedup", body.encode("utf-8")
    )
    output_uri = f"local:{run_id}/D30/{name}"
    metrics = {
        "input_rows": len(result.rows),
        "canonical_rows": len({row["canonical_id"] for row in result.rows}),
        "candidate_pairs": len(result.candidates),
        "unresolved_rows": sum(bool(row["unresolved"]) for row in result.rows),
        "text_threshold": text_threshold,
        "output_format": output_format,
        "third_party_writes": 0,
        "status": "success",
    }
    manifest = {
        "ticket": "D30",
        "run_id": run_id,
        "status": "success",
        "output": name,
        "output_uri": output_uri,
        "output_sha256": hashlib.sha256(data).hexdigest(),
        "output_format": output_format,
        "note": note,
        "text_threshold": text_threshold,
    }
    object_store.put(f"{run_id}/D30/metrics.json", _json_bytes(metrics))
    object_store.put(f"{run_id}/D30/manifest.json", _json_bytes(manifest))
    return result


def _incompatible(left: Mapping[str, set[str]], right: Mapping[str, set[str]]) -> bool:
    shared = any(
        left[kind] & right[kind]
        for kind in ("doi", "arxiv", "repo", "source")
        if kind in left and kind in right
    )
    if shared:
        return False
    left_external = any(left.get(kind) for kind in ("doi", "arxiv", "repo"))
    right_external = any(right.get(kind) for kind in ("doi", "arxiv", "repo"))
    return left_external and right_external


def _canonical_id(
    identifiers: Sequence[Mapping[str, set[str]]],
    members: Sequence[int],
) -> str:
    for kind in ("doi", "arxiv", "repo", "source"):
        values = sorted(
            {value for member in members for value in identifiers[member].get(kind, set())},
            key=lambda value: (
                (0 if value.startswith("id:") else 1, value) if kind == "repo" else (0, value)
            ),
        )
        if values:
            return f"{kind}:{values[0]}"
    raise ValueError("a D30 record must carry an identifier or source record id")


def _write_rows(
    store: ObjectStore, run_id: str, stem: str, body: bytes
) -> tuple[str, str, str, bytes]:
    if importlib.util.find_spec("pyarrow") is None:
        name = f"{stem}.jsonl"
        store.put(f"{run_id}/D30/{name}", body)
        return name, "jsonl", PARQUET_NOTE, body
    name = f"{stem}.parquet"
    data = _to_parquet(body)
    store.put(f"{run_id}/D30/{name}", data)
    return name, "parquet", "", data


def _to_parquet(body: bytes) -> bytes:
    import io

    pyarrow: Any = __import__("pyarrow")
    parquet: Any = __import__("pyarrow.parquet", fromlist=["parquet"])
    rows = [json.loads(line) for line in body.decode("utf-8").splitlines() if line]
    buffer = io.BytesIO()
    parquet.write_table(pyarrow.Table.from_pylist(rows), buffer)
    return buffer.getvalue()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
