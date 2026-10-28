"""E10 claim drafts and I20 support retrieval.

Claims are filled from numbers already stored on the trend or its T20
feature row. Nothing in this module calls a model or a paid API.

Support hits come from ``techpulse.index.hybrid.retrieve``. They are then
graded by quality tier, publication time, and source family. A hashed dense
neighbor with no shared token is not support: I20 already omits a BM25
score of zero, and only those ids are graded. A D30 cluster contributes
one hit. A hit is kept only when the upstream record already has an https
URL, a raw sha256, a date, a public license, and a citation span. Missing
provenance is dropped. URLs are not filled in.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from techpulse.contracts import EvidenceItem
from techpulse.index.embed import (
    ActiveCorpus,
    CorpusDocument,
    Embedder,
    HashedNgramEmbedder,
    _body,
)
from techpulse.index.hybrid import retrieve, system_ranking
from techpulse.trends.features import _FAMILY_ALIASES, _FAMILY_BY_SOURCE, _parse_time

MAX_CLAIMS = 3
MAX_EVIDENCE = 10
_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_URL = re.compile(r"^https://\S+$")
_PUBLIC_LICENSE = frozenset({"metadata_only", "reusable_excerpt", "fulltext_allowed"})
_TIER_RANK = {"A": 3, "B": 2, "C": 1}
_SPAN_FIELDS = ("abstract_or_excerpt", "title")


@dataclass(frozen=True)
class ClaimDraft:
    claim_id: str
    trend_id: str
    template: str
    text: str
    query: str
    window_start: str | None
    window_end: str | None
    inputs: dict[str, object]


@dataclass(frozen=True)
class PreparedCorpus:
    corpus: ActiveCorpus
    records_by_id: Mapping[str, Mapping[str, object]]
    embedder: Embedder
    skipped_duplicates: int


@dataclass(frozen=True)
class SupportResult:
    evidence: tuple[dict[str, object], ...]
    retrieved_ids: tuple[str, ...]
    dropped_reprints: int
    dropped_incomplete: int
    dropped_out_of_window: int
    degraded: bool


@dataclass(frozen=True)
class _Hit:
    record_id: str
    cluster_id: str
    tier_rank: int
    published: datetime
    family: str
    evidence: dict[str, object]


def draft_claims(
    trend: Mapping[str, object],
    feature: Mapping[str, object] | None = None,
) -> tuple[ClaimDraft, ...]:
    """Return one to three falsifiable claims, or none if the counts are absent."""
    trend_id = _text(trend.get("trend_id"))
    topic = _text(trend.get("topic")) or _text(trend.get("topic_id"))
    if trend_id is None or topic is None:
        return ()
    window = _window(trend)
    drafts: list[ClaimDraft] = []
    if window == 28:
        _push(drafts, _persistence(trend, feature, trend_id, topic))
    elif window == 7:
        _push(drafts, _growth(trend, feature, trend_id, topic))
    else:
        _push(drafts, _growth(trend, feature, trend_id, topic))
        _push(drafts, _persistence(trend, feature, trend_id, topic))
    _push(drafts, _diversity(trend, feature, trend_id, topic))
    _push(
        drafts,
        _quality(trend, feature, trend_id, topic)
        or _developer(trend, feature, trend_id, topic)
        or _novelty(trend, feature, trend_id, topic),
    )
    return tuple(drafts[:MAX_CLAIMS])


def clusters_from_dedup(rows: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Map ``source:record`` to the D30 canonical id. Unresolved rows stay out."""
    clusters: dict[str, str] = {}
    for row in rows:
        if row.get("unresolved") is True:
            continue
        key = _record_key(row)
        canonical = _text(row.get("canonical_id"))
        if key is None or canonical is None:
            continue
        clusters[key] = canonical
    return clusters


