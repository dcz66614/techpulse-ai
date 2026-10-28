"""R20 topic dossiers and evidence cards.

Deterministic render. No model and no paid API. Each topic gets a summary,
7-day and 28-day chart series, support and contrary cards, and a timeline.

Markdown and JSON record the same ``source_version``. That version is the
sha256 of the sorted input digests for the evidence pack, contradictions,
trend rows, draft, and entities. A number or URL is copied from those
inputs. A missing series is labelled ``insufficient_evidence`` and is not
filled with zeros. A contradicted or blocked claim is labelled and is not
stated as a fact.

Excerpts are copied only for a short, spanned quote whose license allows it.
``metadata_only``, a missing citation span, a foreign URL, and an over-long
quote stay out of the dossier. One topic that cannot be rendered is isolated.
The index then degrades to a text list and names the defect.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from techpulse.contracts import EvidenceItem
from techpulse.workflows.activities import LocalObjectStore, ObjectStore

TEMPLATE_VERSION = "render-v1"
PROMPT_MODEL_VERSION = "render-v1/none"
_MODEL_VERSION = "none"
MAX_REUSABLE_EXCERPT = 240
MAX_FULLTEXT_EXCERPT = 2000
_EXCERPT_LICENSES = frozenset({"reusable_excerpt", "fulltext_allowed"})
_INPUT_KEYS = ("contradictions", "draft", "entities", "evidence_pack", "trends")
_SERIES_KEYS = {
    7: ("series_7d", "chart_7d", "points_7d"),
    28: ("series_28d", "chart_28d", "points_28d"),
}
_COUNT_FIELDS = {
    7: (
        "growth_previous",
        "growth_current",
        "growth_numerator",
        "growth_denominator",
        "diversity_numerator",
        "diversity_denominator",
        "quality_numerator",
        "quality_denominator",
        "developer_numerator",
        "developer_denominator",
        "novelty_numerator",
        "novelty_denominator",
    ),
    28: ("persistence_numerator", "persistence_denominator"),
}
_DATE_KEYS = {
    "growth_previous": ("window_7d_previous_start",),
    "growth_current": ("window_7d_start", "window_start"),
}
_EVIDENCE_FIELDS = (
    "evidence_id",
    "canonical_id",
    "source_family",
    "source_url",
    "source_published_at",
    "collected_at",
    "normalized_excerpt",
    "quote_length",
    "license_status",
    "quality_tier",
    "sha256",
)
_ROLE_ORDER = {"support": 0, "contrary": 1, "reprint": 2, "qa": 3}
_ID = re.compile(r"^[A-Za-z0-9_:-]{1,120}$")
_LABEL = re.compile(r"^[a-z0-9_]{1,64}$")
_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URL = re.compile(r"https?://[^\s<>\]\)\"']+")
_MD_ESCAPE = {
    "\\": "\\\\",
    "`": "\\`",
    "*": "\\*",
    "_": "\\_",
    "[": "\\[",
    "]": "\\]",
    "|": "\\|",
    "#": "\\#",
    "<": "&lt;",
    ">": "&gt;",
    "&": "&amp;",
}
_METHOD = (
    "本档案由确定性模板 render-v1 生成。"
    "提示词/模型版本为 render-v1/none。"
    "未调用语言模型，未调用付费 API。"
    "事实句只来自 R10 草稿中未被反证或 QA 阻断的 fact。"
    "证据不足、冲突和未完成的反证检索都带标签，不写成事实。"
    "数字和 URL 只复制已存储的输入。"
    "metadata_only、缺失引用范围、外链和超长摘录不写入正文。"
)


@dataclass(frozen=True)
class TopicRender:
    status: str
    cards: dict[str, object]
    markdown_files: dict[str, str]
    metrics: dict[str, object]


class _TopicDefect(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def source_fingerprint(
    evidence_pack: Mapping[str, object],
    contradictions: Mapping[str, object],
    trends: Sequence[Mapping[str, object]],
    draft: Mapping[str, object] | None = None,
    entities: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, str]:
    """Return the input digests and the combined ``source_version``."""
    payloads: dict[str, object] = {
        "contradictions": contradictions,
        "draft": draft,
        "entities": None if entities is None else list(entities),
        "evidence_pack": evidence_pack,
        "trends": list(trends),
    }
    hashes = {name: _hash_input(payloads[name]) for name in _INPUT_KEYS}
    lines = [f"{name}={hashes[name]}" for name in sorted(hashes)]
    version = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {"source_version": version, **hashes}


def render_topics(
    evidence_pack: Mapping[str, object],
    contradictions: Mapping[str, object],
    trends: Sequence[Mapping[str, object]],
    *,
    draft: Mapping[str, object] | None = None,
    entities: Sequence[Mapping[str, object]] | None = None,
    run_id: str | None = None,
) -> TopicRender:
    """Render one dossier per topic from the stored T30, E10, E20, and R10 inputs."""
    fingerprint = source_fingerprint(
        evidence_pack,
        contradictions,
        trends,
        draft,
        entities,
    )
    error = _structure_error(evidence_pack, contradictions, trends, draft, entities)
    if error is not None:
        return _failed(run_id, fingerprint, error)
    pack = evidence_pack
    review = contradictions
    index = _evidence_index(pack)
    allowed = {str(item["source_url"]) for item in index.values()}
    grouped, pack_ids, defects = _group_topics(trends, pack)
    draft_status = _draft_status(draft)
    used: set[str] = set()
    topics: list[dict[str, object]] = []
    files: dict[str, str] = {}
    for topic in sorted(grouped):
        slug = _unique_slug(topic, used)
        rows = grouped[topic]
        try:
            _reject_non_finite(rows)
            built = _build_topic(
                topic,
                slug,
                rows,
                pack_ids.get(topic, ()),
                pack,
                review,
                draft,
                entities,
                index,
                allowed,
                draft_status,
                fingerprint["source_version"],
                run_id,
            )
        except _TopicDefect as exc:
            built = _isolated(topic, slug, exc.reason, fingerprint["source_version"], run_id)
            defects.append({"topic": topic, "reason": exc.reason})
        except Exception:
            built = _isolated(topic, slug, "render_failed", fingerprint["source_version"], run_id)
            defects.append({"topic": topic, "reason": "render_failed"})
        topics.append(built)
        files[str(built["markdown_file"])] = _markdown_topic(built, fingerprint)
    for defect in defects:
        if defect["topic"] in grouped:
            continue
        built = _isolated(
            str(defect["topic"]),
            _unique_slug(str(defect["topic"]), used),
            str(defect["reason"]),
            fingerprint["source_version"],
            run_id,
        )
        topics.append(built)
        files[str(built["markdown_file"])] = _markdown_topic(built, fingerprint)
    topics.sort(key=lambda item: str(item["topic"]))
    status = "degraded" if defects else "success"
    cards = _cards_document(run_id, status, fingerprint, topics, defects, error=None)
    files["topic_index.md"] = _markdown_index(cards, fingerprint)
    return TopicRender(status, cards, files, _metrics(cards))


def run_render(
    evidence_pack: Mapping[str, object],
    contradictions: Mapping[str, object],
    trends: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    artifacts_dir: Path,
    reports_dir: Path,
    draft: Mapping[str, object] | None = None,
    entities: Sequence[Mapping[str, object]] | None = None,
    store: ObjectStore | None = None,
) -> TopicRender:
    """Write topic Markdown and the R20 JSON artifacts."""
    checked = _run_id(run_id)
    result = render_topics(
        evidence_pack,
        contradictions,
        trends,
        draft=draft,
        entities=entities,
        run_id=checked,
    )
    directory = reports_dir / checked
    written: list[dict[str, object]] = []
    for filename, text in sorted(result.markdown_files.items()):
        path = _topic_path(directory, filename)
        _write_text(path, text)
        written.append(
            {
                "markdown_file": filename,
                "markdown_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "topic": _topic_for_file(result.cards, filename),
                "status": _status_for_file(result.cards, filename),
            }
        )
    object_store = LocalObjectStore(artifacts_dir) if store is None else store
    body = _json_bytes(result.cards)
    object_store.put(f"{checked}/R20/cards.json", body)
    manifest: dict[str, object] = {
        "ticket": "R20",
        "run_id": checked,
        "status": result.status,
        "cards": "cards.json",
        "cards_sha256": hashlib.sha256(body).hexdigest(),
        "source_version": result.cards["source_version"],
        "source_hashes": result.cards["source_hashes"],
        "idempotency_key": _idempotency_key(checked, result.cards),
        "template_version": TEMPLATE_VERSION,
        "prompt_model_version": PROMPT_MODEL_VERSION,
        "model_version": _MODEL_VERSION,
        "generated_by_ai": False,
        "topics": written,
        "defects": result.cards["defects"],
    }
    object_store.put(f"{checked}/R20/manifest.json", _json_bytes(manifest))
    object_store.put(f"{checked}/R20/metrics.json", _json_bytes(result.metrics))
    return result


def _failed(
    run_id: str | None,
    fingerprint: Mapping[str, str],
    error: str,
) -> TopicRender:
    cards = _cards_document(run_id, "schema_error", fingerprint, [], [], error=error)
    files = {"topic_index.md": _markdown_index(cards, fingerprint)}
    return TopicRender("schema_error", cards, files, _metrics(cards))


def _cards_document(
    run_id: str | None,
    status: str,
    fingerprint: Mapping[str, str],
    topics: list[dict[str, object]],
    defects: list[dict[str, object]],
    *,
    error: str | None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "1.0",
        "ticket": "R20",
        "run_id": run_id,
        "status": status,
        "template_version": TEMPLATE_VERSION,
        "prompt_model_version": PROMPT_MODEL_VERSION,
        "model_version": _MODEL_VERSION,
        "generated_by_ai": False,
        "llm_calls": 0,
        "paid_api_calls": 0,
        "source_version": fingerprint["source_version"],
        "source_hashes": {name: fingerprint[name] for name in _INPUT_KEYS},
        "topics": topics,
        "defects": defects,
    }
    if error is not None:
        document["error"] = error
    return document


def _build_topic(
    topic: str,
    slug: str,
    rows: Sequence[Mapping[str, object]],
    pack_trend_ids: Sequence[str],
    pack: Mapping[str, object],
    review: Mapping[str, object],
    draft: Mapping[str, object] | None,
    entities: Sequence[Mapping[str, object]] | None,
    index: Mapping[str, Mapping[str, object]],
    allowed: set[str],
    draft_status: str,
    source_version: str,
    run_id: str | None,
) -> dict[str, object]:
    trend_ids = _trend_ids(rows, pack_trend_ids)
    claims = _claims_for(pack, trend_ids)
    _reject_non_finite(claims)
    blocked = _blocked(review, trend_ids, claims)
    contradicted = _contradicted(review, trend_ids, blocked)
    fact_claims = _fact_claims(draft, draft_status, trend_ids, blocked, index)
    fact_pairs = _fact_pairs(fact_claims)
    has_fact = bool(fact_claims)
    if has_fact:
        topic_status = "success"
    elif contradicted:
        topic_status = "contradicted"
    else:
        topic_status = "insufficient_evidence"
    charts = {
        "7d": _chart(7, rows, claims, blocked, fact_pairs, topic_status),
        "28d": _chart(28, rows, claims, blocked, fact_pairs, topic_status),
    }
    support = _support_cards(topic, claims, fact_claims, blocked, index, allowed)
    contrary = _contrary_cards(topic, review, trend_ids, index, allowed)
    timeline = _timeline(support, contrary, index)
    attached = _entities_for(topic, entities, support, contrary, index)
    summary = _summary(
        draft_status,
        fact_claims,
        draft,
        trend_ids,
        blocked,
        review,
        allowed,
        contradicted,
        has_fact,
    )
    return {
        "topic": topic,
        "slug": slug,
        "markdown_file": f"topic_{slug}.md",
        "run_id": run_id,
        "status": topic_status,
        "trend_ids": trend_ids,
        "summary": summary,
        "charts": charts,
        "support_cards": support,
        "contrary_cards": contrary,
        "timeline": timeline,
        "entities": attached,
        "windows": _windows(rows),
        "source_version": source_version,
        "defect": None,
    }


def _isolated(
    topic: str,
    slug: str,
    reason: str,
    source_version: str,
    run_id: str | None,
) -> dict[str, object]:
    label = reason if _LABEL.fullmatch(reason) else "render_failed"
    empty = _empty_chart("isolated", "isolated")
    return {
        "topic": topic,
        "slug": slug,
        "markdown_file": f"topic_{slug}.md",
        "run_id": run_id,
        "status": "isolated",
        "trend_ids": [],
        "summary": f"标签：isolated。该主题已隔离（{label}）。不作为事实陈述。",
        "charts": {"7d": empty, "28d": dict(empty)},
        "support_cards": [],
        "contrary_cards": [],
        "timeline": [],
        "entities": [],
        "windows": [],
        "source_version": source_version,
        "defect": label,
    }


def _empty_chart(status: str, label: str) -> dict[str, object]:
    return {
        "window_days": 0,
        "status": status,
        "stated_as_fact": False,
        "display": "withheld",
        "withheld_label": label,
        "points": [],
        "aggregates": [],
        "disputed": [],
    }


def _chart(
    window: int,
    rows: Sequence[Mapping[str, object]],
    claims: Sequence[Mapping[str, object]],
    blocked: set[str],
    fact_pairs: set[tuple[str, int | float]],
    topic_status: str,
) -> dict[str, object]:
    explicit = _explicit_points(rows, window)
    active = _active_dates(rows) if window == 28 and explicit is None else None
    named = _named_values(rows, claims, blocked, _COUNT_FIELDS[window])
    disputed = [
        {"name": name, "values": values}
        for name, values in named.items()
        if len(values) > 1
    ]
    aggregates: list[dict[str, object]] = []
    points: list[dict[str, object]] = []
    if explicit is not None:
        points = explicit
        _dispute_series(points, named, disputed)
        status = "stored_series" if points else "insufficient_evidence"
    else:
        for name, values in named.items():
            if len(values) != 1:
                continue
            aggregates.append({"name": name, "value": values[0]})
            dated = _dated_point(name, values[0], rows, claims, blocked)
            if dated is not None:
                points.append(dated)
        if active:
            points.extend({"date": day, "active": True} for day in active)
        if disputed:
            status = "numeric_disagreement"
        elif any("value" in point for point in points):
            status = "stored_window_counts"
        elif active:
            status = "stored_active_dates"
        elif aggregates:
            status = "stored_aggregates"
        else:
            status = "insufficient_evidence"
    points.sort(key=_point_sort)
    stated = _stated_as_fact(points, aggregates, disputed, fact_pairs, topic_status)
    display, label = _display(status, stated, topic_status, points)
    return {
        "window_days": window,
        "status": status,
        "stated_as_fact": stated,
        "display": display,
        "withheld_label": label,
        "points": points,
        "aggregates": aggregates,
        "disputed": disputed,
    }


def _explicit_points(
    rows: Sequence[Mapping[str, object]],
    window: int,
) -> list[dict[str, object]] | None:
    found: list[dict[str, object]] | None = None
    for row in rows:
        payload: object | None = None
        for key in _SERIES_KEYS[window]:
            if key in row:
                payload = row.get(key)
                break
        if payload is None and "series" in row and _window_days(row) == window:
            payload = row.get("series")
        if payload is None:
            continue
        points = _coerce_points(payload)
        if found is None:
            found = points
        elif points != found:
            raise _TopicDefect("series_invalid")
    return found


def _coerce_points(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise _TopicDefect("series_invalid")
    points: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise _TopicDefect("series_invalid")
        date = _day(item.get("date")) or _day(item.get("t")) or _day(item.get("x"))
        if date is None:
            raise _TopicDefect("series_invalid")
        point: dict[str, object] = {"date": date}
        if "name" in item and item.get("name") is not None:
            name = item.get("name")
            if not isinstance(name, str) or _LABEL.fullmatch(name) is None:
                raise _TopicDefect("series_invalid")
            point["name"] = name
        if any(key in item for key in ("value", "y", "count")):
            if "value" in item:
                raw = item.get("value")
            elif "y" in item:
                raw = item.get("y")
            else:
                raw = item.get("count")
            point["value"] = _series_number(raw)
        if "active" in item:
            active = item.get("active")
            if not isinstance(active, bool):
                raise _TopicDefect("series_value_invalid")
            point["active"] = active
        if "value" not in point and "active" not in point:
            raise _TopicDefect("series_invalid")
        points.append(point)
    points.sort(key=_point_sort)
    return points


def _series_number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _TopicDefect("series_value_invalid")
    if isinstance(value, float) and not math.isfinite(value):
        raise _TopicDefect("non_finite_series")
    return value


def _active_dates(rows: Sequence[Mapping[str, object]]) -> list[str] | None:
    found: list[str] | None = None
    for row in rows:
        if "active_dates_28d" not in row:
            continue
        raw = row.get("active_dates_28d")
        if not isinstance(raw, list):
            raise _TopicDefect("series_invalid")
        dates: list[str] = []
        for item in raw:
            day = _day(item)
            if day is None:
                raise _TopicDefect("series_invalid")
            if day not in dates:
                dates.append(day)
        dates.sort()
        if found is None:
            found = dates
        elif found != dates:
            raise _TopicDefect("series_invalid")
    return found


def _named_values(
    rows: Sequence[Mapping[str, object]],
    claims: Sequence[Mapping[str, object]],
    blocked: set[str],
    names: Sequence[str],
) -> dict[str, list[int | float]]:
    found: dict[str, list[int | float]] = {name: [] for name in names}
    sources: list[Mapping[str, object]] = []
    for row in rows:
        sources.append(row)
        breakdown = row.get("breakdown")
        if isinstance(breakdown, Mapping):
            inputs = breakdown.get("inputs")
            if isinstance(inputs, Mapping):
                sources.append(inputs)
    for claim in claims:
        claim_id = _safe_id(claim.get("claim_id"))
        if claim_id is None or claim_id in blocked or claim.get("status") != "supported":
            continue
        inputs = claim.get("inputs")
        if isinstance(inputs, Mapping):
            sources.append(inputs)
    for source in sources:
        for name in names:
            if name not in source:
                continue
            number = _stored_number(source.get(name))
            if number is None:
                continue
            bucket = found[name]
            if number not in bucket:
                bucket.append(number)
    return found


def _dispute_series(
    points: Sequence[Mapping[str, object]],
    named: Mapping[str, Sequence[int | float]],
    disputed: list[dict[str, object]],
) -> None:
    seen = {str(item["name"]) for item in disputed}
    for point in points:
        name = point.get("name")
        value = point.get("value")
        if (
            not isinstance(name, str)
            or not isinstance(value, int | float)
            or isinstance(value, bool)
        ):
            continue
        others = [item for item in named.get(name, ()) if item != value]
        if not others or name in seen:
            continue
        seen.add(name)
        disputed.append({"name": name, "values": sorted([value, *others], key=_number_sort)})


def _dated_point(
    name: str,
    value: int | float,
    rows: Sequence[Mapping[str, object]],
    claims: Sequence[Mapping[str, object]],
    blocked: set[str],
) -> dict[str, object] | None:
    keys = _DATE_KEYS.get(name)
    if keys is None:
        return None
    dates: list[str] = []
    for row in rows:
        for key in keys:
            day = _day(row.get(key))
            if day is not None and day not in dates:
                dates.append(day)
    if "window_start" in keys:
        for claim in claims:
            claim_id = _safe_id(claim.get("claim_id"))
            if claim_id is None or claim_id in blocked:
                continue
            day = _day(claim.get("window_start"))
            if day is not None and day not in dates:
                dates.append(day)
    if len(dates) != 1:
        return None
    return {"date": dates[0], "name": name, "value": value}


def _stated_as_fact(
    points: Sequence[Mapping[str, object]],
    aggregates: Sequence[Mapping[str, object]],
    disputed: Sequence[Mapping[str, object]],
    fact_pairs: set[tuple[str, int | float]],
    topic_status: str,
) -> bool:
    if topic_status != "success" or disputed:
        return False
    numbers: list[tuple[str | None, int | float]] = []
    for point in points:
        value = point.get("value")
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        name = point.get("name")
        numbers.append((name if isinstance(name, str) else None, value))
    for item in aggregates:
        value = item.get("value")
        name = item.get("name")
        if isinstance(name, str) and isinstance(value, int | float) and not isinstance(value, bool):
            numbers.append((name, value))
    if not numbers:
        return False
    fact_values = {value for _name, value in fact_pairs}
    for name, value in numbers:
        if name is None:
            if value not in fact_values:
                return False
        elif (name, value) not in fact_pairs:
            return False
    return True


def _display(
    status: str,
    stated: bool,
    topic_status: str,
    points: Sequence[Mapping[str, object]],
) -> tuple[str, str | None]:
    if stated and any("value" in point for point in points):
        return "facts", None
    if status == "stored_active_dates" and topic_status == "success":
        return "active_dates", None
    if topic_status == "contradicted":
        return "withheld", "contradicted"
    if status == "numeric_disagreement":
        return "withheld", "numeric_disagreement"
    if topic_status == "insufficient_evidence" or status == "insufficient_evidence":
        return "withheld", "insufficient_evidence"
    return "withheld", status


def _support_cards(
    topic: str,
    claims: Sequence[Mapping[str, object]],
    fact_claims: Sequence[Mapping[str, object]],
    blocked: set[str],
    index: Mapping[str, Mapping[str, object]],
    allowed: set[str],
) -> list[dict[str, object]]:
    owners: dict[str, list[str]] = {}
    fact_ids: set[str] = set()
    for claim in fact_claims:
        claim_id = _safe_id(claim.get("source_claim_id")) or _safe_id(claim.get("claim_id"))
        for evidence_id in _strings(claim.get("evidence_ids")):
            fact_ids.add(evidence_id)
            if claim_id is not None:
                owners.setdefault(evidence_id, [])
                if claim_id not in owners[evidence_id]:
                    owners[evidence_id].append(claim_id)
    for claim in claims:
        claim_id = _safe_id(claim.get("claim_id"))
        if claim_id is None or claim_id in blocked or claim.get("status") != "supported":
            continue
        for evidence_id in _strings(claim.get("supporting_evidence_ids")):
            owners.setdefault(evidence_id, [])
            if claim_id not in owners[evidence_id]:
                owners[evidence_id].append(claim_id)
    cards: list[dict[str, object]] = []
    for evidence_id in sorted(owners):
        stated = evidence_id in fact_ids and evidence_id in index
        cards.append(
            _card(
                "support",
                topic,
                evidence_id,
                owners[evidence_id],
                index,
                allowed,
                stated=stated,
                label="support" if stated else "insufficient_evidence",
                kept=None,
                reason=None,
            )
        )
    return cards


def _contrary_cards(
    topic: str,
    review: Mapping[str, object],
    trend_ids: Sequence[str],
    index: Mapping[str, Mapping[str, object]],
    allowed: set[str],
) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in _mappings(review.get("contradictions")):
        if _safe_id(item.get("trend_id")) not in trend_ids:
            continue
        evidence_id = _safe_id(item.get("evidence_id"))
        claim_id = _safe_id(item.get("claim_id"))
        if evidence_id is None or ( "contrary", evidence_id) in seen:
            continue
        seen.add(("contrary", evidence_id))
        label = "contradicted" if item.get("independent") is not False else "not_independent"
        cards.append(
            _card(
                "contrary",
                topic,
                evidence_id,
                [claim_id] if claim_id is not None else [],
                index,
                allowed,
                stated=False,
                label=label,
                kept=None,
                reason=None,
            )
        )
    for item in _mappings(review.get("reprints")):
        if _safe_id(item.get("trend_id")) not in trend_ids:
            continue
        evidence_id = _safe_id(item.get("evidence_id"))
        if evidence_id is None or ("reprint", evidence_id) in seen:
            continue
        seen.add(("reprint", evidence_id))
        claim_id = _safe_id(item.get("claim_id"))
        cards.append(
            _card(
                "reprint",
                topic,
                evidence_id,
                [claim_id] if claim_id is not None else [],
                index,
                allowed,
                stated=False,
                label="reprint",
                kept=_safe_id(item.get("kept_evidence_id")),
                reason=None,
            )
        )
    for item in _mappings(review.get("qa_queue")):
        if _safe_id(item.get("trend_id")) not in trend_ids:
            continue
        evidence_id = _safe_id(item.get("evidence_id"))
        qa_id = _safe_id(item.get("qa_id"))
        if evidence_id is None or qa_id is None or ("qa", qa_id) in seen:
            continue
        seen.add(("qa", qa_id))
        reason = _label(item.get("reason")) or "unspecified"
        card = _card(
            "qa",
            topic,
            evidence_id,
            [qa_id],
            index,
            allowed,
            stated=False,
            label="qa",
            kept=None,
            reason=reason,
        )
        cards.append(card)
    cards.sort(key=lambda item: (str(item["role"]), str(item["evidence_id"]), str(item["card_id"])))
    return cards


def _card(
    role: str,
    topic: str,
    evidence_id: str,
    claim_ids: Sequence[str],
    index: Mapping[str, Mapping[str, object]],
    allowed: set[str],
    *,
    stated: bool,
    label: str,
    kept: str | None,
    reason: str | None,
) -> dict[str, object]:
    item = index.get(evidence_id)
    excerpt: str | None = None
    excerpt_status = "withheld"
    excerpt_reason = "evidence_not_in_pack"
    source_url: str | None = None
    published: str | None = None
    collected: str | None = None
    license_status: str | None = None
    quality_tier: str | None = None
    digest: str | None = None
    family: str | None = None
    if item is not None:
        source_url = str(item["source_url"])
        published = _text(item.get("source_published_at"))
        collected = _text(item.get("collected_at"))
        license_status = _text(item.get("license_status"))
        quality_tier = _text(item.get("quality_tier"))
        digest = _text(item.get("sha256"))
        family = _text(item.get("source_family"))
        excerpt, excerpt_status, excerpt_reason = _excerpt(item, allowed)
    card: dict[str, object] = {
        "card_id": _digest(
            f"{role}\n{topic}\n{evidence_id}\n{kept or ''}\n{reason or ''}",
            "card_",
        ),
        "role": role,
        "topic": topic,
        "trend_ids": [],
        "claim_ids": sorted(claim_ids),
        "evidence_id": evidence_id,
        "source_url": source_url,
        "source_published_at": published,
        "collected_at": collected,
        "license_status": license_status,
        "quality_tier": quality_tier,
        "sha256": digest,
        "source_family": family,
        "excerpt": excerpt,
        "excerpt_status": excerpt_status,
        "excerpt_reason": excerpt_reason,
        "stated_as_fact": stated and excerpt_reason != "evidence_not_in_pack",
        "label": "evidence_not_in_pack" if item is None and role == "support" else label,
    }
    if item is None and role == "support":
        card["stated_as_fact"] = False
    if kept is not None:
        card["kept_evidence_id"] = kept
    if reason is not None:
        card["reason"] = reason
    return card


def _excerpt(
    item: Mapping[str, object],
    allowed: set[str],
) -> tuple[str | None, str, str]:
    license_status = _text(item.get("license_status")) or ""
    excerpt = item.get("normalized_excerpt")
    if license_status not in _EXCERPT_LICENSES:
        reason = license_status if _LABEL.fullmatch(license_status) else "license_not_public"
        return None, "withheld", reason
    span_missing = item.get("citation_span_missing") is True
    span = item.get("citation_span")
    if span_missing or not isinstance(span, Mapping):
        return None, "withheld", "span_missing"
    if not isinstance(span, Mapping):
        return None, "withheld", "span_missing"
    start = span.get("start")
    end = span.get("end")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or not isinstance(excerpt, str)
        or excerpt == ""
    ):
        return None, "withheld", "span_missing"
    quote_length = item.get("quote_length")
    if (
        isinstance(quote_length, bool)
        or not isinstance(quote_length, int)
        or quote_length != len(excerpt)
        or end - start != len(excerpt)
    ):
        return None, "withheld", "quote_length_mismatch"
    limit = MAX_FULLTEXT_EXCERPT if license_status == "fulltext_allowed" else MAX_REUSABLE_EXCERPT
    if len(excerpt) > limit:
        return None, "withheld", "excerpt_too_long"
    if _foreign_url(excerpt, allowed):
        return None, "withheld", "foreign_url"
    return excerpt, "shown", "shown"


def _timeline(
    support: Sequence[Mapping[str, object]],
    contrary: Sequence[Mapping[str, object]],
    index: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for card in (*support, *contrary):
        evidence_id = str(card["evidence_id"])
        item = index.get(evidence_id)
        if item is None:
            continue
        stamp = _text(item.get("source_published_at")) or _text(item.get("collected_at"))
        if stamp is None:
            continue
        events.append(
            {
                "at": stamp,
                "role": card["role"],
                "evidence_id": evidence_id,
                "source_url": card.get("source_url"),
                "label": card["label"],
                "stated_as_fact": card["stated_as_fact"],
            }
        )
    events.sort(
        key=lambda item: (
            str(item["at"]),
            _ROLE_ORDER.get(str(item["role"]), 9),
            str(item["evidence_id"]),
            str(item["label"]),
        )
    )
    return events


def _entities_for(
    topic: str,
    entities: Sequence[Mapping[str, object]] | None,
    support: Sequence[Mapping[str, object]],
    contrary: Sequence[Mapping[str, object]],
    index: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    if entities is None:
        return []
    linked: set[str] = set()
    for card in (*support, *contrary):
        evidence_id = str(card["evidence_id"])
        item = index.get(evidence_id)
        if item is None:
            continue
        canonical = _text(item.get("canonical_id"))
        if canonical is not None:
            linked.add(canonical)
    rows: list[dict[str, object]] = []
    for entity in entities:
        if not isinstance(entity, Mapping):
            continue
        if not _entity_matches(entity, topic, linked):
            continue
        status = _text(entity.get("resolution_status")) or "unresolved"
        label = "resolved" if status == "resolved" else "unresolved"
        rows.append(
            {
                "canonical_id": _text(entity.get("canonical_id")) or "",
                "entity_type": _text(entity.get("entity_type")) or "",
                "display_name": _text(entity.get("display_name")) or "",
                "normalized_key": _text(entity.get("normalized_key")) or "",
                "resolution_status": status,
                "label": label,
            }
        )
    rows.sort(key=lambda item: (str(item["canonical_id"]), str(item["display_name"])))
    return rows


def _entity_matches(
    entity: Mapping[str, object],
    topic: str,
    linked: set[str],
) -> bool:
    canonical = _text(entity.get("canonical_id"))
    if canonical is not None and canonical in linked:
        return True
    for key in ("display_name", "normalized_key"):
        if _text(entity.get(key)) == topic:
            return True
    aliases = entity.get("aliases")
    if isinstance(aliases, list):
        return any(item == topic for item in aliases if isinstance(item, str))
    return False


def _summary(
    draft_status: str,
    fact_claims: Sequence[Mapping[str, object]],
    draft: Mapping[str, object] | None,
    trend_ids: Sequence[str],
    blocked: set[str],
    review: Mapping[str, object],
    allowed: set[str],
    contradicted: bool,
    has_fact: bool,
) -> str:
    if draft_status in {"no_material_update", "schema_error", "invalid"}:
        return f"标签：{draft_status}。不作为事实陈述。"
    lines: list[str] = []
    if draft_status == "success":
        for claim in fact_claims:
            text = _sanitize(_text(claim.get("text")) or "", allowed)
            kind = _label(claim.get("kind")) or "fact"
            lines.append(f"[{kind}] {text}")
        lines.extend(_blocked_lines(draft, trend_ids, blocked, allowed))
    lines.extend(_chain_lines(review, trend_ids))
    if draft is not None:
        lines.extend(_omission_lines(draft, trend_ids))
    if not lines:
        if contradicted and not has_fact:
            return "标签：contradicted。不作为事实陈述。"
        return "标签：insufficient_evidence。不作为事实陈述。"
    deduped: list[str] = []
    for line in lines:
        if line not in deduped:
            deduped.append(line)
    return "\n".join(deduped)


def _blocked_lines(
    draft: Mapping[str, object] | None,
    trend_ids: Sequence[str],
    blocked: set[str],
    allowed: set[str],
) -> list[str]:
    if draft is None:
        return []
    lines: list[str] = []
    for claim in _mappings(draft.get("claims")):
        if _safe_id(claim.get("trend_id")) not in trend_ids:
            continue
        source = _safe_id(claim.get("source_claim_id")) or _safe_id(claim.get("claim_id"))
        if source is None or source not in blocked:
            continue
        kind = _label(claim.get("kind")) or ""
        if kind in {"counter", "unknown", "hypothesis"}:
            text = _sanitize(_text(claim.get("text")) or "", allowed)
            lines.append(f"[{kind}] {text}")
            continue
        lines.append(f"标签：contradicted。主张 {source} 不作为事实陈述。")
    return lines


def _chain_lines(review: Mapping[str, object], trend_ids: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for chain in _mappings(review.get("chains")):
        if _safe_id(chain.get("trend_id")) not in trend_ids:
            continue
        status = _label(chain.get("status"))
        claim_id = _safe_id(chain.get("claim_id")) or "未编号"
        if status == "conflict":
            lines.append(f"标签：contradicted。主张 {claim_id} 不作为事实陈述。")
        elif status == "contrary_search_incomplete":
            lines.append("标签：contrary_search_incomplete。不作为已无反证。")
        elif status == "counterevidence_not_found":
            lines.append("标签：counterevidence_not_found。不能据此认为不存在反证。")
    return lines


def _omission_lines(draft: Mapping[str, object], trend_ids: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for item in _mappings(draft.get("omissions")):
        if _safe_id(item.get("trend_id")) not in trend_ids:
            continue
        reason = _label(item.get("reason")) or "unspecified"
        claim_id = _safe_id(item.get("claim_id")) or "未编号"
        lines.append(f"标签：{reason}。主张 {claim_id} 不作为事实陈述。")
    return lines


def _fact_claims(
    draft: Mapping[str, object] | None,
    draft_status: str,
    trend_ids: Sequence[str],
    blocked: set[str],
    index: Mapping[str, Mapping[str, object]],
) -> list[Mapping[str, object]]:
    if draft is None or draft_status != "success":
        return []
    facts: list[Mapping[str, object]] = []
    for claim in _mappings(draft.get("claims")):
        if _label(claim.get("kind")) != "fact":
            continue
        if _safe_id(claim.get("trend_id")) not in trend_ids:
            continue
        source = _safe_id(claim.get("source_claim_id")) or _safe_id(claim.get("claim_id"))
        if source is None or source in blocked:
            continue
        evidence_ids = _strings(claim.get("evidence_ids"))
        if not any(evidence_id in index for evidence_id in evidence_ids):
            continue
        facts.append(claim)
    facts.sort(key=lambda item: str(item.get("source_claim_id") or item.get("claim_id") or ""))
    return facts


def _fact_pairs(claims: Sequence[Mapping[str, object]]) -> set[tuple[str, int | float]]:
    pairs: set[tuple[str, int | float]] = set()
    for claim in claims:
        for number in _mappings(claim.get("numbers")):
            name = _label(number.get("name"))
            value = _stored_number(number.get("value"))
            if name is None or value is None:
                continue
            pairs.add((name, value))
    return pairs


def _blocked(
    review: Mapping[str, object],
    trend_ids: Sequence[str],
    claims: Sequence[Mapping[str, object]],
) -> set[str]:
    blocked: set[str] = set()
    major: set[str] = set()
    for candidate in _mappings(review.get("candidates")):
        trend_id = _safe_id(candidate.get("trend_id"))
        if trend_id is None or trend_id not in trend_ids:
            continue
        if candidate.get("major_conflict") is True:
            major.add(trend_id)
    for claim in claims:
        claim_id = _safe_id(claim.get("claim_id"))
        trend_id = _safe_id(claim.get("trend_id"))
        if claim_id is not None and trend_id in major:
            blocked.add(claim_id)
    for key in ("contradictions", "qa_queue"):
        for item in _mappings(review.get(key)):
            if _safe_id(item.get("trend_id")) not in trend_ids:
                continue
            claim_id = _safe_id(item.get("claim_id"))
            if claim_id is not None:
                blocked.add(claim_id)
    for chain in _mappings(review.get("chains")):
        if _safe_id(chain.get("trend_id")) not in trend_ids:
            continue
        if _label(chain.get("status")) != "conflict":
            continue
        claim_id = _safe_id(chain.get("claim_id"))
        if claim_id is not None:
            blocked.add(claim_id)
    return blocked


def _contradicted(
    review: Mapping[str, object],
    trend_ids: Sequence[str],
    blocked: set[str],
) -> bool:
    if blocked:
        return True
    for candidate in _mappings(review.get("candidates")):
        if _safe_id(candidate.get("trend_id")) not in trend_ids:
            continue
        if candidate.get("major_conflict") is True or candidate.get("downgraded") is True:
            return True
    return False


def _group_topics(
    trends: Sequence[Mapping[str, object]],
    pack: Mapping[str, object],
) -> tuple[dict[str, list[Mapping[str, object]]], dict[str, list[str]], list[dict[str, object]]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    defects: list[dict[str, object]] = []
    for row in trends:
        if not isinstance(row, Mapping):
            defects.append({"topic": "invalid_topic", "reason": "topic_missing"})
            continue
        topic = _topic_name(row.get("topic")) or _topic_name(row.get("topic_id"))
        if topic is None:
            label = _safe_id(row.get("trend_id")) or "invalid_topic"
            defects.append({"topic": label, "reason": "topic_missing"})
            continue
        grouped.setdefault(topic, []).append(row)
    pack_ids: dict[str, list[str]] = {}
    for row in _mappings(pack.get("trends")):
        topic = _topic_name(row.get("topic")) or _topic_name(row.get("topic_id"))
        trend_id = _safe_id(row.get("trend_id"))
        if topic is None or trend_id is None:
            continue
        grouped.setdefault(topic, [])
        bucket = pack_ids.setdefault(topic, [])
        if trend_id not in bucket:
            bucket.append(trend_id)
    return grouped, pack_ids, defects


def _trend_ids(
    rows: Sequence[Mapping[str, object]],
    pack_ids: Sequence[str],
) -> list[str]:
    found: list[str] = []
    for row in rows:
        trend_id = _safe_id(row.get("trend_id"))
        if trend_id is not None and trend_id not in found:
            found.append(trend_id)
    for trend_id in pack_ids:
        if trend_id not in found:
            found.append(trend_id)
    return sorted(found)


def _claims_for(
    pack: Mapping[str, object],
    trend_ids: Sequence[str],
) -> list[Mapping[str, object]]:
    return [
        claim
        for claim in _mappings(pack.get("claims"))
        if _safe_id(claim.get("trend_id")) in trend_ids
    ]


def _windows(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    windows: list[dict[str, object]] = []
    for row in rows:
        window = _window_days(row)
        if window is None:
            continue
        score = _stored_number(row.get("score"))
        confidence = _label(row.get("confidence"))
        windows.append(
            {
                "window_days": window,
                "trend_id": _safe_id(row.get("trend_id")) or "",
                "confidence": confidence,
                "score": score,
                "stated_as_fact": False,
            }
        )
    windows.sort(key=lambda item: (int(item["window_days"]), str(item["trend_id"])))
    return windows


def _markdown_topic(topic: Mapping[str, object], fingerprint: Mapping[str, str]) -> str:
    lines = [
        f"# 专题档案：{_escape_md(str(topic['topic']))}",
        "",
        *_version_lines(fingerprint, topic),
        "",
    ]
    defect = topic.get("defect")
    if isinstance(defect, str) and _LABEL.fullmatch(defect):
        lines.extend([f"- 缺陷：{defect}", ""])
    lines.extend(
        [
            "## 摘要",
            "",
        ]
    )
    summary = str(topic.get("summary") or "")
    if summary:
        for line in summary.split("\n"):
            lines.extend([_escape_md(line), ""])
    else:
        lines.extend(["标签：insufficient_evidence。不作为事实陈述。", ""])
    lines.extend(["## 趋势图数据", ""])
    charts = topic.get("charts")
    chart_map = charts if isinstance(charts, Mapping) else {}
    for key, title in (("7d", "7 日"), ("28d", "28 日")):
        lines.extend([f"### {title}", ""])
        chart = chart_map.get(key)
        if isinstance(chart, Mapping):
            lines.extend(_markdown_chart(chart))
        else:
            lines.extend(["- 标签：insufficient_evidence。不作为事实陈述。", ""])
    lines.extend(["## 支持证据卡", ""])
    lines.extend(_markdown_cards(topic.get("support_cards"), empty="无。"))
    lines.extend(["## 反证卡", ""])
    lines.extend(_markdown_cards(topic.get("contrary_cards"), empty="无。"))
    lines.extend(["## 时间线", ""])
    lines.extend(_markdown_timeline(topic.get("timeline")))
    lines.extend(["## 实体", ""])
    lines.extend(_markdown_entities(topic.get("entities")))
    lines.extend(["## 方法", "", _METHOD, ""])
    return "\n".join(lines)


def _markdown_chart(chart: Mapping[str, object]) -> list[str]:
    display = chart.get("display")
    lines: list[str] = []
    if display == "facts":
        for point in _mappings(chart.get("points")):
            if "value" not in point:
                continue
            name = point.get("name")
            value = point.get("value")
            if not isinstance(value, int | float) or isinstance(value, bool):
                continue
            label = _label(name) or "value"
            lines.append(f"- {_escape_md(str(point.get('date')))}：{label}={_format_number(value)}")
    elif display == "active_dates":
        for point in _mappings(chart.get("points")):
            if point.get("active") is True:
                lines.append(
                    f"- {_escape_md(str(point.get('date')))}：active（已存储日期，不是计数）"
                )
    else:
        label = _label(chart.get("withheld_label")) or "insufficient_evidence"
        lines.append(f"- 标签：{label}。不作为事实陈述。")
    if not lines:
        lines.append("- 标签：insufficient_evidence。不作为事实陈述。")
    lines.append("")
    return lines


def _markdown_cards(value: object, *, empty: str) -> list[str]:
    cards = _mappings(value)
    if not cards:
        return [empty, ""]
    lines: list[str] = []
    for card in cards:
        lines.extend(_markdown_card(card))
    return lines


def _markdown_card(card: Mapping[str, object]) -> list[str]:
    label = _label(card.get("label")) or "unspecified"
    evidence_id = str(card.get("evidence_id") or "")
    lines = [f"- 标签：{_card_sentence(label, card)}", f"- 证据 {_code(evidence_id)}"]
    url = card.get("source_url")
    if isinstance(url, str) and url:
        lines.append(f"  - source_url：{_code(url)}")
    published = card.get("source_published_at")
    if isinstance(published, str) and published:
        lines.append(f"  - 日期：{_escape_md(published)}")
    license_status = card.get("license_status")
    if isinstance(license_status, str) and license_status:
        lines.append(f"  - 许可：{_escape_md(license_status)}")
    if card.get("excerpt_status") == "shown" and isinstance(card.get("excerpt"), str):
        lines.append(f"  - 摘录：{_escape_md(str(card['excerpt']))}")
    else:
        reason = _label(card.get("excerpt_reason")) or "withheld"
        lines.append(f"  - 摘录：已省略（{reason}）")
    kept = card.get("kept_evidence_id")
    if isinstance(kept, str) and kept:
        lines.append(f"  - 保留证据 {_code(kept)}")
    lines.append("")
    return lines


def _card_sentence(label: str, card: Mapping[str, object]) -> str:
    if label == "support" and card.get("stated_as_fact") is True:
        return "support"
    if label == "reprint":
        return "reprint。同源转载，不作为独立反证，也不作为新的事实。"
    if label == "contradicted":
        return "contradicted。不作为事实陈述。"
    if label == "qa":
        reason = _label(card.get("reason")) or "unspecified"
        return f"qa。原因 {reason}。不作为事实陈述。"
    if label == "evidence_not_in_pack":
        return "evidence_not_in_pack。不作为事实陈述。"
    if label == "not_independent":
        return "not_independent。不作为独立反证，也不作为事实。"
    return f"{label}。不作为事实陈述。"


def _markdown_timeline(value: object) -> list[str]:
    events = _mappings(value)
    if not events:
        return ["无。", ""]
    lines: list[str] = []
    for event in events:
        evidence_id = str(event.get("evidence_id") or "")
        url = event.get("source_url")
        url_text = _code(url) if isinstance(url, str) and url else "无 URL"
        label = _label(event.get("label")) or "unspecified"
        stamp = _escape_md(str(event.get("at") or ""))
        if label == "support" and event.get("stated_as_fact") is True:
            lines.append(f"- {stamp} support {_code(evidence_id)} {url_text}")
        else:
            sentence = _card_sentence(label, event)
            lines.append(f"- {stamp} 标签：{sentence} 证据 {_code(evidence_id)} {url_text}")
    lines.append("")
    return lines


def _markdown_entities(value: object) -> list[str]:
    entities = _mappings(value)
    if not entities:
        return ["无。", ""]
    lines: list[str] = []
    for entity in entities:
        lines.append(f"- {_code(str(entity.get('canonical_id') or ''))}")
        lines.append(f"  - 显示名：{_escape_md(str(entity.get('display_name') or ''))}")
        lines.append(f"  - 类型：{_escape_md(str(entity.get('entity_type') or ''))}")
        if entity.get("label") == "resolved":
            lines.append("  - 解析状态：resolved")
        else:
            lines.append("  - 解析状态：unresolved。未解析实体不作为已确认身份。")
        lines.append("")
    return lines


def _markdown_index(cards: Mapping[str, object], fingerprint: Mapping[str, str]) -> str:
    status = str(cards.get("status") or "")
    lines = [
        "# 专题清单",
        "",
        *_version_lines(fingerprint, cards),
        "",
    ]
    if status == "schema_error":
        lines.extend(["输入未通过结构检查，未生成专题卡片。", ""])
    elif status == "degraded":
        lines.extend(["周报降级为文本清单。失败的主题已隔离，并已标识缺陷。", ""])
    lines.append("## 主题")
    lines.append("")
    topics = _mappings(cards.get("topics"))
    if not topics:
        lines.append("无主题。")
    for topic in topics:
        filename = str(topic.get("markdown_file") or "")
        name = _escape_md(str(topic.get("topic") or ""))
        topic_status = str(topic.get("status") or "")
        defect = topic.get("defect")
        if topic_status == "isolated" and isinstance(defect, str):
            lines.append(f"- {name}：缺陷 {defect}")
        else:
            lines.append(f"- {name}：{filename}（{topic_status}）")
    lines.append("")
    return "\n".join(lines)


def _version_lines(fingerprint: Mapping[str, str], document: Mapping[str, object]) -> list[str]:
    lines = [
        f"- 模板版本：{TEMPLATE_VERSION}",
        f"- 提示词/模型版本：{PROMPT_MODEL_VERSION}",
        f"- 状态：{document.get('status')}",
        f"- run_id：{_escape_md(_run_label(document.get('run_id')))}",
        f"- source_version：{fingerprint['source_version']}",
    ]
    for name in _INPUT_KEYS:
        lines.append(f"- {name}：{fingerprint[name]}")
    return lines


def _metrics(cards: Mapping[str, object]) -> dict[str, object]:
    topics = _mappings(cards.get("topics"))
    support = 0
    contrary = 0
    events = 0
    charts = 0
    for topic in topics:
        support += len(_mappings(topic.get("support_cards")))
        contrary += len(_mappings(topic.get("contrary_cards")))
        events += len(_mappings(topic.get("timeline")))
        chart_map = topic.get("charts")
        if isinstance(chart_map, Mapping):
            for chart in chart_map.values():
                if isinstance(chart, Mapping) and (
                    _mappings(chart.get("points")) or _mappings(chart.get("aggregates"))
                ):
                    charts += 1
    metrics: dict[str, object] = {
        "status": cards.get("status"),
        "template_version": TEMPLATE_VERSION,
        "prompt_model_version": PROMPT_MODEL_VERSION,
        "model_version": _MODEL_VERSION,
        "generated_by_ai": False,
        "topics": len(topics),
        "isolated_topics": sum(1 for topic in topics if topic.get("status") == "isolated"),
        "insufficient_topics": sum(
            1 for topic in topics if topic.get("status") == "insufficient_evidence"
        ),
        "contradicted_topics": sum(1 for topic in topics if topic.get("status") == "contradicted"),
        "support_cards": support,
        "contrary_cards": contrary,
        "timeline_events": events,
        "charts": charts,
        "defects": len(_mappings(cards.get("defects"))),
        "llm_calls": 0,
        "paid_api_calls": 0,
        "third_party_writes": 0,
        "source_version": cards.get("source_version"),
    }
    error = cards.get("error")
    if isinstance(error, str):
        metrics["error"] = error
    return metrics


def _evidence_index(pack: Mapping[str, object]) -> dict[str, dict[str, object]]:
    index: dict[str, dict[str, object]] = {}
    for item in _mappings(pack.get("evidence")):
        evidence_id = _safe_id(item.get("evidence_id"))
        if evidence_id is None or evidence_id in index:
            continue
        payload = {key: item.get(key) for key in _EVIDENCE_FIELDS}
        try:
            parsed = EvidenceItem.model_validate(payload)
        except ValidationError:
            continue
        published = item.get("source_published_at")
        collected = item.get("collected_at")
        if not isinstance(published, str) or not isinstance(collected, str):
            continue
        index[evidence_id] = {
            "evidence_id": parsed.evidence_id,
            "canonical_id": parsed.canonical_id,
            "source_family": parsed.source_family.value,
            "source_url": parsed.source_url,
            "source_published_at": published,
            "collected_at": collected,
            "license_status": parsed.license_status.value,
            "quality_tier": parsed.quality_tier.value,
            "sha256": parsed.sha256,
            "normalized_excerpt": item.get("normalized_excerpt"),
            "quote_length": item.get("quote_length"),
            "citation_span": item.get("citation_span"),
            "citation_span_missing": item.get("citation_span_missing") is True,
        }
    return index


def _structure_error(
    evidence_pack: object,
    contradictions: object,
    trends: object,
    draft: object,
    entities: object,
) -> str | None:
    if not isinstance(evidence_pack, Mapping):
        return "evidence_pack"
    if _version(evidence_pack) not in {None, "1.0"}:
        return "evidence_pack_schema_version"
    for key in ("trends", "claims", "evidence"):
        if not isinstance(evidence_pack.get(key), list):
            return key
    if not isinstance(contradictions, Mapping):
        return "contradictions"
    if _version(contradictions) not in {None, "1.0"}:
        return "contradictions_schema_version"
    for key in ("candidates", "chains", "contradictions"):
        if not isinstance(contradictions.get(key), list):
            return key
    for key in ("reprints", "qa_queue"):
        value = contradictions.get(key)
        if value is not None and not isinstance(value, list):
            return key
    if isinstance(trends, str | bytes) or not isinstance(trends, Sequence):
        return "trends"
    if any(not isinstance(item, Mapping) for item in trends):
        return "trends"
    if draft is not None:
        if not isinstance(draft, Mapping):
            return "draft"
        if _version(draft) not in {None, "1.0"}:
            return "draft_schema_version"
        for key in ("claims", "omissions"):
            value = draft.get(key)
            if value is not None and not isinstance(value, list):
                return "draft"
    if entities is not None:
        if isinstance(entities, str | bytes) or not isinstance(entities, Sequence):
            return "entities"
        if any(not isinstance(item, Mapping) for item in entities):
            return "entities"
    return None


def _hash_input(value: object) -> str:
    body = json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _json_ready(value: object) -> object:
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return "non_finite"
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_ready(item) for item in value]
    return type(value).__name__


def _reject_non_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise _TopicDefect("non_finite_series")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, tuple | list):
        for item in value:
            _reject_non_finite(item)


def _foreign_url(text: str, allowed: set[str]) -> bool:
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;，。)")
        if url not in allowed:
            return True
    return False


def _sanitize(text: str, allowed: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip(".,;，。)")
        suffix = raw[len(trimmed) :]
        if trimmed in allowed:
            return trimmed + suffix
        return "［链接已省略］"

    cleaned = _URL.sub(replace, text)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
    return cleaned.replace("\r", " ").replace("\n", " ")


def _escape_md(text: str) -> str:
    return "".join(_MD_ESCAPE.get(char, char) for char in text)


def _code(value: str) -> str:
    if "`" in value or "\n" in value:
        return "［已省略］"
    return f"`{value}`"


def _idempotency_key(run_id: str, cards: Mapping[str, object]) -> str:
    hashes = cards.get("source_hashes")
    values: list[str] = []
    if isinstance(hashes, Mapping):
        values = [str(hashes[name]) for name in sorted(hashes)]
    body = "|".join(["R20", run_id, *values])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _topic_for_file(cards: Mapping[str, object], filename: str) -> str:
    if filename == "topic_index.md":
        return ""
    for topic in _mappings(cards.get("topics")):
        if topic.get("markdown_file") == filename:
            return str(topic.get("topic") or "")
    return ""


def _status_for_file(cards: Mapping[str, object], filename: str) -> str:
    if filename == "topic_index.md":
        return str(cards.get("status") or "")
    for topic in _mappings(cards.get("topics")):
        if topic.get("markdown_file") == filename:
            return str(topic.get("status") or "")
    return ""


def _point_sort(point: Mapping[str, object]) -> tuple[str, str, str]:
    value = point.get("value")
    rendered = "" if value is None else str(value)
    return (str(point.get("date") or ""), str(point.get("name") or ""), rendered)


def _number_sort(value: int | float) -> tuple[int, float]:
    return (0 if isinstance(value, int) else 1, float(value))


def _format_number(value: int | float) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return format(float(value), ".10g")


def _window_days(row: Mapping[str, object]) -> int | None:
    value = row.get("window_days")
    if value in (7, 28):
        return 7 if value == 7 else 28
    return None


def _day(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if _DAY.fullmatch(value):
        return value
    if len(value) >= 11 and value[10] in "T " and _DAY.fullmatch(value[:10]):
        return value[:10]
    return None


def _stored_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _version(value: Mapping[str, object]) -> str | None:
    stored = value.get("schema_version")
    if stored is None:
        return None
    return stored if isinstance(stored, str) else "invalid"


def _draft_status(draft: Mapping[str, object] | None) -> str:
    if draft is None:
        return "missing"
    status = draft.get("status")
    if isinstance(status, str) and _LABEL.fullmatch(status):
        return status
    return "invalid"


def _safe_id(value: object) -> str | None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        return None
    return value


def _label(value: object) -> str | None:
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None:
        return None
    return value


def _topic_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > 200 or stripped in {".", ".."}:
        return None
    if any(char in stripped for char in "/\\"):
        return None
    if any(ord(char) < 32 for char in stripped):
        return None
    return stripped


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped == "":
        return None
    return stripped


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and _ID.fullmatch(item)]


def _mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            rows.append({str(key): entry for key, entry in item.items()})
    return rows


def _digest(value: str, prefix: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _run_label(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _unique_slug(topic: str, used: set[str]) -> str:
    if _SLUG.fullmatch(topic):
        base = topic
    else:
        base = hashlib.sha256(topic.encode("utf-8")).hexdigest()[:20]
    if base == "index":
        base = "index-topic"
    slug = base
    if slug in used:
        suffix = hashlib.sha256(topic.encode("utf-8")).hexdigest()[:8]
        slug = f"{base}-{suffix}"[:80]
    used.add(slug)
    return slug


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."} or any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _topic_path(directory: Path, filename: str) -> Path:
    if filename != Path(filename).name or not filename.endswith(".md"):
        raise ValueError("invalid topic filename")
    path = directory / filename
    if path.parent != directory:
        raise ValueError("invalid topic path")
    return path


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(text.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
