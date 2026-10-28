"""T10 clustering: stable ids, baseline vectors, and drift freeze."""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

from techpulse.index.embed import HashedNgramEmbedder
from techpulse.trends.cluster import (
    ALGORITHM_VERSION,
    JOIN_COSINE,
    ClusterResult,
    cluster_topics,
    run_cluster,
)

WATER = "municipal chlorine residual monitoring in the north distribution main"
CHEESE = "alpine cave aged cheese wheels turned on spruce boards each morning"
SEED_TEXT = "sparse attention kernels for long context sliding windows and block sparsity"
NEAR_SEED = "kernels for long context sliding windows and block sparsity"


def test_seed_match_keeps_id_when_display_name_changes() -> None:
    records = [_row("r1", SEED_TEXT)]
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "topics.sqlite"
        first = cluster_topics(
            records,
            display_names={"sparse-attention": "Sparse Attention"},
            seed=0,
            database=database,
        )
        second = cluster_topics(
            records,
            display_names={"sparse-attention": "Sparse-Attention Models"},
            seed=0,
            database=database,
        )
    assert first.topics[0]["topic_id"] == "topic:sparse-attention"
    assert second.topics[0]["topic_id"] == "topic:sparse-attention"
    assert second.topics[0]["display_name"] == "Sparse-Attention Models"
    assert second.topics[0]["origin"] == "seed"
    assert second.degraded is False
    assert second.algorithm_version == ALGORITHM_VERSION


def test_same_input_and_seed_match() -> None:
    records = [_row("w1", WATER), _row("w2", WATER + " today"), _row("c1", CHEESE)]
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "topics.sqlite"
        first = cluster_topics(records, seed=0, database=database)
        second = cluster_topics(records, seed=0, database=database)
    assert _members(first) == _members(second)
    assert first.proposals == second.proposals
    assert first.drift == 0.0


def test_baseline_vectors_group_near_copies_only() -> None:
    records = [
        _row("w1", WATER),
        _row("w2", WATER + " today"),
        _row("c1", CHEESE),
        _row("c2", CHEESE + " today"),
    ]
    embedder = HashedNgramEmbedder()
    vectors = embedder.embed([str(record["title"]) for record in records])
    explicit = cluster_topics(
        records,
        embeddings={
            str(record["record_id"]): vector
            for record, vector in zip(records, vectors, strict=True)
        },
        seed=0,
    )
    implicit = cluster_topics(records, seed=0)
    assert _cosine(vectors[0], vectors[1]) >= JOIN_COSINE
    assert _cosine(vectors[0], vectors[2]) < JOIN_COSINE
    assert _members(explicit) == _members(implicit)
    groups = set(_members(explicit).values())
    assert groups == {("w1", "w2"), ("c1", "c2")}
    assert all(topic["origin"] == "cluster" for topic in explicit.topics)


def test_supplied_vectors_are_not_reembedded() -> None:
    records = [_row("a", WATER), _row("b", CHEESE), _row("c", "unrelated municipal note")]
    shared = (1.0, 0.0, 0.0, 0.0)
    other = (0.0, 1.0, 0.0, 0.0)
    result = cluster_topics(
        records,
        embeddings={"a": shared, "b": shared, "c": other},
        seed=0,
    )
    grouped = set(_members(result).values())
    assert grouped == {("a", "b")}
    assert result.unclustered_ids == ("c",)
    assert all(str(topic["origin"]) == "cluster" for topic in result.topics)


def test_added_member_reuses_cluster_id() -> None:
    first_records = [_row("w1", WATER), _row("w2", WATER + " today")]
    second_records = [*first_records, _row("w3", WATER + " this week")]
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "topics.sqlite"
        first = cluster_topics(first_records, seed=0, database=database)
        second = cluster_topics(second_records, seed=0, database=database)
    assert len(first.topics) == 1
    assert second.topics[0]["topic_id"] == first.topics[0]["topic_id"]
    assert second.topics[0]["member_ids"] == ["w1", "w2", "w3"]
    assert second.degraded is False