def prepare_corpus(
    records: Sequence[Mapping[str, object]],
    embedder: Embedder | None = None,
) -> PreparedCorpus:
    """Build an in-memory I20 corpus with the local hashed embedder."""
    chosen = _local_embedder(embedder)
    keyed: list[tuple[str, Mapping[str, object], str]] = []
    seen: set[str] = set()
    skipped = 0
    for record in records:
        key = _record_key(record)
        if key is None:
            continue
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        text = _body(record)
        if text.strip() == "":
            continue
        keyed.append((key, record, text))
    documents: list[CorpusDocument] = []
    by_id: dict[str, Mapping[str, object]] = {}
    vectors: Sequence[Sequence[float]] = ()
    if keyed:
        vectors = chosen.embed(tuple(text for _key, _record, text in keyed))
        if len(vectors) != len(keyed):
            raise ValueError("embedder returned a different number of vectors")
    for (key, record, text), vector in zip(keyed, vectors, strict=True):
        source_id = key.split(":", 1)[0]
        stored_hash = _text(record.get("content_sha256"))
        # The corpus object needs some hash string. Evidence keeps a record
        # only when its own content_sha256 already matches the sha256 pattern.
        index_hash = stored_hash if stored_hash is not None and _SHA.fullmatch(stored_hash) else ""
        documents.append(
            CorpusDocument(
                vector_id=key,
                chunk_id=key,
                source_id=source_id,
                source_record_id=key,
                language=_language(record),
                license_status=_text(record.get("license_status")) or "",
                body=text,
                content_sha256=index_hash,
                embedding=tuple(vector),
            )
        )
        by_id[key] = record
    available = bool(documents)
    corpus = ActiveCorpus(
        text_version="e10" if available else None,
        published_version="e10" if available else None,
        model_id=chosen.model_id,
        model_version=chosen.model_version,
        dimension=chosen.dimension,
        vectors_available=available,
        documents=tuple(documents),
    )
    return PreparedCorpus(corpus, by_id, chosen, skipped)


def retrieve_support(
    query: str,
    records: Sequence[Mapping[str, object]],
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    clusters: Mapping[str, str] | None = None,
    embedder: Embedder | None = None,
    vector_store_available: bool | None = None,
    limit: int = MAX_EVIDENCE,
) -> SupportResult:
    """Retrieve one claim's support set and grade it."""
    prepared = prepare_corpus(records, embedder)
    return retrieve_from_corpus(
        query,
        prepared,
        window_start=window_start,
        window_end=window_end,
        clusters=clusters,
        vector_store_available=vector_store_available,
        limit=limit,
    )


def retrieve_from_corpus(
    query: str,
    prepared: PreparedCorpus,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    clusters: Mapping[str, str] | None = None,
    vector_store_available: bool | None = None,
    limit: int = MAX_EVIDENCE,
) -> SupportResult:
    """Grade one I20 result list down to at most ten support hits."""
    found = retrieve(
        query,
        prepared.corpus,
        embedder=prepared.embedder,
        language="und",
        vector_store_available=vector_store_available,
    )
    lexical = set(found.bm25)
    ranked = [doc_id for doc_id in system_ranking(found) if doc_id in lexical]
    start = _parse_time(window_start) if window_start is not None else None
    end = _parse_time(window_end) if window_end is not None else None
    if window_start is None or window_end is None or start is None or end is None:
        start = None
        end = None
    seen: set[str] = set()
    eligible: list[_Hit] = []
    incomplete = 0
    out_of_window = 0
    for doc_id in ranked:
        if doc_id in seen:
            continue
        seen.add(doc_id)
        record = prepared.records_by_id.get(doc_id)
        if record is None:
            incomplete += 1
            continue
        hit = _hit(doc_id, record, clusters)
        if hit is None:
            incomplete += 1
            continue
        if start is not None and end is not None and not (start <= hit.published < end):
            out_of_window += 1
            continue
        eligible.append(hit)
    chosen, reprints = _select(eligible, min(MAX_EVIDENCE, max(0, limit)))
    return SupportResult(
        evidence=tuple(hit.evidence for hit in chosen),
        retrieved_ids=tuple(ranked),
        dropped_reprints=reprints,
        dropped_incomplete=incomplete,
        dropped_out_of_window=out_of_window,
        degraded=found.degraded,
    )


def _select(eligible: Sequence[_Hit], limit: int) -> tuple[list[_Hit], int]:
    grouped: dict[str, list[_Hit]] = {}
    for hit in eligible:
        grouped.setdefault(hit.cluster_id, []).append(hit)
    reprints = 0
    representatives: list[_Hit] = []
    for hits in grouped.values():
        ordered = sorted(hits, key=_grade_key)
        representatives.append(ordered[0])
        reprints += len(hits) - 1
    representatives.sort(key=_grade_key)
    return representatives[:limit], reprints


