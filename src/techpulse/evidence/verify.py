"""E10 claim verification, confidence recalibration, and artifact output.

Confidence uses the T30 gate. High requires at least three source families
and two Tier-A evidence ids on the retrieved support set, plus a complete
citation chain, a finished contrary search, and QA pass. One source family
is never high. An experimental trend stays low. Degraded retrieval cannot
stay high.

A trend with no traceable support is ``insufficient_evidence`` and is left
off the high-confidence list. Bytes go through ``ObjectStore``. The local
backend writes under the artifacts directory. SQLite holds the evidence rows.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from techpulse.evidence.retrieve import (
    ClaimDraft,
    draft_claims,
    prepare_corpus,
    retrieve_from_corpus,
)
from techpulse.index.embed import Embedder
from techpulse.trends.features import _parse_time
from techpulse.trends.score import _confidence as trend_confidence
from techpulse.workflows.activities import LocalObjectStore, ObjectStore

_PUBLIC_LICENSE = frozenset({"metadata_only", "reusable_excerpt", "fulltext_allowed"})
_DDL = """
CREATE TABLE IF NOT EXISTS source_registry (
    source_id TEXT PRIMARY KEY,
    source_family TEXT NOT NULL,
    base_url TEXT NOT NULL,
    policy_snapshot_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    requests_per_minute INTEGER NOT NULL DEFAULT 10,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_records (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_registry (source_id),
    source_record_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    abstract_or_excerpt TEXT,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    license_status TEXT NOT NULL,
    policy_snapshot_id TEXT NOT NULL,
    raw_uri TEXT NOT NULL,
    UNIQUE (source_id, source_record_id, content_sha256)
);
CREATE TABLE IF NOT EXISTS canonical_entities (
    canonical_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_id TEXT PRIMARY KEY,
    canonical_id TEXT REFERENCES canonical_entities (canonical_id),
    source_record_id INTEGER NOT NULL REFERENCES raw_records (id),
    source_family TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    quality_tier TEXT NOT NULL,
    license_status TEXT NOT NULL,
    embedding_status TEXT NOT NULL DEFAULT 'pending',
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trend_scores (
    trend_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    window_days INTEGER NOT NULL CHECK (window_days IN (7, 28)),
    score REAL NOT NULL,
    confidence TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    features_json TEXT NOT NULL,
    source_families TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trend_evidence (
    trend_id TEXT REFERENCES trend_scores (trend_id),
    evidence_id TEXT REFERENCES evidence_items (evidence_id),
    relation TEXT NOT NULL CHECK (relation IN ('support', 'contradict')),
    PRIMARY KEY (trend_id, evidence_id, relation)
);
"""


@dataclass(frozen=True)
class EvidencePack:
    status: str
    document: dict[str, object]
    metrics: dict[str, object]


def build_evidence_pack(
    trends: Sequence[Mapping[str, object]],
    records: Sequence[Mapping[str, object]],
    *,
    features: Sequence[Mapping[str, object]] | None = None,
    clusters: Mapping[str, str] | None = None,
    embedder: Embedder | None = None,
    vector_store_available: bool | None = None,
    run_id: str | None = None,
) -> EvidencePack:
    """Draft claims, retrieve support, and recalibrate confidence."""
    prepared = prepare_corpus(records, embedder)
    indexed = _index_features(features)
    trend_rows: list[dict[str, object]] = []
    claim_rows: list[dict[str, object]] = []
    evidence_by_id: dict[str, dict[str, object]] = {}
    reprints = 0
    incomplete = 0
    out_of_window = 0
    degraded = False
    for trend in trends:
        drafts = draft_claims(trend, _feature_for(trend, indexed))
        claim_evidence: list[tuple[ClaimDraft, tuple[dict[str, object], ...], bool]] = []
        trend_degraded = False
        for draft in drafts:
            support = retrieve_from_corpus(
                draft.query,
                prepared,
                window_start=draft.window_start,
                window_end=draft.window_end,
                clusters=clusters,
                vector_store_available=vector_store_available,
            )
            reprints += support.dropped_reprints
            incomplete += support.dropped_incomplete
            out_of_window += support.dropped_out_of_window
            trend_degraded = trend_degraded or support.degraded
            claim_evidence.append((draft, support.evidence, support.degraded))
            for item in support.evidence:
                evidence_id = item.get("evidence_id")
                if isinstance(evidence_id, str):
                    evidence_by_id.setdefault(evidence_id, item)
        row, claims = _trend_row(trend, claim_evidence, trend_degraded)
        trend_rows.append(row)
        claim_rows.extend(claims)
        degraded = degraded or trend_degraded
    evidence = [evidence_by_id[key] for key in sorted(evidence_by_id)]
    status = _pack_status(trend_rows)
    high = [
        str(row["trend_id"])
        for row in trend_rows
        if row.get("confidence") == "high" and isinstance(row.get("trend_id"), str)
    ]
    removed = [
        str(row["trend_id"])
        for row in trend_rows
        if row.get("removed_from_high_confidence") is True and isinstance(row.get("trend_id"), str)
    ]
    document: dict[str, object] = {
        "schema_version": "1.0",
        "ticket": "E10",
        "run_id": run_id,
        "status": status,
        "retrieval_degraded": degraded,
        "trends": trend_rows,
        "claims": claim_rows,
        "evidence": evidence,
        "high_confidence_trend_ids": high,
        "removed_from_high_confidence": removed,
    }
    publishable = sum(1 for claim in claim_rows if claim.get("status") == "supported")
    metrics: dict[str, object] = {
        "status": status,
        "trends": len(trend_rows),
        "supported_trends": sum(1 for row in trend_rows if row.get("status") == "supported"),
        "insufficient_trends": sum(
            1 for row in trend_rows if row.get("status") == "insufficient_evidence"
        ),
        "claims": len(claim_rows),
        "publishable_claims": publishable,
        "max_claims_per_trend": max(
            (
                sum(1 for claim in claim_rows if claim.get("trend_id") == row.get("trend_id"))
                for row in trend_rows
            ),
            default=0,
        ),
        "evidence_items": len(evidence),
        "high_confidence": len(high),
        "removed_from_high_confidence": len(removed),
        "url_chain_complete_rate": _chain_rate(evidence),
        "reprint_duplicates_dropped": reprints,
        "incomplete_records_dropped": incomplete,
        "skipped_duplicate_records": prepared.skipped_duplicates,
        "out_of_window_dropped": out_of_window,
        "retrieval_degraded": degraded,
        "llm_calls": 0,
        "paid_api_calls": 0,
        "third_party_writes": 0,
    }
    return EvidencePack(status, document, metrics)


def run_evidence(
    trends: Sequence[Mapping[str, object]],
    records: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    artifacts_dir: Path,
    features: Sequence[Mapping[str, object]] | None = None,
    clusters: Mapping[str, str] | None = None,
    embedder: Embedder | None = None,
    vector_store_available: bool | None = None,
    store: ObjectStore | None = None,
    database: Path | None = None,
) -> EvidencePack:
    """Write ``{run_id}/E10`` evidence pack, manifest, and metrics. Rows go to SQLite."""
    checked = _run_id(run_id)
    pack = build_evidence_pack(
        trends,
        records,
        features=features,
        clusters=clusters,
        embedder=embedder,
        vector_store_available=vector_store_available,
        run_id=checked,
    )
    object_store = LocalObjectStore(artifacts_dir) if store is None else store
    database_path = (
        database if database is not None else artifacts_dir / checked / "E10" / "evidence.sqlite"
    )
    _save(database_path, pack, run_id=checked)
    body = _json_bytes(pack.document)
    object_store.put(f"{checked}/E10/evidence_pack.json", body)
    manifest: dict[str, object] = {
        "ticket": "E10",
        "run_id": checked,
        "status": pack.status,
        "evidence_pack": "evidence_pack.json",
        "evidence_pack_sha256": hashlib.sha256(body).hexdigest(),
        "database": database_path.name,
        "high_confidence_trend_ids": pack.document["high_confidence_trend_ids"],
        "removed_from_high_confidence": pack.document["removed_from_high_confidence"],
        "retrieval_degraded": pack.document["retrieval_degraded"],
    }
    object_store.put(f"{checked}/E10/manifest.json", _json_bytes(manifest))
    object_store.put(f"{checked}/E10/metrics.json", _json_bytes(pack.metrics))
    return pack


def _trend_row(
    trend: Mapping[str, object],
    claim_evidence: Sequence[tuple[ClaimDraft, tuple[dict[str, object], ...], bool]],
    degraded: bool,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    trend_id = _text(trend.get("trend_id")) or ""
    topic = _text(trend.get("topic")) or _text(trend.get("topic_id")) or ""
    claims: list[dict[str, object]] = []
    evidence_ids: list[str] = []
    for draft, items, claim_degraded in claim_evidence:
        ids = [
            str(item["evidence_id"]) for item in items if isinstance(item.get("evidence_id"), str)
        ]
        for evidence_id in ids:
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        claims.append(
            {
                "claim_id": draft.claim_id,
                "trend_id": draft.trend_id,
                "template": draft.template,
                "text": draft.text,
                "query": draft.query,
                "status": "supported" if ids else "insufficient_evidence",
                "window_start": draft.window_start,
                "window_end": draft.window_end,
                "inputs": draft.inputs,
                "supporting_evidence_ids": ids,
                "retrieval_degraded": claim_degraded,
            }
        )
    families, tier_a = _families_and_tier(evidence_ids, claim_evidence)
    chain = bool(evidence_ids) and all(
        _chain_complete(_item(claim_evidence, evidence_id)) for evidence_id in evidence_ids
    )
    experimental = trend.get("experimental") is True or trend.get("score_kind") == "experimental"
    contrary = trend.get("contrary_search_executed") is True
    qa_pass = trend.get("qa_pass") is True
    supported = any(claim["status"] == "supported" for claim in claims)
    if not supported:
        confidence = "low"
        status = "insufficient_evidence"
    else:
        confidence = trend_confidence(
            families,
            {
                "tier_a_evidence_ids": tier_a,
                "citation_chain_complete": chain,
                "contrary_search_executed": contrary,
                "qa_pass": qa_pass,
            },
            experimental,
        )
        if degraded and confidence == "high":
            confidence = "medium"
        status = "supported"
    prior = trend.get("confidence")
    prior_text = prior if isinstance(prior, str) else ""
    row: dict[str, object] = {
        "trend_id": trend_id,
        "topic": topic,
        "window_days": _window(trend),
        "score": _score(trend.get("score")),
        "prior_confidence": prior_text,
        "confidence": confidence,
        "status": status,
        "removed_from_high_confidence": prior_text == "high" and confidence != "high",
        "source_families": families,
        "tier_a_count": len(tier_a),
        "tier_a_evidence_ids": tier_a,
        "citation_chain_complete": chain,
        "contrary_search_executed": contrary,
        "qa_pass": qa_pass,
        "experimental": experimental,
        "supporting_evidence_ids": evidence_ids,
        "claim_ids": [claim["claim_id"] for claim in claims],
        "retrieval_degraded": degraded,
        "last_verified_at": _text(trend.get("last_verified_at")),
    }
    return row, claims


def _families_and_tier(
    evidence_ids: Sequence[str],
    claim_evidence: Sequence[tuple[ClaimDraft, tuple[dict[str, object], ...], bool]],
) -> tuple[list[str], list[str]]:
    families: list[str] = []
    tier_a: list[str] = []
    for evidence_id in evidence_ids:
        item = _item(claim_evidence, evidence_id)
        family = item.get("source_family")
        if isinstance(family, str) and family not in families:
            families.append(family)
        if item.get("quality_tier") == "A" and evidence_id not in tier_a:
            tier_a.append(evidence_id)
    families.sort()
    return families, tier_a


def _item(
    claim_evidence: Sequence[tuple[ClaimDraft, tuple[dict[str, object], ...], bool]],
    evidence_id: str,
) -> Mapping[str, object]:
    for _draft, items, _degraded in claim_evidence:
        for item in items:
            if item.get("evidence_id") == evidence_id:
                return item
    return {}


def _chain_complete(item: Mapping[str, object]) -> bool:
    url = item.get("source_url")
    digest = item.get("sha256")
    published = item.get("source_published_at")
    license_status = item.get("license_status")
    excerpt = item.get("normalized_excerpt")
    quote_length = item.get("quote_length")
    span = item.get("citation_span")
    if (
        not isinstance(url, str)
        or not url.startswith("https://")
        or any(char.isspace() for char in url)
    ):
        return False
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    try:
        int(digest, 16)
    except ValueError:
        return False
    if _parse_time(published) is None or license_status not in _PUBLIC_LICENSE:
        return False
    if not isinstance(excerpt, str) or excerpt == "":
        return False
    if isinstance(quote_length, bool) or not isinstance(quote_length, int):
        return False
    if quote_length != len(excerpt):
        return False
    if not isinstance(span, dict):
        return False
    start = span.get("start")
    end = span.get("end")
    field = span.get("field")
    if isinstance(start, bool) or isinstance(end, bool):
        return False
    if not isinstance(start, int) or not isinstance(end, int) or not isinstance(field, str):
        return False
    return 0 <= start < end and end - start == len(excerpt)


def _chain_rate(evidence: Sequence[Mapping[str, object]]) -> float:
    if not evidence:
        return 1.0
    complete = sum(1 for item in evidence if _chain_complete(item))
    return complete / len(evidence)


def _pack_status(trends: Sequence[Mapping[str, object]]) -> str:
    if not trends:
        return "insufficient_evidence"
    if any(row.get("status") == "insufficient_evidence" for row in trends):
        return "insufficient_evidence"
    return "success"


def _index_features(
    features: Sequence[Mapping[str, object]] | None,
) -> dict[str, Mapping[str, object]]:
    indexed: dict[str, Mapping[str, object]] = {}
    if features is None:
        return indexed
    for row in features:
        topic_id = row.get("topic_id")
        if isinstance(topic_id, str) and topic_id and topic_id not in indexed:
            indexed[topic_id] = row
    return indexed


def _feature_for(
    trend: Mapping[str, object],
    indexed: Mapping[str, Mapping[str, object]],
) -> Mapping[str, object] | None:
    for key in ("topic_id", "topic"):
        value = trend.get(key)
        if isinstance(value, str) and value in indexed:
            return indexed[value]
    return None


def _save(database: Path, pack: EvidencePack, *, run_id: str) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    evidence = _mappings(pack.document.get("evidence"))
    trends = _mappings(pack.document.get("trends"))
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_DDL)
        with connection:
            connection.execute("DELETE FROM trend_evidence")
            connection.execute("DELETE FROM evidence_items")
            connection.execute("DELETE FROM trend_scores")
            connection.execute("DELETE FROM raw_records")
            connection.execute("DELETE FROM canonical_entities")
            connection.execute("DELETE FROM source_registry")
            _insert_evidence(connection, evidence, run_id)
            for trend in trends:
                _insert_trend(connection, trend)
    finally:
        connection.close()


def _insert_evidence(
    connection: sqlite3.Connection,
    evidence: Sequence[Mapping[str, object]],
    run_id: str,
) -> None:
    raw_uri = f"local:{run_id}/E10/evidence_pack.json"
    for item in evidence:
        evidence_id = _text(item.get("evidence_id"))
        source_id = _text(item.get("source_id"))
        source_record_id = _text(item.get("source_record_id"))
        url = _text(item.get("source_url"))
        digest = _text(item.get("sha256"))
        family = _text(item.get("source_family"))
        collected = _text(item.get("collected_at"))
        license_status = _text(item.get("license_status"))
        tier = _text(item.get("quality_tier"))
        excerpt = item.get("normalized_excerpt")
        published = _text(item.get("source_published_at"))
        if (
            evidence_id is None
            or source_id is None
            or source_record_id is None
            or url is None
            or digest is None
            or family is None
            or collected is None
            or license_status is None
            or tier is None
            or published is None
            or not isinstance(excerpt, str)
            or excerpt == ""
        ):
            continue
        policy = _text(item.get("policy_snapshot_id")) or "not_supplied"
        stored_title = item.get("title")
        title = stored_title if isinstance(stored_title, str) and stored_title else excerpt
        connection.execute(
            """
            INSERT OR IGNORE INTO source_registry (
                source_id, source_family, base_url, policy_snapshot_id,
                enabled, requests_per_minute, checked_at
            ) VALUES (?, ?, ?, ?, 0, 10, ?)
            """,
            (source_id, family, _base_url(url), policy, collected),
        )
        connection.execute(
            """
            INSERT INTO raw_records (
                source_id, source_record_id, canonical_url, title,
                abstract_or_excerpt, published_at, collected_at, content_sha256,
                license_status, policy_snapshot_id, raw_uri
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                source_record_id,
                url,
                title,
                excerpt,
                published,
                collected,
                digest,
                license_status,
                policy,
                raw_uri,
            ),
        )
        raw_id = connection.execute("SELECT last_insert_rowid()").fetchone()
        if raw_id is None:
            continue
        canonical = _text(item.get("canonical_id")) or evidence_id
        connection.execute(
            """
            INSERT OR IGNORE INTO canonical_entities (
                canonical_id, entity_type, display_name, normalized_key
            ) VALUES (?, 'record', ?, ?)
            """,
            (canonical, title, canonical),
        )
        connection.execute(
            """
            INSERT INTO evidence_items (
                evidence_id, canonical_id, source_record_id, source_family,
                excerpt, quality_tier, license_status, embedding_status, sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                evidence_id,
                canonical,
                int(raw_id[0]),
                family,
                excerpt,
                tier,
                license_status,
                digest,
            ),
        )


def _insert_trend(
    connection: sqlite3.Connection,
    trend: Mapping[str, object],
) -> None:
    trend_id = _text(trend.get("trend_id"))
    window = trend.get("window_days")
    score = trend.get("score")
    confidence = _text(trend.get("confidence"))
    if (
        trend_id is None
        or confidence is None
        or not isinstance(window, int)
        or isinstance(window, bool)
        or window not in (7, 28)
        or isinstance(score, bool)
        or not isinstance(score, int | float)
        or not math.isfinite(float(score))
    ):
        return
    scored_at = _text(trend.get("last_verified_at"))
    if scored_at is None or _parse_time(scored_at) is None:
        return
    families = trend.get("source_families")
    if not isinstance(families, list):
        families = []
    connection.execute(
        """
        INSERT INTO trend_scores (
            trend_id, topic, window_days, score, confidence,
            scored_at, features_json, source_families
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trend_id,
            _text(trend.get("topic")) or trend_id,
            window,
            float(score),
            confidence,
            scored_at,
            json.dumps(
                {
                    "status": trend.get("status"),
                    "tier_a_evidence_ids": trend.get("tier_a_evidence_ids"),
                    "citation_chain_complete": trend.get("citation_chain_complete"),
                    "contrary_search_executed": trend.get("contrary_search_executed"),
                    "qa_pass": trend.get("qa_pass"),
                    "experimental": trend.get("experimental"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            json.dumps(families, ensure_ascii=False, sort_keys=True),
        ),
    )
    evidence_ids = trend.get("supporting_evidence_ids")
    if not isinstance(evidence_ids, list):
        return
    for evidence_id in evidence_ids:
        if not isinstance(evidence_id, str):
            continue
        connection.execute(
            """
            INSERT INTO trend_evidence (trend_id, evidence_id, relation)
            VALUES (?, ?, 'support')
            """,
            (trend_id, evidence_id),
        )


def _mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            rows.append({str(key): entry for key, entry in item.items()})
    return rows


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return f"https://{parts.hostname}"
    return url


def _score(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _window(trend: Mapping[str, object]) -> int | None:
    value = trend.get("window_days")
    if isinstance(value, bool) or not isinstance(value, int) or value not in (7, 28):
        return None
    return value


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped == "":
        return None
    return stripped


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."} or any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
