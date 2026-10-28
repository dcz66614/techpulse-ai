"""T10 topic clustering and stable topic ids.

Seed aliases assign candidates first. Records that match no seed are grouped
with average-linkage clustering on I10 baseline vectors (``HashedNgramEmbedder``
or vectors already produced by that embedder). No model is downloaded.

A topic id is not derived from the display name. An existing id is kept when
the name changes, and a cluster id is kept when membership still overlaps the
stored members. If at least half of the stored assignments would change, the
stored dictionary is left as it is and the result is marked ``cluster_degraded``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from techpulse.index.embed import HashedNgramEmbedder
from techpulse.normalize.dedup import title_fingerprint
from techpulse.normalize.entities import DEFAULT_TOPICS
from techpulse.workflows.activities import LocalObjectStore, ObjectStore

ALGORITHM_VERSION = "t10-seed-agglomerative-1"
DEFAULT_SEED = 0
JOIN_COSINE = 0.60
MERGE_COSINE = 0.80
SPLIT_COSINE = 0.40
MIN_CLUSTER_SIZE = 2
MIN_SPLIT_MEMBERS = 4
DRIFT_FREEZE_RATIO = 0.5
PARQUET_NOTE = "pyarrow is not installed; wrote {stem}.jsonl instead of {stem}.parquet"

_DDL = """
CREATE TABLE IF NOT EXISTS topic_dictionary (
    topic_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_assignments (
    record_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ClusterResult:
    topics: tuple[dict[str, object], ...]
    assignments: tuple[dict[str, object], ...]
    proposals: tuple[dict[str, object], ...]
    unclustered_ids: tuple[str, ...]
    degraded: bool
    drift: float
    seed: int
    algorithm_version: str


@dataclass(frozen=True)
class _Prepared:
    record_id: str
    text: str
    vector: tuple[float, ...]
    seed_key: str | None


@dataclass(frozen=True)
class _History:
    topics: tuple[dict[str, object], ...]
    assignments: dict[str, str]
    version: str | None

    @classmethod
    def empty(cls) -> _History:
        return cls((), {}, None)


@dataclass
class _Built:
    topics: list[dict[str, object]]
    assignments: list[dict[str, object]]
    proposals: list[dict[str, object]]
    unclustered_ids: list[str]
    drift: float

    def result(self, seed: int, *, degraded: bool) -> ClusterResult:
        return ClusterResult(
            topics=tuple(self.topics),
            assignments=tuple(self.assignments),
            proposals=tuple(self.proposals),
            unclustered_ids=tuple(self.unclustered_ids),
            degraded=degraded,
            drift=self.drift,
            seed=seed,
            algorithm_version=ALGORITHM_VERSION,
        )


def cluster_topics(
    records: Sequence[Mapping[str, object]],
    *,
    embeddings: Mapping[str, Sequence[float]] | None = None,
    topics: Mapping[str, Sequence[str]] | None = None,
    display_names: Mapping[str, str] | None = None,
    seed: int = DEFAULT_SEED,
    database: Path | None = None,
) -> ClusterResult:
    """Cluster ``records``. ``database`` stores the dictionary across calls."""
    seed_map = DEFAULT_TOPICS if topics is None else topics
    prepared, _duplicates = _prepare_records(
        records,
        embeddings=embeddings,
        alias_index=_alias_index(seed_map),
    )
    history = _load_history(database) if database is not None else _History.empty()
    built = _cluster(prepared, seed_map, display_names or {}, seed, history)
    freeze = bool(history.assignments) and built.drift >= DRIFT_FREEZE_RATIO
    if database is not None and not freeze:
        _save_history(database, built.topics, built.assignments)
    if freeze:
        return ClusterResult(
            topics=history.topics,
            assignments=tuple(
                {"record_id": record_id, "topic_id": topic_id, "assigned_by": "frozen"}
                for record_id, topic_id in sorted(history.assignments.items())
            ),
            proposals=tuple(built.proposals),
            unclustered_ids=tuple(built.unclustered_ids),
            degraded=True,
            drift=built.drift,
            seed=seed,
            algorithm_version=ALGORITHM_VERSION,
        )
    return built.result(seed, degraded=False)


def run_cluster(
    records: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    artifacts_dir: Path,
    store: ObjectStore | None = None,
    embeddings: Mapping[str, Sequence[float]] | None = None,
    topics: Mapping[str, Sequence[str]] | None = None,
    display_names: Mapping[str, str] | None = None,
    seed: int = DEFAULT_SEED,
    database: Path | None = None,
) -> ClusterResult:
    """Write ``{run_id}/T10`` topics, manifest, and metrics. SQL stays in SQLite."""
    database_path = (
        database if database is not None else artifacts_dir / run_id / "T10" / "topics.sqlite"
    )
    result = cluster_topics(
        records,
        embeddings=embeddings,
        topics=topics,
        display_names=display_names,
        seed=seed,
        database=database_path,
    )
    object_store = LocalObjectStore(artifacts_dir) if store is None else store
    body = _jsonl(result.topics)
    name, output_format, note, data = _write_table(
        object_store, run_id, "topics", result.topics, body
    )
    metrics: dict[str, object] = {
        "input_rows": len(records),
        "topics": len(result.topics),
        "seed_topics": sum(1 for topic in result.topics if topic.get("origin") == "seed"),
        "cluster_topics": sum(1 for topic in result.topics if topic.get("origin") == "cluster"),
        "assignments": len(result.assignments),
        "unclustered": len(result.unclustered_ids),
        "proposals": len(result.proposals),
        "merge_proposals": sum(1 for item in result.proposals if item.get("kind") == "merge"),
        "split_proposals": sum(1 for item in result.proposals if item.get("kind") == "split"),
        "drift": result.drift,
        "cluster_degraded": result.degraded,
        "algorithm_version": result.algorithm_version,
        "seed": result.seed,
        "join_cosine": JOIN_COSINE,
        "output_format": output_format,
        "note": note,
        "third_party_writes": 0,
        "status": "success",
    }
    manifest: dict[str, object] = {
        "ticket": "T10",
        "run_id": run_id,
        "status": "success",
        "cluster_degraded": result.degraded,
        "topics": name,
        "topics_sha256": hashlib.sha256(data).hexdigest(),
        "output_format": output_format,
        "note": note,
        "algorithm_version": result.algorithm_version,
        "seed": result.seed,
        "drift": result.drift,
        "proposals": list(result.proposals),
        "database": database_path.name,
    }
    object_store.put(f"{run_id}/T10/metrics.json", _json_bytes(metrics))
    object_store.put(f"{run_id}/T10/manifest.json", _json_bytes(manifest))
    return result


def _prepare_records(
    records: Sequence[Mapping[str, object]],
    *,
    embeddings: Mapping[str, Sequence[float]] | None,
    alias_index: Mapping[str, str],
) -> tuple[list[_Prepared], int]:
    embedder = HashedNgramEmbedder()
    prepared: list[_Prepared] = []
    seen: set[str] = set()
    duplicates = 0
    texts: list[str] = []
    pending: list[tuple[str, str, str | None]] = []
    for record in records:
        record_id = _record_id(record)
        if record_id is None:
            continue
        if record_id in seen:
            duplicates += 1
            continue
        seen.add(record_id)
        text = _document_text(record)
        seeds = _matching_seeds(text, alias_index)
        seed_key = seeds[0] if seeds else None
        supplied = _supplied_vector(record_id, record, embeddings)
        if supplied is None:
            texts.append(text)
            pending.append((record_id, text, seed_key))
        else:
            prepared.append(_Prepared(record_id, text, supplied, seed_key))
    if texts:
        vectors = embedder.embed(texts)
        for (record_id, text, seed_key), vector in zip(pending, vectors, strict=True):
            prepared.append(_Prepared(record_id, text, tuple(vector), seed_key))
    prepared.sort(key=lambda item: item.record_id)
    return prepared, duplicates


def _cluster(
    prepared: Sequence[_Prepared],
    seed_map: Mapping[str, Sequence[str]],
    display_names: Mapping[str, str],
    seed: int,
    history: _History,
) -> _Built:
    vectors = {item.record_id: item.vector for item in prepared}
    present = {item.record_id for item in prepared}
    historical_by_seed = {
        str(topic["seed_key"]): topic
        for topic in history.topics
        if isinstance(topic.get("seed_key"), str)
    }
    historical_clusters = [topic for topic in history.topics if topic.get("origin") == "cluster"]
    topics: list[dict[str, object]] = []
    assignments: list[dict[str, object]] = []
    by_seed: dict[str, list[str]] = {}
    unseeded: list[str] = []
    for item in prepared:
        if item.seed_key is None:
            unseeded.append(item.record_id)
        else:
            by_seed.setdefault(item.seed_key, []).append(item.record_id)
    for seed_key in sorted(by_seed):
        members = sorted(by_seed[seed_key])
        previous = historical_by_seed.get(seed_key)
        topic_id = (
            str(previous["topic_id"])
            if previous is not None and isinstance(previous.get("topic_id"), str)
            else f"topic:{seed_key}"
        )
        stored_name = previous.get("display_name") if previous is not None else None
        display_name = display_names.get(seed_key) or (
            stored_name
            if isinstance(stored_name, str) and stored_name
            else _default_name(seed_key, seed_map)
        )
        topics.append(_topic_row(topic_id, display_name, seed_key, "seed", members, seed))
        for record_id in members:
            assignments.append(
                {"record_id": record_id, "topic_id": topic_id, "assigned_by": "seed"}
            )
    groups = _agglomerate(sorted(unseeded), vectors, seed)
    unclustered = [sorted(group)[0] for group in groups if len(group) < MIN_CLUSTER_SIZE]
    used_history: set[str] = set()
    for group in groups:
        if len(group) < MIN_CLUSTER_SIZE:
            continue
        members = sorted(group)
        topic_id, display_name = _reuse_cluster_id(
            members, historical_clusters, used_history, display_names
        )
        used_history.add(topic_id)
        topics.append(_topic_row(topic_id, display_name, None, "cluster", members, seed))
        for record_id in members:
            assignments.append(
                {"record_id": record_id, "topic_id": topic_id, "assigned_by": "cluster"}
            )
    topics.sort(key=lambda topic: str(topic["topic_id"]))
    assignments.sort(key=lambda item: str(item["record_id"]))
    proposals = _proposals(topics, vectors)
    new_map = {str(item["record_id"]): str(item["topic_id"]) for item in assignments}
    drift = _drift(history.assignments, new_map, present)
    return _Built(topics, assignments, proposals, sorted(unclustered), drift)


def _reuse_cluster_id(
    members: Sequence[str],
    historical: Sequence[Mapping[str, object]],
    used: set[str],
    display_names: Mapping[str, str],
) -> tuple[str, str]:
    member_set = set(members)
    best_id = ""
    best_name = ""
    best_score = 0.0
    for topic in historical:
        topic_id = topic.get("topic_id")
        if not isinstance(topic_id, str) or topic_id in used:
            continue
        stored = topic.get("member_ids")
        if not isinstance(stored, list):
            continue
        stored_ids = {item for item in stored if isinstance(item, str)}
        score = _jaccard(member_set, stored_ids)
        if score < 0.5:
            continue
        if score > best_score or (score == best_score and (best_id == "" or topic_id < best_id)):
            best_score = score
            best_id = topic_id
            name = topic.get("display_name")
            best_name = name if isinstance(name, str) else ""
    if best_id:
        override = display_names.get(best_id)
        return best_id, override or best_name or best_id
    digest = hashlib.sha256(
        (ALGORITHM_VERSION + "|" + "|".join(members)).encode("utf-8")
    ).hexdigest()[:16]
    topic_id = f"topic:cluster:{digest}"
    override = display_names.get(topic_id)
    return topic_id, override or f"cluster {digest[:8]}"


def _proposals(
    topics: Sequence[Mapping[str, object]],
    vectors: Mapping[str, tuple[float, ...]],
) -> list[dict[str, object]]:
    centroids = {
        str(topic["topic_id"]): _centroid(_member_vectors(topic, vectors)) for topic in topics
    }
    proposals: list[dict[str, object]] = []
    ordered = sorted(centroids)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            similarity = _cosine(centroids[left], centroids[right])
            if similarity >= MERGE_COSINE:
                proposals.append(
                    {
                        "kind": "merge",
                        "left_topic_id": left,
                        "right_topic_id": right,
                        "cosine": similarity,
                    }
                )
    for topic in topics:
        members = _member_ids(topic)
        if len(members) < MIN_SPLIT_MEMBERS:
            continue
        split = _split(members, vectors)
        if split is None:
            continue
        left_ids, right_ids, similarity = split
        proposals.append(
            {
                "kind": "split",
                "topic_id": str(topic["topic_id"]),
                "left_member_ids": left_ids,
                "right_member_ids": right_ids,
                "cosine": similarity,
            }
        )
    proposals.sort(
        key=lambda item: (
            str(item["kind"]),
            str(item.get("left_topic_id", item.get("topic_id", ""))),
            str(item.get("right_topic_id", "")),
        )
    )
    return proposals


def _split(
    members: Sequence[str],
    vectors: Mapping[str, tuple[float, ...]],
) -> tuple[list[str], list[str], float] | None:
    pair = _furthest_pair(members, vectors)
    if pair is None:
        return None
    left_center, right_center = pair
    left = [left_center]
    right = [right_center]
    for record_id in sorted(members):
        if record_id in {left_center, right_center}:
            continue
        vector = vectors[record_id]
        left_similarity = _cosine(vector, vectors[left_center])
        right_similarity = _cosine(vector, vectors[right_center])
        if left_similarity > right_similarity or (
            left_similarity == right_similarity and left_center <= right_center
        ):
            left.append(record_id)
        else:
            right.append(record_id)
    if len(left) < 2 or len(right) < 2:
        return None
    similarity = _cosine(
        _centroid([vectors[item] for item in left]), _centroid([vectors[item] for item in right])
    )
    if similarity >= SPLIT_COSINE:
        return None
    return sorted(left), sorted(right), similarity


def _furthest_pair(
    members: Sequence[str],
    vectors: Mapping[str, tuple[float, ...]],
) -> tuple[str, str] | None:
    best: tuple[float, str, str] | None = None
    ordered = sorted(members)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            similarity = _cosine(vectors[left], vectors[right])
            candidate = (similarity, left, right)
            if best is None or candidate < best:
                best = candidate
    if best is None or best[0] >= SPLIT_COSINE:
        return None
    return best[1], best[2]


def _agglomerate(
    record_ids: Sequence[str],
    vectors: Mapping[str, tuple[float, ...]],
    seed: int,
) -> list[list[str]]:
    clusters: dict[str, list[str]] = {record_id: [record_id] for record_id in record_ids}
    max_distance = 1.0 - JOIN_COSINE
    while len(clusters) >= 2:
        best: tuple[float, str, str, str] | None = None
        keys = sorted(clusters)
        for index, left in enumerate(keys):
            for right in keys[index + 1 :]:
                distance = _average_distance(clusters[left], clusters[right], vectors)
                tie = hashlib.sha256(f"{seed}|{left}|{right}".encode()).hexdigest()
                candidate = (distance, tie, left, right)
                if best is None or candidate < best:
                    best = candidate
        if best is None or best[0] > max_distance:
            break
        _distance, _tie, left, right = best
        merged = sorted(clusters[left] + clusters[right])
        del clusters[left]
        del clusters[right]
        clusters[merged[0]] = merged
    return [sorted(members) for members in clusters.values()]


def _average_distance(
    left: Sequence[str],
    right: Sequence[str],
    vectors: Mapping[str, tuple[float, ...]],
) -> float:
    total = 0.0
    count = 0
    for left_id in left:
        for right_id in right:
            total += 1.0 - _cosine(vectors[left_id], vectors[right_id])
            count += 1
    if count == 0:
        return 1.0
    return total / count


def _drift(previous: Mapping[str, str], new_map: Mapping[str, str], present: set[str]) -> float:
    compared = 0
    changed = 0
    for record_id, topic_id in previous.items():
        if record_id not in present:
            continue
        compared += 1
        if new_map.get(record_id) != topic_id:
            changed += 1
    if compared == 0:
        return 0.0
    return changed / compared


def _topic_row(
    topic_id: str,
    display_name: str,
    seed_key: str | None,
    origin: str,
    members: Sequence[str],
    seed: int,
) -> dict[str, object]:
    return {
        "topic_id": topic_id,
        "display_name": display_name,
        "seed_key": seed_key,
        "origin": origin,
        "member_ids": list(members),
        "algorithm_version": ALGORITHM_VERSION,
        "seed": seed,
    }


def _default_name(seed_key: str, seed_map: Mapping[str, Sequence[str]]) -> str:
    aliases = seed_map.get(seed_key, ())
    for alias in aliases:
        if alias.strip():
            return alias
    return seed_key


def _alias_index(topics: Mapping[str, Sequence[str]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for key, aliases in topics.items():
        for alias in aliases:
            normalized = title_fingerprint(alias)
            if normalized:
                index[normalized] = key
    return index


def _matching_seeds(text: str, alias_index: Mapping[str, str]) -> list[str]:
    padded = f" {title_fingerprint(text)} "
    found: list[str] = []
    order = sorted(alias_index.items(), key=lambda item: (-len(item[0]), item[0]))
    for alias, key in order:
        if len(alias) < 3 or key in found:
            continue
        needle = f" {alias} "
        if needle in padded:
            found.append(key)
            padded = padded.replace(needle, " ")
    return found


def _record_id(record: Mapping[str, object]) -> str | None:
    value = record.get("record_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    source = record.get("source_id")
    source_record = record.get("source_record_id")
    if isinstance(source, str) and isinstance(source_record, str) and source and source_record:
        return f"{source}:{source_record}"
    return None


def _document_text(record: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key in ("title", "abstract_or_excerpt", "text"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def _supplied_vector(
    record_id: str,
    record: Mapping[str, object],
    embeddings: Mapping[str, Sequence[float]] | None,
) -> tuple[float, ...] | None:
    if embeddings is not None and record_id in embeddings:
        return _as_vector(embeddings[record_id])
    return _as_vector(record.get("embedding"))


def _as_vector(value: object) -> tuple[float, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return None
    if len(value) == 0:
        return None
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float):
            return None
        vector.append(float(item))
    return tuple(vector)


def _member_ids(topic: Mapping[str, object]) -> list[str]:
    raw = topic.get("member_ids")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _member_vectors(
    topic: Mapping[str, object],
    vectors: Mapping[str, tuple[float, ...]],
) -> list[tuple[float, ...]]:
    return [vectors[record_id] for record_id in _member_ids(topic) if record_id in vectors]


def _centroid(vectors: Sequence[tuple[float, ...]]) -> tuple[float, ...]:
    if not vectors:
        return ()
    width = len(vectors[0])
    totals = [0.0] * width
    used = 0
    for vector in vectors:
        if len(vector) != width:
            continue
        used += 1
        for index, value in enumerate(vector):
            totals[index] += value
    if used == 0:
        return ()
    return tuple(value / used for value in totals)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right, strict=True):
        dot += left_value * right_value
        left_norm += left_value * left_value
        right_norm += right_value * right_value
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.executescript(_DDL)
    return connection


def _load_history(database: Path) -> _History:
    if not database.is_file():
        return _History.empty()
    connection = _connect(database)
    try:
        topic_rows = connection.execute(
            "SELECT payload FROM topic_dictionary ORDER BY topic_id"
        ).fetchall()
        assignment_rows = connection.execute(
            "SELECT record_id, topic_id FROM topic_assignments ORDER BY record_id"
        ).fetchall()
        meta = connection.execute(
            "SELECT value FROM topic_meta WHERE key = 'algorithm_version'"
        ).fetchone()
    finally:
        connection.close()
    topics: list[dict[str, object]] = []
    for row in topic_rows:
        payload = row["payload"]
        if isinstance(payload, str):
            topics.append(_object_json(payload))
    assignments: dict[str, str] = {}
    for row in assignment_rows:
        record_id = row["record_id"]
        topic_id = row["topic_id"]
        if isinstance(record_id, str) and isinstance(topic_id, str):
            assignments[record_id] = topic_id
    version = meta["value"] if meta is not None and isinstance(meta["value"], str) else None
    return _History(tuple(topics), assignments, version)


def _save_history(
    database: Path,
    topics: Sequence[Mapping[str, object]],
    assignments: Sequence[Mapping[str, object]],
) -> None:
    connection = _connect(database)
    try:
        with connection:
            connection.execute("DELETE FROM topic_assignments")
            connection.execute("DELETE FROM topic_dictionary")
            connection.execute("DELETE FROM topic_meta")
            for topic in topics:
                topic_id = str(topic["topic_id"])
                connection.execute(
                    "INSERT INTO topic_dictionary (topic_id, payload) VALUES (?, ?)",
                    (topic_id, json.dumps(topic, ensure_ascii=False, sort_keys=True)),
                )
            for assignment in assignments:
                connection.execute(
                    "INSERT INTO topic_assignments (record_id, topic_id) VALUES (?, ?)",
                    (str(assignment["record_id"]), str(assignment["topic_id"])),
                )
            connection.execute(
                "INSERT INTO topic_meta (key, value) VALUES ('algorithm_version', ?)",
                (ALGORITHM_VERSION,),
            )
            connection.execute(
                "INSERT INTO topic_meta (key, value) VALUES ('saved_at', ?)",
                (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),),
            )
    finally:
        connection.close()


def _object_json(payload: str) -> dict[str, object]:
    loaded: object = json.loads(payload)
    if not isinstance(loaded, dict):
        return {}
    return {str(key): value for key, value in loaded.items()}


def _jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_table(
    store: ObjectStore,
    run_id: str,
    stem: str,
    rows: Sequence[Mapping[str, object]],
    body: bytes,
) -> tuple[str, str, str, bytes]:
    if importlib.util.find_spec("pyarrow") is None:
        name = f"{stem}.jsonl"
        store.put(f"{run_id}/T10/{name}", body)
        return name, "jsonl", PARQUET_NOTE.format(stem=stem), body
    name = f"{stem}.parquet"
    data = _parquet_bytes(rows)
    store.put(f"{run_id}/T10/{name}", data)
    return name, "parquet", "", data


def _parquet_bytes(rows: Sequence[Mapping[str, object]]) -> bytes:
    import io
    from typing import Any

    pyarrow: Any = __import__("pyarrow")
    parquet: Any = __import__("pyarrow.parquet", fromlist=["parquet"])
    buffer = io.BytesIO()
    parquet.write_table(pyarrow.Table.from_pylist([dict(row) for row in rows]), buffer)
    data = buffer.getvalue()
    if not isinstance(data, bytes):
        raise RuntimeError("parquet writer returned no bytes")
    return data