def _grade_key(hit: _Hit) -> tuple[int, float, str, str]:
    # Higher tier, then newer date, then family name, then record id.
    return (-hit.tier_rank, -hit.published.timestamp(), hit.family, hit.record_id)


def _hit(
    record_id: str,
    record: Mapping[str, object],
    clusters: Mapping[str, str] | None,
) -> _Hit | None:
    evidence = _evidence_item(record_id, record, clusters)
    if evidence is None:
        return None
    published = _parse_time(evidence.get("source_published_at"))
    if published is None:
        return None
    family = evidence.get("source_family")
    tier = evidence.get("quality_tier")
    cluster = evidence.get("cluster_id")
    if not isinstance(family, str) or not isinstance(tier, str) or not isinstance(cluster, str):
        return None
    rank = _TIER_RANK.get(tier)
    if rank is None:
        return None
    return _Hit(record_id, cluster, rank, published, family, evidence)


def _evidence_item(
    record_id: str,
    record: Mapping[str, object],
    clusters: Mapping[str, str] | None,
) -> dict[str, object] | None:
    url = _text(record.get("canonical_url"))
    digest = _text(record.get("content_sha256"))
    if (
        url is None
        or _URL.fullmatch(url) is None
        or digest is None
        or _SHA.fullmatch(digest) is None
    ):
        return None
    license_status = _text(record.get("license_status"))
    if license_status not in _PUBLIC_LICENSE:
        return None
    published = _text(record.get("published_at"))
    collected = _text(record.get("collected_at"))
    if _parse_time(published) is None or _parse_time(collected) is None:
        return None
    family = _family(record)
    tier = _tier(record.get("quality_tier"))
    if family is None or tier is None or published is None or collected is None:
        return None
    spanned = _span(record)
    if spanned is None:
        return None
    span, quote = spanned
    source_id, source_record_id = _source_parts(record, record_id)
    canonical = _canonical(record, record_id, clusters)
    payload: dict[str, object] = {
        "evidence_id": "ev_" + hashlib.sha256(f"{record_id}\n{digest}".encode()).hexdigest()[:32],
        "canonical_id": canonical,
        "source_family": family,
        "source_url": url,
        "source_published_at": published,
        "collected_at": collected,
        "normalized_excerpt": quote,
        "quote_length": len(quote),
        "license_status": license_status,
        "quality_tier": tier,
        "sha256": digest,
    }
    try:
        EvidenceItem.model_validate(payload)
    except ValidationError:
        return None
    title = record.get("title")
    payload["citation_span"] = span
    payload["source_id"] = source_id
    payload["source_record_id"] = source_record_id
    payload["cluster_id"] = _cluster(record, record_id, clusters)
    payload["title"] = title if isinstance(title, str) and title else quote
    policy = _text(record.get("policy_snapshot_id"))
    if policy is not None:
        payload["policy_snapshot_id"] = policy
    return payload


def _span(record: Mapping[str, object]) -> tuple[dict[str, object], str] | None:
    stored = record.get("citation_span")
    if isinstance(stored, dict):
        field = stored.get("field")
        start = stored.get("start")
        end = stored.get("end")
        if (
            isinstance(field, str)
            and field in _SPAN_FIELDS
            and isinstance(start, int)
            and isinstance(end, int)
            and not isinstance(start, bool)
            and not isinstance(end, bool)
        ):
            source = record.get(field)
            if isinstance(source, str) and 0 <= start < end <= len(source):
                quote = source[start:end]
                if quote:
                    return {"field": field, "start": start, "end": end}, quote
    for field in _SPAN_FIELDS:
        source = record.get(field)
        if isinstance(source, str) and source:
            return {"field": field, "start": 0, "end": len(source)}, source
    return None


def _cluster(
    record: Mapping[str, object],
    record_id: str,
    clusters: Mapping[str, str] | None,
) -> str:
    if record.get("unresolved") is True:
        return record_id
    if clusters is not None:
        mapped = _text(clusters.get(record_id))
        if mapped is not None:
            return mapped
    canonical = _text(record.get("canonical_id"))
    if canonical is not None:
        return canonical
    return record_id


def _canonical(
    record: Mapping[str, object],
    record_id: str,
    clusters: Mapping[str, str] | None,
) -> str:
    canonical = _text(record.get("canonical_id"))
    if canonical is not None:
        return canonical
    return _cluster(record, record_id, clusters)