def test_merge_proposal_links_near_unseeded_cluster_to_seed() -> None:
    records = [
        _row("seed", SEED_TEXT),
        _row("u1", NEAR_SEED),
        _row("u2", NEAR_SEED + " today"),
    ]
    result = cluster_topics(records, seed=0)
    seed_topic = next(topic for topic in result.topics if topic["origin"] == "seed")
    cluster_topic = next(topic for topic in result.topics if topic["origin"] == "cluster")
    assert seed_topic["topic_id"] == "topic:sparse-attention"
    assert cluster_topic["member_ids"] == ["u1", "u2"]
    merge = [item for item in result.proposals if item["kind"] == "merge"]
    assert len(merge) == 1
    assert {merge[0]["left_topic_id"], merge[0]["right_topic_id"]} == {
        seed_topic["topic_id"],
        cluster_topic["topic_id"],
    }


def test_split_proposal_does_not_replace_seed_id() -> None:
    records = [
        _row("a1", "sparse attention " + ("alpha " * 30)),
        _row("a2", "sparse attention " + ("alpha " * 30) + "extra"),
        _row("b1", "sparse attention " + ("bravo " * 30)),
        _row("b2", "sparse attention " + ("bravo " * 30) + "extra"),
    ]
    result = cluster_topics(records, seed=0)
    assert [topic["topic_id"] for topic in result.topics] == ["topic:sparse-attention"]
    splits = [item for item in result.proposals if item["kind"] == "split"]
    assert len(splits) == 1
    assert splits[0]["topic_id"] == "topic:sparse-attention"
    sides = {
        _str_tuple(splits[0]["left_member_ids"]),
        _str_tuple(splits[0]["right_member_ids"]),
    }
    assert sides == {("a1", "a2"), ("b1", "b2")}


def test_large_drift_freezes_the_stored_dictionary() -> None:
    records = [_row("r1", SEED_TEXT), _row("r2", SEED_TEXT + " today")]
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "topics.sqlite"
        first = cluster_topics(records, seed=0, database=database)
        frozen = cluster_topics(records, topics={}, seed=0, database=database)
        kept = cluster_topics(
            records,
            display_names={"sparse-attention": "Still Sparse"},
            seed=0,
            database=database,
        )
    assert first.degraded is False
    assert frozen.degraded is True
    assert frozen.drift == 1.0
    assert [topic["topic_id"] for topic in frozen.topics] == ["topic:sparse-attention"]
    assert kept.degraded is False
    assert kept.topics[0]["topic_id"] == "topic:sparse-attention"
    assert kept.topics[0]["display_name"] == "Still Sparse"


def test_run_cluster_writes_sqlite_and_artifacts() -> None:
    records = [_row("w1", WATER), _row("w2", WATER + " today")]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = run_cluster(records, run_id="t10-run", artifacts_dir=root, seed=0)
        manifest = json.loads(
            (root / "t10-run" / "T10" / "manifest.json").read_text(encoding="utf-8")
        )
        metrics = json.loads(
            (root / "t10-run" / "T10" / "metrics.json").read_text(encoding="utf-8")
        )
        topics = (root / "t10-run" / "T10" / "topics.jsonl").read_text(encoding="utf-8")
        database = root / "t10-run" / "T10" / "topics.sqlite"
        assert result.degraded is False
        assert manifest["ticket"] == "T10"
        assert manifest["seed"] == 0
        assert manifest["algorithm_version"] == ALGORITHM_VERSION
        assert manifest["cluster_degraded"] is False
        assert "topics.jsonl" in manifest["note"]
        assert metrics["status"] == "success"
        assert metrics["third_party_writes"] == 0
        assert topics.count("\n") == 1
        assert database.is_file()


def _row(record_id: str, title: str) -> dict[str, object]:
    return {"record_id": record_id, "title": title}


def _members(result: ClusterResult) -> dict[str, tuple[str, ...]]:
    mapping: dict[str, tuple[str, ...]] = {}
    for topic in result.topics:
        mapping[str(topic["topic_id"])] = _str_tuple(topic["member_ids"])
    return mapping


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return tuple(str(item) for item in value)


def _cosine(left: list[float], right: list[float]) -> float:
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right, strict=True):
        dot += left_value * right_value
        left_norm += left_value * left_value
        right_norm += right_value * right_value
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))
