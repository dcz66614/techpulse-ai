"""E20 contrary search and conflict review.

Each claim gets two template queries: a negation and an alternative
explanation. Both run through I20. No model and no paid API is called.

A retrieved row counts only when ``evidence_supports_claim`` accepts it
and the upstream record already has an https URL, raw hash, date, license,
and citation span. Irrelevant hits are not contradictions. URLs and spans
are copied from the record.

One D30 cluster contributes at most one independent contradiction. Extra
members of that cluster are same-origin reprints. An opposing conclusion,
a publication-date conflict, or a statistical-definition difference is a
major conflict and lowers confidence by one step. A numeric or date
disagreement is also appended to the QA queue. A major conflict never
stays at high confidence.

If retrieval cannot run, the candidate is ``contrary_search_incomplete``
and confidence is at most medium. A finished search with no contrary hit
is ``counterevidence_not_found``. The query text is kept either way.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from techpulse.evidence.retrieve import (
    PreparedCorpus,
    _evidence_item,
    evidence_supports_claim,
    prepare_corpus,
)
from techpulse.index.embed import Embedder
from techpulse.index.hybrid import retrieve, system_ranking
from techpulse.trends.features import _parse_time
from techpulse.trends.score import _confidence as trend_confidence
from techpulse.workflows.activities import LocalObjectStore, ObjectStore

_MAJOR = frozenset({"opposing_conclusion", "publication_date_conflict", "statistical_definition"})
_TIER_RANK = {"A": 3, "B": 2, "C": 1}
_FROM_TO = re.compile(r"\bfrom\s+(\d+)\s+to\s+(\d+)\b", re.IGNORECASE)
_OF = re.compile(r"\b(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)
_FAMILY_COUNT = re.compile(r"\bsource-family count\s+(\d+)\b", re.IGNORECASE)
_TIER_WEIGHT = re.compile(
    r"\btier-weight total\s+(\d+(?:\.\d+)?)\s*/\s*(\d+)\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"\b(?:did not|do not|does not|unchanged)\b", re.IGNORECASE)
_DOWN = re.compile(r"\b(?:declined|decreased|fell|dropped)\b", re.IGNORECASE)
_UP = re.compile(r"\b(?:increased|rose|grew)\b", re.IGNORECASE)
_DEFINITION: dict[str, tuple[str, ...]] = {
    "growth_7d": ("raw records", "including reprints", "calendar week", "percent"),
    "growth_7d_unknown": ("raw records", "including reprints", "percent"),
    "persistence_28d": ("calendar days", "including reprints", "raw records"),
    "diversity_7d": ("raw sources", "including reprints"),
    "quality_7d": ("unweighted", "percent"),
    "developer_7d": ("including reprints", "commits"),
    "novelty_7d": ("including reprints", "raw records", "calendar week"),
}
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
CREATE TABLE IF NOT EXISTS qa_queue (
    qa_id TEXT PRIMARY KEY,
    trend_id TEXT NOT NULL,
    claim_id TEXT,
    evidence_id TEXT,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ContraryQuery:
    kind: str
    template: str
    text: str


@dataclass(frozen=True)
class ContradictionReview:
    status: str
    document: dict[str, object]
    metrics: dict[str, object]


@dataclass(frozen=True)
class _Finding:
    kinds: tuple[str, ...]
    qa: tuple[tuple[str, dict[str, object]], ...]

    @property
    def major(self) -> bool:
        return any(kind in _MAJOR for kind in self.kinds)


@dataclass(frozen=True)
class _Hit:
    record_id: str
    item: dict[str, object]
    text: str


@dataclass
class _ChainDraft:
    trend_id: str
    claim_id: str | None
    template: str
    queries: tuple[ContraryQuery, ...]
    retrieved_ids: list[str]
    irrelevant_dropped: int
    span_missing_dropped: int
    incomplete_dropped: int
    contradictions: list[dict[str, object]]
    reprints: list[dict[str, object]]
    qa: list[dict[str, object]]
    incomplete: bool
    degraded: bool


def contrary_queries(
    template: str,
    topic: str,
    inputs: Mapping[str, object],
    claim_text: str,
) -> tuple[ContraryQuery, ContraryQuery]:
    """Return the negation query and the alternative-explanation query."""
    label = topic.strip() or "topic"
    negation_text, alternative_text = _query_text(template, label, inputs, claim_text)
    name = template or "claim"
    return (
        ContraryQuery("negation", f"{name}_negation", negation_text),
        ContraryQuery("alternative_explanation", f"{name}_alternative", alternative_text),
    )


def review_contradictions(
    pack: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    *,
    clusters: Mapping[str, str] | None = None,
    embedder: Embedder | None = None,
    vector_store_available: bool | None = None,
    retrieval_available: bool = True,
    run_id: str | None = None,
) -> ContradictionReview:
    """Search contrary evidence for every claim in an E10 pack."""
    trends, claims_by_trend, evidence_by_id = _pack_parts(pack)
    prepared = prepare_corpus(records, embedder) if retrieval_available else None
    candidates: list[dict[str, object]] = []
    chains: list[dict[str, object]] = []
    contradictions: list[dict[str, object]] = []
    reprints: list[dict[str, object]] = []
    qa_queue: list[dict[str, object]] = []
    degraded = False
    for trend in trends:
        trend_id = _text(trend.get("trend_id")) or ""
        topic = _text(trend.get("topic")) or _text(trend.get("topic_id")) or trend_id
        claim_rows = claims_by_trend.get(trend_id, [])
        drafts: list[_ChainDraft] = []
        if not claim_rows:
            drafts.append(
                _search_claim(
                    trend_id,
                    None,
                    "candidate",
                    topic,
                    {},
                    f"{topic} stored claim",
                    None,
                    None,
                    (),
                    prepared,
                    clusters,
                    vector_store_available,
                    retrieval_available,
                )
            )
        for claim in claim_rows:
            drafts.append(
                _search_claim(
                    trend_id,
                    _text(claim.get("claim_id")),
                    _text(claim.get("template")) or "claim",
                    topic,
                    _inputs(claim.get("inputs")),
                    _text(claim.get("text")) or "",
                    _text(claim.get("window_start")),
                    _text(claim.get("window_end")),
                    _support_ids(claim),
                    prepared,
                    clusters,
                    vector_store_available,
                    retrieval_available,
                    _support_clusters(claim, evidence_by_id),
                )
            )
        row, chain_rows = _candidate_row(trend, drafts)
        degraded = degraded or any(draft.degraded for draft in drafts)
        candidates.append(row)
        chains.extend(chain_rows)
        for draft in drafts:
            contradictions.extend(draft.contradictions)
            reprints.extend(draft.reprints)
            qa_queue.extend(draft.qa)
    status = _status(candidates)
    high = [
        str(row["trend_id"])
        for row in candidates
        if row.get("confidence") == "high" and isinstance(row.get("trend_id"), str)
    ]
    downgraded = [
        str(row["trend_id"])
        for row in candidates
        if row.get("downgraded") is True and isinstance(row.get("trend_id"), str)
    ]
    document: dict[str, object] = {
        "schema_version": "1.0",
        "ticket": "E20",
        "run_id": run_id,
        "status": status,
        "retrieval_degraded": degraded,
        "retrieval_available": retrieval_available,
        "candidates": candidates,
        "chains": chains,
        "contradictions": contradictions,
        "reprints": reprints,
        "qa_queue": qa_queue,
        "high_confidence_trend_ids": high,
        "downgraded_trend_ids": downgraded,
    }
    metrics = _metrics(document, degraded)
    return ContradictionReview(status, document, metrics)


def run_contradictions(
    pack: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    artifacts_dir: Path,
    clusters: Mapping[str, str] | None = None,
    embedder: Embedder | None = None,
    vector_store_available: bool | None = None,
    retrieval_available: bool = True,
    store: ObjectStore | None = None,
    database: Path | None = None,
) -> ContradictionReview:
    """Write ``{run_id}/E20`` contradictions, manifest, and metrics. Rows go to SQLite."""
    checked = _run_id(run_id)
    review = review_contradictions(
        pack,
        records,
        clusters=clusters,
        embedder=embedder,
        vector_store_available=vector_store_available,
        retrieval_available=retrieval_available,
        run_id=checked,
    )
    object_store = LocalObjectStore(artifacts_dir) if store is None else store
    database_path = (
        database if database is not None else artifacts_dir / checked / "E20" / "evidence.sqlite"
    )
    _save(database_path, review, run_id=checked)
    body = _json_bytes(review.document)
    object_store.put(f"{checked}/E20/contradictions.json", body)
    manifest: dict[str, object] = {
        "ticket": "E20",
        "run_id": checked,
        "status": review.status,
        "contradictions": "contradictions.json",
        "contradictions_sha256": hashlib.sha256(body).hexdigest(),
        "database": database_path.name,
        "high_confidence_trend_ids": review.document["high_confidence_trend_ids"],
        "downgraded_trend_ids": review.document["downgraded_trend_ids"],
        "retrieval_degraded": review.document["retrieval_degraded"],
        "retrieval_available": retrieval_available,
        "qa_queue": len(_mappings(review.document.get("qa_queue"))),
    }
    object_store.put(f"{checked}/E20/manifest.json", _json_bytes(manifest))
    object_store.put(f"{checked}/E20/metrics.json", _json_bytes(review.metrics))
    return review


def _search_claim(
    trend_id: str,
    claim_id: str | None,
    template: str,
    topic: str,
    inputs: Mapping[str, object],
    claim_text: str,
    window_start: str | None,
    window_end: str | None,
    support_ids: Sequence[str],
    prepared: PreparedCorpus | None,
    clusters: Mapping[str, str] | None,
    vector_store_available: bool | None,
    retrieval_available: bool,
    support_clusters: set[str] | None = None,
) -> _ChainDraft:
    queries = contrary_queries(template, topic, inputs, claim_text)
    draft = _ChainDraft(
        trend_id=trend_id,
        claim_id=claim_id,
        template=template,
        queries=queries,
        retrieved_ids=[],
        irrelevant_dropped=0,
        span_missing_dropped=0,
        incomplete_dropped=0,
        contradictions=[],
        reprints=[],
        qa=[],
        incomplete=not retrieval_available,
        degraded=False,
    )
    if not retrieval_available or prepared is None:
        return draft
    seen: set[str] = set()
    for query in queries:
        found = retrieve(
            query.text,
            prepared.corpus,
            embedder=prepared.embedder,
            language="und",
            vector_store_available=vector_store_available,
        )
        draft.degraded = draft.degraded or found.degraded
        lexical = set(found.bm25)
        for doc_id in system_ranking(found):
            if doc_id not in lexical or doc_id in seen:
                continue
            seen.add(doc_id)
            draft.retrieved_ids.append(doc_id)
    relevant: list[_Hit] = []
    for doc_id in draft.retrieved_ids:
        record = prepared.records_by_id.get(doc_id)
        if not isinstance(record, Mapping):
            draft.incomplete_dropped += 1
            continue
        item = _evidence_item(doc_id, record, clusters)
        if item is None:
            draft.incomplete_dropped += 1
            continue
        if item.get("citation_span_missing") is True:
            draft.span_missing_dropped += 1
            continue
        text = _evidence_text(item)
        if not evidence_supports_claim(topic, text, inputs):
            draft.irrelevant_dropped += 1
            continue
        relevant.append(_Hit(doc_id, item, text))
    clusters_for_claim = support_clusters if support_clusters is not None else set()
    _collate(
        draft,
        relevant,
        template,
        inputs,
        window_start,
        window_end,
        set(support_ids),
        clusters_for_claim,
    )
    return draft


def _collate(
    draft: _ChainDraft,
    relevant: Sequence[_Hit],
    template: str,
    inputs: Mapping[str, object],
    window_start: str | None,
    window_end: str | None,
    support_ids: set[str],
    support_clusters: set[str],
) -> None:
    grouped: dict[str, list[_Hit]] = {}
    for hit in relevant:
        grouped.setdefault(_cluster_of(hit.item), []).append(hit)
    for cluster in sorted(grouped):
        ordered = sorted(grouped[cluster], key=_grade_key)
        classified = [
            (
                hit,
                _classify(
                    template,
                    inputs,
                    hit.text,
                    _text(hit.item.get("source_published_at")) or "",
                    window_start,
                    window_end,
                ),
            )
            for hit in ordered
        ]
        support_members = [hit for hit, _finding in classified if _evidence_id(hit) in support_ids]
        same_origin = cluster in support_clusters or bool(support_members)
        if same_origin:
            _collate_support_cluster(draft, classified, support_ids, cluster)
            continue
        _collate_independent_cluster(draft, classified, cluster)


def _collate_support_cluster(
    draft: _ChainDraft,
    classified: Sequence[tuple[_Hit, _Finding]],
    support_ids: set[str],
    cluster: str,
) -> None:
    kept = next(
        (_evidence_id(hit) for hit, _finding in classified if _evidence_id(hit) in support_ids),
        _evidence_id(classified[0][0]) if classified else "",
    )
    for hit, finding in classified:
        evidence_id = _evidence_id(hit)
        if evidence_id in support_ids:
            if finding.kinds:
                draft.contradictions.append(_contradiction(draft, hit, finding, True))
            for reason, observed in finding.qa:
                draft.qa.append(_qa_item(draft, hit, reason, observed, same_origin=False))
            continue
        draft.reprints.append(_reprint(draft, hit, cluster, kept, finding.kinds))
        for reason, observed in finding.qa:
            draft.qa.append(_qa_item(draft, hit, reason, observed, same_origin=True))


def _collate_independent_cluster(
    draft: _ChainDraft,
    classified: Sequence[tuple[_Hit, _Finding]],
    cluster: str,
) -> None:
    conflicts = [(hit, finding) for hit, finding in classified if finding.kinds]
    cited: _Hit | None = None
    if conflicts:
        cited, cited_finding = conflicts[0]
        draft.contradictions.append(_contradiction(draft, cited, cited_finding, True))
    if cited is not None:
        kept = _evidence_id(cited)
    elif classified:
        kept = _evidence_id(classified[0][0])
    else:
        kept = ""
    primary = classified[0][0] if classified else None
    for hit, finding in classified:
        is_cited = cited is not None and hit is cited
        is_primary = cited is None and hit is primary
        if not is_cited and not is_primary:
            draft.reprints.append(_reprint(draft, hit, cluster, kept, finding.kinds))
        for reason, observed in finding.qa:
            draft.qa.append(
                _qa_item(
                    draft,
                    hit,
                    reason,
                    observed,
                    same_origin=not is_cited and not is_primary,
                )
            )


def _candidate_row(
    trend: Mapping[str, object],
    drafts: Sequence[_ChainDraft],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    trend_id = _text(trend.get("trend_id")) or ""
    incomplete = any(draft.incomplete for draft in drafts) or not drafts
    degraded = any(draft.degraded for draft in drafts)
    major = any(
        item.get("major") is True
        for draft in drafts
        for item in draft.contradictions
        if item.get("independent") is True
    )
    executed = not incomplete
    confidence = _next_confidence(
        trend,
        executed=executed,
        incomplete=incomplete,
        degraded=degraded,
        major=major,
    )
    prior = _text(trend.get("confidence")) or ""
    chain_rows: list[dict[str, object]] = []
    chain_ids: list[str] = []
    contradiction_ids: list[str] = []
    qa_ids: list[str] = []
    for draft in drafts:
        chain = _chain_row(draft)
        chain_id = chain["chain_id"]
        if isinstance(chain_id, str):
            chain_ids.append(chain_id)
        chain_rows.append(chain)
        for item in draft.contradictions:
            evidence_id = item.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id not in contradiction_ids:
                contradiction_ids.append(evidence_id)
        for item in draft.qa:
            qa_id = item.get("qa_id")
            if isinstance(qa_id, str) and qa_id not in qa_ids:
                qa_ids.append(qa_id)
    row: dict[str, object] = {
        "trend_id": trend_id,
        "topic": _text(trend.get("topic")) or _text(trend.get("topic_id")) or trend_id,
        "window_days": trend.get("window_days"),
        "score": trend.get("score"),
        "status": trend.get("status"),
        "prior_confidence": prior,
        "confidence": confidence,
        "downgraded": _rank(confidence) < _rank(prior),
        "removed_from_high_confidence": prior == "high" and confidence != "high",
        "major_conflict": major,
        "contrary_search_executed": executed,
        "contrary_search_incomplete": incomplete,
        "retrieval_degraded": degraded,
        "source_families": trend.get("source_families"),
        "tier_a_evidence_ids": trend.get("tier_a_evidence_ids"),
        "citation_chain_complete": trend.get("citation_chain_complete"),
        "qa_pass": trend.get("qa_pass"),
        "experimental": trend.get("experimental") is True
        or trend.get("score_kind") == "experimental",
        "last_verified_at": _text(trend.get("last_verified_at")),
        "chain_ids": chain_ids,
        "contradiction_ids": contradiction_ids,
        "qa_ids": qa_ids,
    }
    return row, chain_rows


def _chain_row(draft: _ChainDraft) -> dict[str, object]:
    if draft.incomplete:
        status = "contrary_search_incomplete"
    elif draft.contradictions:
        status = "conflict"
    else:
        status = "counterevidence_not_found"
    claim_key = draft.claim_id or ""
    chain_id = _digest(f"{draft.trend_id}\n{claim_key}", "cc_")
    queries = [
        {
            "query_id": _digest(f"{draft.trend_id}\n{claim_key}\n{query.kind}", "cq_"),
            "kind": query.kind,
            "template": query.template,
            "text": query.text,
        }
        for query in draft.queries
    ]
    return {
        "chain_id": chain_id,
        "trend_id": draft.trend_id,
        "claim_id": draft.claim_id,
        "template": draft.template,
        "queries": queries,
        "retrieved_ids": list(draft.retrieved_ids),
        "irrelevant_dropped": draft.irrelevant_dropped,
        "span_missing_dropped": draft.span_missing_dropped,
        "incomplete_dropped": draft.incomplete_dropped,
        "contradiction_ids": [item["evidence_id"] for item in draft.contradictions],
        "reprint_ids": [item["evidence_id"] for item in draft.reprints],
        "qa_ids": [item["qa_id"] for item in draft.qa],
        "status": status,
    }


def _next_confidence(
    trend: Mapping[str, object],
    *,
    executed: bool,
    incomplete: bool,
    degraded: bool,
    major: bool,
) -> str:
    if trend.get("status") == "insufficient_evidence":
        confidence = "low"
    else:
        families = _strings(trend.get("source_families"))
        evidence = {
            "tier_a_evidence_ids": _strings(trend.get("tier_a_evidence_ids")),
            "citation_chain_complete": trend.get("citation_chain_complete") is True,
            "contrary_search_executed": executed and not incomplete,
            "qa_pass": trend.get("qa_pass") is True,
        }
        experimental = (
            trend.get("experimental") is True or trend.get("score_kind") == "experimental"
        )
        confidence = trend_confidence(families, evidence, experimental)
    if major and confidence == "high":
        confidence = "medium"
    elif major and confidence == "medium":
        confidence = "low"
    if (incomplete or degraded) and confidence == "high":
        confidence = "medium"
    if trend.get("status") == "insufficient_evidence":
        confidence = "low"
    return confidence


def _classify(
    template: str,
    inputs: Mapping[str, object],
    text: str,
    published: str,
    window_start: str | None,
    window_end: str | None,
) -> _Finding:
    folded = text.casefold()
    definition = any(phrase in folded for phrase in _DEFINITION.get(template, ()))
    parsed, kinds, qa = _measurement(template, inputs, text)
    if definition:
        kinds.append("statistical_definition")
    if (parsed or definition) and _outside(published, window_start, window_end):
        kinds.append("publication_date_conflict")
        qa.append(("date_inconsistency", {"source_published_at": published}))
    return _Finding(tuple(_unique(kinds)), tuple(qa))


def _measurement(
    template: str,
    inputs: Mapping[str, object],
    text: str,
) -> tuple[bool, list[str], list[tuple[str, dict[str, object]]]]:
    if template in {"growth_7d", "growth_7d_unknown"}:
        return _growth(template, inputs, text)
    if template == "persistence_28d":
        return _pair_count(
            text,
            inputs,
            "persistence_numerator",
            "persistence_denominator",
            "utc days",
        )
    if template == "developer_7d":
        return _pair_count(
            text,
            inputs,
            "developer_numerator",
            "developer_denominator",
            "code-family",
        )
    if template == "novelty_7d":
        return _pair_count(
            text,
            inputs,
            "novelty_numerator",
            "novelty_denominator",
            "absent before",
        )
    if template == "diversity_7d":
        return _diversity(text, inputs)
    if template == "quality_7d":
        return _quality(text, inputs)
    return False, [], []


def _growth(
    template: str,
    inputs: Mapping[str, object],
    text: str,
) -> tuple[bool, list[str], list[tuple[str, dict[str, object]]]]:
    match = _FROM_TO.search(text)
    if match is None:
        return False, [], []
    observed_previous = int(match.group(1))
    observed_current = int(match.group(2))
    previous = _stored_int(inputs.get("growth_previous"))
    current = _stored_int(inputs.get("growth_current"))
    kinds: list[str] = []
    qa: list[tuple[str, dict[str, object]]] = []
    if template == "growth_7d_unknown":
        kinds.append("opposing_conclusion")
    numbers_match = previous == observed_previous and current == observed_current
    if previous is not None and current is not None and not numbers_match:
        qa.append(
            (
                "numeric_inconsistency",
                {
                    "growth_previous": observed_previous,
                    "growth_current": observed_current,
                },
            )
        )
        if _sign(observed_previous, observed_current) != _sign(previous, current):
            kinds.append("opposing_conclusion")
    if numbers_match and _NEGATED.search(text) is not None:
        kinds.append("opposing_conclusion")
    if numbers_match and previous is not None and current is not None:
        claim_sign = _sign(previous, current)
        if _DOWN.search(text) is not None and claim_sign > 0:
            kinds.append("opposing_conclusion")
        if _UP.search(text) is not None and claim_sign < 0:
            kinds.append("opposing_conclusion")
    return True, kinds, qa


def _pair_count(
    text: str,
    inputs: Mapping[str, object],
    numerator_key: str,
    denominator_key: str,
    marker: str,
) -> tuple[bool, list[str], list[tuple[str, dict[str, object]]]]:
    if marker not in text.casefold():
        return False, [], []
    match = _OF.search(text)
    if match is None:
        return False, [], []
    observed = (int(match.group(1)), int(match.group(2)))
    numerator = _stored_int(inputs.get(numerator_key))
    denominator = _stored_int(inputs.get(denominator_key))
    kinds: list[str] = []
    qa: list[tuple[str, dict[str, object]]] = []
    if numerator is not None and denominator is not None and observed != (numerator, denominator):
        qa.append(
            (
                "numeric_inconsistency",
                {numerator_key: observed[0], denominator_key: observed[1]},
            )
        )
    if (
        numerator is not None
        and denominator is not None
        and observed == (numerator, denominator)
        and _NEGATED.search(text) is not None
    ):
        kinds.append("opposing_conclusion")
    return True, kinds, qa


def _diversity(
    text: str,
    inputs: Mapping[str, object],
) -> tuple[bool, list[str], list[tuple[str, dict[str, object]]]]:
    match = _FAMILY_COUNT.search(text)
    if match is None:
        return False, [], []
    observed = int(match.group(1))
    stored = _stored_int(inputs.get("diversity_numerator"))
    kinds: list[str] = []
    qa: list[tuple[str, dict[str, object]]] = []
    if stored is not None and observed != stored:
        qa.append(("numeric_inconsistency", {"diversity_numerator": observed}))
    if stored is not None and observed == stored and _NEGATED.search(text) is not None:
        kinds.append("opposing_conclusion")
    return True, kinds, qa


def _quality(
    text: str,
    inputs: Mapping[str, object],
) -> tuple[bool, list[str], list[tuple[str, dict[str, object]]]]:
    match = _TIER_WEIGHT.search(text)
    if match is None:
        return False, [], []
    observed_numerator = float(match.group(1))
    observed_denominator = int(match.group(2))
    stored_numerator = inputs.get("quality_numerator")
    stored_denominator = _stored_int(inputs.get("quality_denominator"))
    kinds: list[str] = []
    qa: list[tuple[str, dict[str, object]]] = []
    same = _same_number(observed_numerator, stored_numerator) and (
        stored_denominator is not None and observed_denominator == stored_denominator
    )
    if stored_denominator is not None and isinstance(stored_numerator, int | float) and not same:
        qa.append(
            (
                "numeric_inconsistency",
                {
                    "quality_numerator": observed_numerator,
                    "quality_denominator": observed_denominator,
                },
            )
        )
    if same and _NEGATED.search(text) is not None:
        kinds.append("opposing_conclusion")
    return True, kinds, qa


def _contradiction(
    draft: _ChainDraft,
    hit: _Hit,
    finding: _Finding,
    independent: bool,
) -> dict[str, object]:
    item = hit.item
    span = item.get("citation_span")
    return {
        "trend_id": draft.trend_id,
        "claim_id": draft.claim_id,
        "evidence_id": _evidence_id(hit),
        "cluster_id": _cluster_of(item),
        "independent": independent,
        "major": finding.major and independent,
        "kinds": list(finding.kinds),
        "source_url": item.get("source_url"),
        "source_published_at": item.get("source_published_at"),
        "collected_at": item.get("collected_at"),
        "sha256": item.get("sha256"),
        "license_status": item.get("license_status"),
        "quality_tier": item.get("quality_tier"),
        "source_family": item.get("source_family"),
        "source_id": item.get("source_id"),
        "source_record_id": item.get("source_record_id"),
        "canonical_id": item.get("canonical_id"),
        "citation_span": span if isinstance(span, dict) else None,
        "normalized_excerpt": item.get("normalized_excerpt"),
        "quote_length": item.get("quote_length"),
        "title": item.get("title"),
    }


def _reprint(
    draft: _ChainDraft,
    hit: _Hit,
    cluster: str,
    kept_evidence_id: str,
    kinds: Sequence[str],
) -> dict[str, object]:
    item = hit.item
    return {
        "trend_id": draft.trend_id,
        "claim_id": draft.claim_id,
        "cluster_id": cluster,
        "evidence_id": _evidence_id(hit),
        "kept_evidence_id": kept_evidence_id,
        "source_url": item.get("source_url"),
        "source_record_id": item.get("source_record_id"),
        "sha256": item.get("sha256"),
        "kinds": list(kinds),
    }


def _qa_item(
    draft: _ChainDraft,
    hit: _Hit,
    reason: str,
    observed: Mapping[str, object],
    *,
    same_origin: bool,
) -> dict[str, object]:
    evidence_id = _evidence_id(hit)
    claim_key = draft.claim_id or ""
    return {
        "qa_id": _digest(f"{draft.trend_id}\n{claim_key}\n{evidence_id}\n{reason}", "qa_"),
        "trend_id": draft.trend_id,
        "claim_id": draft.claim_id,
        "evidence_id": evidence_id,
        "source_url": hit.item.get("source_url"),
        "reason": reason,
        "same_origin": same_origin,
        "observed": dict(observed),
    }


def _query_text(
    template: str,
    topic: str,
    inputs: Mapping[str, object],
    claim_text: str,
) -> tuple[str, str]:
    if template == "growth_7d":
        previous = _number(inputs.get("growth_previous"))
        current = _number(inputs.get("growth_current"))
        negation = f"{topic} independent records did not change from {previous} to {current}"
        alternative = (
            f"{topic} independent records from {previous} to {current} "
            "alternative explanation raw records including reprints calendar week"
        )
        return negation, alternative
    if template == "growth_7d_unknown":
        previous = _number(inputs.get("growth_previous"))
        negation = f"{topic} 7-day growth rate is stored for previous window {previous}"
        alternative = (
            f"{topic} previous window {previous} alternative explanation "
            "raw records including reprints"
        )
        return negation, alternative
    if template == "persistence_28d":
        numerator = _number(inputs.get("persistence_numerator"))
        denominator = _number(inputs.get("persistence_denominator"))
        negation = f"{topic} independent records not on {numerator} of {denominator} UTC days"
        alternative = (
            f"{topic} {numerator} of {denominator} calendar days "
            "alternative explanation including reprints raw records"
        )
        return negation, alternative
    if template == "diversity_7d":
        numerator = _number(inputs.get("diversity_numerator"))
        families = inputs.get("source_families")
        listed = " ".join(_strings(families))
        negation = f"{topic} source-family count not {numerator} {listed}"
        alternative = (
            f"{topic} source-family count {numerator} {listed} "
            "alternative explanation raw sources including reprints"
        )
        return negation, alternative
    if template == "quality_7d":
        numerator = _number(inputs.get("quality_numerator"))
        denominator = _number(inputs.get("quality_denominator"))
        negation = f"{topic} tier-weight total not {numerator}/{denominator}"
        alternative = (
            f"{topic} tier-weight total {numerator}/{denominator} "
            "alternative explanation unweighted percent"
        )
        return negation, alternative
    if template == "developer_7d":
        numerator = _number(inputs.get("developer_numerator"))
        denominator = _number(inputs.get("developer_denominator"))
        negation = f"{topic} code-family records not {numerator} of {denominator}"
        alternative = (
            f"{topic} code-family records {numerator} of {denominator} "
            "alternative explanation commits including reprints"
        )
        return negation, alternative
    if template == "novelty_7d":
        numerator = _number(inputs.get("novelty_numerator"))
        denominator = _number(inputs.get("novelty_denominator"))
        negation = f"{topic} not {numerator} of {denominator} absent before the window"
        alternative = (
            f"{topic} {numerator} of {denominator} absent before "
            "alternative explanation calendar week raw records including reprints"
        )
        return negation, alternative
    body = claim_text.strip() or topic
    negation = f"{topic} did not {body}"
    alternative = f"{topic} alternative explanation raw records including reprints {body}"
    return negation, alternative


def _metrics(document: Mapping[str, object], degraded: bool) -> dict[str, object]:
    candidates = _mappings(document.get("candidates"))
    chains = _mappings(document.get("chains"))
    contradictions = _mappings(document.get("contradictions"))
    reprints = _mappings(document.get("reprints"))
    qa_queue = _mappings(document.get("qa_queue"))
    chains_by_trend: dict[str, list[Mapping[str, object]]] = {}
    for chain in chains:
        trend_id = chain.get("trend_id")
        if isinstance(trend_id, str):
            chains_by_trend.setdefault(trend_id, []).append(chain)
    with_chain = 0
    for row in candidates:
        trend_id = row.get("trend_id")
        if not isinstance(trend_id, str):
            continue
        if any(chain.get("queries") for chain in chains_by_trend.get(trend_id, [])):
            with_chain += 1
    high = [row for row in candidates if row.get("confidence") == "high"]
    high_with_chain = 0
    for row in high:
        trend_id = row.get("trend_id")
        if not isinstance(trend_id, str):
            continue
        owned = chains_by_trend.get(trend_id, [])
        if (
            owned
            and row.get("contrary_search_executed") is True
            and row.get("contrary_search_incomplete") is False
            and all(chain.get("queries") for chain in owned)
        ):
            high_with_chain += 1
    queries = 0
    irrelevant = 0
    for chain in chains:
        query_rows = chain.get("queries")
        if isinstance(query_rows, list):
            queries += len(query_rows)
        dropped = chain.get("irrelevant_dropped")
        if isinstance(dropped, int) and not isinstance(dropped, bool):
            irrelevant += dropped
    return {
        "status": document.get("status"),
        "candidates": len(candidates),
        "candidates_with_chain": with_chain,
        "high_confidence": len(high),
        "high_confidence_with_chain": high_with_chain,
        "high_confidence_chain_rate": 1.0 if not high else high_with_chain / len(high),
        "chains": len(chains),
        "queries": queries,
        "contradictions": len(contradictions),
        "major_conflicts": sum(1 for row in candidates if row.get("major_conflict") is True),
        "same_origin_reprints": len(reprints),
        "irrelevant_dropped": irrelevant,
        "qa_queue": len(qa_queue),
        "numeric_inconsistencies": sum(
            1 for item in qa_queue if item.get("reason") == "numeric_inconsistency"
        ),
        "date_inconsistencies": sum(
            1 for item in qa_queue if item.get("reason") == "date_inconsistency"
        ),
        "counterevidence_not_found": sum(
            1 for chain in chains if chain.get("status") == "counterevidence_not_found"
        ),
        "contrary_search_incomplete": sum(
            1 for row in candidates if row.get("contrary_search_incomplete") is True
        ),
        "downgraded": sum(1 for row in candidates if row.get("downgraded") is True),
        "retrieval_degraded": degraded,
        "llm_calls": 0,
        "paid_api_calls": 0,
        "third_party_writes": 0,
    }


def _status(candidates: Sequence[Mapping[str, object]]) -> str:
    if any(row.get("contrary_search_incomplete") is True for row in candidates):
        return "contrary_search_incomplete"
    return "success"


def _save(database: Path, review: ContradictionReview, *, run_id: str) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_DDL)
        with connection:
            connection.execute("DELETE FROM qa_queue")
            connection.execute("DELETE FROM trend_evidence")
            connection.execute("DELETE FROM evidence_items")
            connection.execute("DELETE FROM trend_scores")
            connection.execute("DELETE FROM raw_records")
            connection.execute("DELETE FROM canonical_entities")
            connection.execute("DELETE FROM source_registry")
            inserted = _insert_evidence(connection, review, run_id)
            for trend in _mappings(review.document.get("candidates")):
                _insert_trend(connection, trend, inserted)
            for item in _mappings(review.document.get("qa_queue")):
                _insert_qa(connection, item)
    finally:
        connection.close()


def _insert_evidence(
    connection: sqlite3.Connection,
    review: ContradictionReview,
    run_id: str,
) -> set[str]:
    raw_uri = f"local:{run_id}/E20/contradictions.json"
    inserted: set[str] = set()
    for item in _mappings(review.document.get("contradictions")):
        evidence_id = _text(item.get("evidence_id"))
        if evidence_id is None or evidence_id in inserted:
            continue
        if _insert_one_evidence(connection, item, raw_uri):
            inserted.add(evidence_id)
    return inserted


def _insert_one_evidence(
    connection: sqlite3.Connection,
    item: Mapping[str, object],
    raw_uri: str,
) -> bool:
    evidence_id = _text(item.get("evidence_id"))
    source_id = _text(item.get("source_id"))
    source_record_id = _text(item.get("source_record_id"))
    url = _text(item.get("source_url"))
    digest = _text(item.get("sha256"))
    family = _text(item.get("source_family"))
    collected = _text(item.get("collected_at"))
    license_status = _text(item.get("license_status"))
    tier = _text(item.get("quality_tier"))
    published = _text(item.get("source_published_at"))
    excerpt = item.get("normalized_excerpt")
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
        return False
    title_value = item.get("title")
    title = title_value if isinstance(title_value, str) and title_value else excerpt
    connection.execute(
        """
        INSERT OR IGNORE INTO source_registry (
            source_id, source_family, base_url, policy_snapshot_id,
            enabled, requests_per_minute, checked_at
        ) VALUES (?, ?, ?, ?, 0, 10, ?)
        """,
        (source_id, family, _base_url(url), "not_supplied", collected),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO raw_records (
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
            "not_supplied",
            raw_uri,
        ),
    )
    raw_row = connection.execute(
        """
        SELECT id FROM raw_records
        WHERE source_id = ? AND source_record_id = ? AND content_sha256 = ?
        """,
        (source_id, source_record_id, digest),
    ).fetchone()
    if raw_row is None:
        return False
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
        INSERT OR IGNORE INTO evidence_items (
            evidence_id, canonical_id, source_record_id, source_family,
            excerpt, quality_tier, license_status, embedding_status, sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (evidence_id, canonical, int(raw_row[0]), family, excerpt, tier, license_status, digest),
    )
    return True


def _insert_trend(
    connection: sqlite3.Connection,
    trend: Mapping[str, object],
    inserted: set[str],
) -> None:
    trend_id = _text(trend.get("trend_id"))
    window = trend.get("window_days")
    score = trend.get("score")
    confidence = _text(trend.get("confidence"))
    if (
        trend_id is None
        or confidence is None
        or isinstance(window, bool)
        or not isinstance(window, int)
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
                    "major_conflict": trend.get("major_conflict"),
                    "contrary_search_executed": trend.get("contrary_search_executed"),
                    "contrary_search_incomplete": trend.get("contrary_search_incomplete"),
                    "qa_ids": trend.get("qa_ids"),
                    "status": trend.get("status"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            json.dumps(families, ensure_ascii=False, sort_keys=True),
        ),
    )
    for evidence_id in _strings(trend.get("contradiction_ids")):
        if evidence_id not in inserted:
            continue
        connection.execute(
            """
            INSERT OR IGNORE INTO trend_evidence (trend_id, evidence_id, relation)
            VALUES (?, ?, 'contradict')
            """,
            (trend_id, evidence_id),
        )


def _insert_qa(connection: sqlite3.Connection, item: Mapping[str, object]) -> None:
    qa_id = _text(item.get("qa_id"))
    trend_id = _text(item.get("trend_id"))
    reason = _text(item.get("reason"))
    if qa_id is None or trend_id is None or reason is None:
        return
    connection.execute(
        """
        INSERT OR IGNORE INTO qa_queue (
            qa_id, trend_id, claim_id, evidence_id, reason, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            qa_id,
            trend_id,
            _text(item.get("claim_id")),
            _text(item.get("evidence_id")),
            reason,
            json.dumps(
                {"observed": item.get("observed"), "source_url": item.get("source_url")},
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )


def _pack_parts(
    pack: Mapping[str, object],
) -> tuple[
    list[Mapping[str, object]],
    dict[str, list[Mapping[str, object]]],
    dict[str, Mapping[str, object]],
]:
    trends = _mappings(pack.get("trends"))
    claims = _mappings(pack.get("claims"))
    evidence = _mappings(pack.get("evidence"))
    claims_by_trend: dict[str, list[Mapping[str, object]]] = {}
    for claim in claims:
        trend_id = _text(claim.get("trend_id"))
        if trend_id is not None:
            claims_by_trend.setdefault(trend_id, []).append(claim)
    evidence_by_id = {
        evidence_id: item
        for item in evidence
        if (evidence_id := _text(item.get("evidence_id"))) is not None
    }
    seen: set[str] = set()
    ordered: list[Mapping[str, object]] = []
    for trend in trends:
        trend_id = _text(trend.get("trend_id"))
        if trend_id is None or trend_id in seen:
            continue
        seen.add(trend_id)
        ordered.append(trend)
    for trend_id in sorted(claims_by_trend):
        if trend_id in seen:
            continue
        ordered.append(
            {
                "trend_id": trend_id,
                "topic": trend_id,
                "confidence": "low",
                "status": "insufficient_evidence",
            }
        )
    return ordered, claims_by_trend, evidence_by_id


def _support_ids(claim: Mapping[str, object]) -> list[str]:
    return _strings(claim.get("supporting_evidence_ids"))


def _support_clusters(
    claim: Mapping[str, object],
    evidence_by_id: Mapping[str, Mapping[str, object]],
) -> set[str]:
    found: set[str] = set()
    for evidence_id in _support_ids(claim):
        item = evidence_by_id.get(evidence_id)
        if item is None:
            continue
        cluster = _text(item.get("cluster_id"))
        if cluster is not None:
            found.add(cluster)
    return found


def _grade_key(hit: _Hit) -> tuple[int, float, str, str]:
    tier = _TIER_RANK.get(_text(hit.item.get("quality_tier")) or "", 0)
    published = _parse_time(hit.item.get("source_published_at"))
    stamp = published.timestamp() if published is not None else 0.0
    family = _text(hit.item.get("source_family")) or ""
    record_id = _text(hit.item.get("source_record_id")) or hit.record_id
    return (-tier, -stamp, family, record_id)


def _outside(published: str, window_start: str | None, window_end: str | None) -> bool:
    if window_start is None or window_end is None:
        return False
    start = _parse_time(window_start)
    end = _parse_time(window_end)
    moment = _parse_time(published)
    if start is None or end is None or moment is None:
        return False
    return not (start <= moment < end)


def _evidence_text(item: Mapping[str, object]) -> str:
    parts: list[str] = []
    title = item.get("title")
    excerpt = item.get("normalized_excerpt")
    if isinstance(title, str) and title:
        parts.append(title)
    if isinstance(excerpt, str) and excerpt:
        parts.append(excerpt)
    return "\n".join(parts)


def _evidence_id(hit: _Hit) -> str:
    return _text(hit.item.get("evidence_id")) or ""


def _cluster_of(item: Mapping[str, object]) -> str:
    return _text(item.get("cluster_id")) or _text(item.get("evidence_id")) or ""


def _inputs(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            rows.append({str(key): entry for key, entry in item.items()})
    return rows


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _unique(values: Sequence[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if value not in found:
            found.append(value)
    return found


def _sign(previous: int, current: int) -> int:
    if current > previous:
        return 1
    if current < previous:
        return -1
    return 0


def _same_number(observed: float, stored: object) -> bool:
    if isinstance(stored, bool) or not isinstance(stored, int | float):
        return False
    return float(stored) == observed


def _number(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return "unknown"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        if float(value).is_integer():
            return str(int(value))
        return format(float(value), ".10g")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "unknown"


def _stored_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _rank(level: str) -> int:
    return {"high": 2, "medium": 1, "low": 0}.get(level, -1)


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return f"https://{parts.hostname}"
    return url


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped == "":
        return None
    return stripped


def _digest(text: str, prefix: str) -> str:
    return prefix + hashlib.sha256(text.encode()).hexdigest()[:16]


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."} or any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