def _family(record: Mapping[str, object]) -> str | None:
    declared = _text(record.get("source_family"))
    if declared is not None:
        mapped = _FAMILY_ALIASES.get(declared.lower())
        if mapped is not None:
            return mapped
    source = _text(record.get("source_id"))
    if source is None:
        return None
    return _FAMILY_BY_SOURCE.get(source.lower())


def _tier(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    canonical = text.upper()
    if canonical in _TIER_RANK:
        return canonical
    return None


def _source_parts(record: Mapping[str, object], record_id: str) -> tuple[str, str]:
    source = _text(record.get("source_id"))
    source_record = _text(record.get("source_record_id"))
    if source is not None and source_record is not None:
        return source, source_record
    source_id, _, remainder = record_id.partition(":")
    return source_id, remainder


def _growth(
    trend: Mapping[str, object],
    feature: Mapping[str, object] | None,
    trend_id: str,
    topic: str,
) -> ClaimDraft | None:
    current = _stored_int(_field(feature, trend, "growth_current"))
    previous = _stored_int(_field(feature, trend, "growth_previous"))
    if current is None or previous is None:
        return None
    start, end = _window_pair(feature, trend, "window_7d_start", "window_7d_end")
    bounds = _bounds(start, end)
    if _field(feature, trend, "growth_unknown") is True:
        text = (
            f"A 7-day growth rate for {topic} is not stored when the previous "
            f"window has {previous} independent records{bounds}."
        )
        template = "growth_7d_unknown"
    else:
        text = (
            f"Independent records for {topic} changed from {previous} to {current} "
            f"in the UTC 7-day window{bounds}."
        )
        template = "growth_7d"
    return _draft(
        trend_id,
        topic,
        template,
        text,
        start,
        end,
        {"growth_previous": previous, "growth_current": current},
    )


def _persistence(
    trend: Mapping[str, object],
    feature: Mapping[str, object] | None,
    trend_id: str,
    topic: str,
) -> ClaimDraft | None:
    numerator = _stored_int(_field(feature, trend, "persistence_numerator"))
    denominator = _stored_int(_field(feature, trend, "persistence_denominator"))
    if numerator is None or denominator is None:
        return None
    start, end = _window_pair(feature, trend, "window_28d_start", "window_28d_end")
    text = (
        f"Topic {topic} has independent records on {numerator} of {denominator} "
        f"UTC days{_bounds(start, end)}."
    )
    return _draft(
        trend_id,
        topic,
        "persistence_28d",
        text,
        start,
        end,
        {"persistence_numerator": numerator, "persistence_denominator": denominator},
    )


def _diversity(
    trend: Mapping[str, object],
    feature: Mapping[str, object] | None,
    trend_id: str,
    topic: str,
) -> ClaimDraft | None:
    families = _family_list(_field(feature, trend, "source_families"))
    numerator = _stored_int(_field(feature, trend, "diversity_numerator"))
    if families is None or numerator is None or numerator != len(families):
        return None
    start, end = _window_pair(feature, trend, "window_7d_start", "window_7d_end")
    listed = ", ".join(families)
    text = (
        f"Topic {topic} has stored source-family count {numerator} ({listed}) "
        f"in the UTC 7-day window{_bounds(start, end)}."
    )
    return _draft(
        trend_id,
        topic,
        "diversity_7d",
        text,
        start,
        end,
        {"diversity_numerator": numerator, "source_families": list(families)},
    )


def _quality(
    trend: Mapping[str, object],
    feature: Mapping[str, object] | None,
    trend_id: str,
    topic: str,
) -> ClaimDraft | None:
    if _field(feature, trend, "quality_unknown") is True:
        return None
    numerator = _stored_number(_field(feature, trend, "quality_numerator"))
    denominator = _stored_int(_field(feature, trend, "quality_denominator"))
    if numerator is None or denominator is None:
        return None
    start, end = _window_pair(feature, trend, "window_7d_start", "window_7d_end")
    text = (
        f"The stored tier-weight total for {topic} in the UTC 7-day window is "
        f"{_format_number(numerator)}/{denominator}{_bounds(start, end)}."
    )
    return _draft(
        trend_id,
        topic,
        "quality_7d",
        text,
        start,
        end,
        {"quality_numerator": numerator, "quality_denominator": denominator},
    )


def _developer(
    trend: Mapping[str, object],
    feature: Mapping[str, object] | None,
    trend_id: str,
    topic: str,
) -> ClaimDraft | None:
    if _field(feature, trend, "developer_unknown") is True:
        return None
    numerator = _stored_int(_field(feature, trend, "developer_numerator"))
    denominator = _stored_int(_field(feature, trend, "developer_denominator"))
    if numerator is None or denominator is None:
        return None
    start, end = _window_pair(feature, trend, "window_7d_start", "window_7d_end")
    text = (
        f"Code-family records are {numerator} of {denominator} independent records "
        f"for {topic} in the UTC 7-day window{_bounds(start, end)}."
    )
    return _draft(
        trend_id,
        topic,
        "developer_7d",
        text,
        start,
        end,
        {"developer_numerator": numerator, "developer_denominator": denominator},
    )


def _novelty(
    trend: Mapping[str, object],
    feature: Mapping[str, object] | None,
    trend_id: str,
    topic: str,
) -> ClaimDraft | None:
    if _field(feature, trend, "novelty_unknown") is True:
        return None
    numerator = _stored_int(_field(feature, trend, "novelty_numerator"))
    denominator = _stored_int(_field(feature, trend, "novelty_denominator"))
    if numerator is None or denominator is None:
        return None
    start, end = _window_pair(feature, trend, "window_7d_start", "window_7d_end")
    text = (
        f"{numerator} of {denominator} independent records for {topic} in the UTC "
        f"7-day window use a key that is absent before that window{_bounds(start, end)}."
    )
    return _draft(
        trend_id,
        topic,
        "novelty_7d",
        text,
        start,
        end,
        {"novelty_numerator": numerator, "novelty_denominator": denominator},
    )


def _draft(
    trend_id: str,
    topic: str,
    template: str,
    text: str,
    start: str | None,
    end: str | None,
    inputs: dict[str, object],
) -> ClaimDraft:
    digest = hashlib.sha256(f"{trend_id}\n{template}".encode()).hexdigest()
    return ClaimDraft(
        claim_id="cl_" + digest[:16],
        trend_id=trend_id,
        template=template,
        text=text,
        query=_query(topic, text),
        window_start=start,
        window_end=end,
        inputs=inputs,
    )


def _query(topic: str, text: str) -> str:
    token = topic.split(":")[-1].strip()
    if token and token != topic:
        return f"{token} {topic} {text}"
    return f"{topic} {text}"


def _push(drafts: list[ClaimDraft], draft: ClaimDraft | None) -> None:
    if draft is not None:
        drafts.append(draft)


def _field(
    feature: Mapping[str, object] | None,
    trend: Mapping[str, object],
    key: str,
) -> object:
    if feature is not None and key in feature:
        return feature[key]
    if key in trend:
        return trend[key]
    return None


def _window_pair(
    feature: Mapping[str, object] | None,
    trend: Mapping[str, object],
    start_key: str,
    end_key: str,
) -> tuple[str | None, str | None]:
    start = _text(_field(feature, trend, start_key))
    end = _text(_field(feature, trend, end_key))
    if start is None or end is None:
        return None, None
    if _parse_time(start) is None or _parse_time(end) is None:
        return None, None
    return start, end


def _bounds(start: str | None, end: str | None) -> str:
    if start is None or end is None:
        return ""
    return f" from {start} inclusive to {end} exclusive"


def _family_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    found: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item in found:
            return None
        found.append(item)
    return found


def _window(trend: Mapping[str, object]) -> int | None:
    value = trend.get("window_days")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value in (7, 28):
        return value
    return None


def _record_key(record: Mapping[str, object]) -> str | None:
    source = _text(record.get("source_id"))
    source_record = _text(record.get("source_record_id"))
    if source is None or source_record is None:
        return None
    return f"{source}:{source_record}"


def _local_embedder(embedder: Embedder | None) -> Embedder:
    chosen = HashedNgramEmbedder() if embedder is None else embedder
    if chosen.calls_network:
        raise ValueError("E10 does not call a network embedder")
    return chosen


def _language(record: Mapping[str, object]) -> str:
    return _text(record.get("language")) or "und"


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped == "":
        return None
    return stripped


def _stored_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _stored_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _format_number(value: int | float) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return format(float(value), ".10g")
