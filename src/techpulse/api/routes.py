"""Read-only report, trend, evidence, and status routes.

A row is public only when a stored report has a Q10 manifest with
``qa_pass`` and ``signed`` both true and names that row. Unknown and blocked
licences stay out. Excerpts are capped so the raw record body is never the
response. Query connections use SQLite ``mode=ro`` and ``query_only``.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from techpulse.api.auth import (
    SCHEMA_VERSION,
    ApiError,
    apply_rate_limit,
    request_id_of,
    require_internal,
    require_reader,
    settings_of,
    sign_cursor,
    store_of,
    verify_cursor,
)
from techpulse.contracts import HTTPS_URL_PATTERN, SHA256_PATTERN
from techpulse.workflows.activities import ObjectStore

UTC_TEXT = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_LIMIT = re.compile(r"^[1-9][0-9]*$")
MAX_REPORTS = 200
MAX_IDS = 500
MAX_TOPIC = 200
MAX_ID = 256
EXCERPT_SHORT = 280
EXCERPT_FULL = 2000
MAX_CONTENT = 100_000
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
_PUBLIC_LICENSES = frozenset({"metadata_only", "reusable_excerpt", "fulltext_allowed"})
_KINDS = frozenset({"daily", "weekly", "topic"})
_WINDOWS = frozenset({7, 28})
_CONFIDENCE = frozenset({"high", "medium", "low"})
_FAMILIES = frozenset({"academic", "code", "community", "news"})
_TABLES = (
    "published_reports",
    "trend_scores",
    "evidence_items",
    "trend_evidence",
    "raw_records",
)
PROM_MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ErrorBody(_Model):
    code: str
    message: str
    request_id: str
    as_of: str = Field(pattern=UTC_TEXT)
    schema_version: Literal["1.0"]


class LiveResponse(_Model):
    status: Literal["ok"]
    as_of: str = Field(pattern=UTC_TEXT)
    schema_version: Literal["1.0"]


class ReadyResponse(_Model):
    status: Literal["ok", "unavailable"]
    checks: dict[str, Literal["ok", "fail"]]
    as_of: str = Field(pattern=UTC_TEXT)
    schema_version: Literal["1.0"]


class TrendItem(_Model):
    trend_id: str
    topic: str
    window_days: Literal[7, 28]
    score: float
    confidence: Literal["high", "medium", "low"]


class TrendListResponse(_Model):
    items: list[TrendItem]
    next_cursor: str | None
    as_of: str = Field(pattern=UTC_TEXT)
    schema_version: Literal["1.0"]


class TrendBody(_Model):
    trend_id: str
    topic: str
    window_days: Literal[7, 28]
    score: float
    confidence: Literal["high", "medium", "low"]
    source_families: list[str]
    claim: str
    supporting_evidence_ids: list[str]
    contrary_evidence_ids: list[str]
    numeric_checks_passed: bool
    last_verified_at: str = Field(pattern=UTC_TEXT)


class TrendDetailResponse(_Model):
    trend: TrendBody
    features: dict[str, Any]
    evidence_ids: list[str]
    contrary_ids: list[str]
    as_of: str = Field(pattern=UTC_TEXT)
    schema_version: Literal["1.0"]


class EvidenceDates(_Model):
    published_at: str | None = None
    collected_at: str = Field(pattern=UTC_TEXT)


class EvidenceResponse(_Model):
    source_url: str = Field(pattern=HTTPS_URL_PATTERN)
    excerpt: str
    license: Literal["metadata_only", "reusable_excerpt", "fulltext_allowed"]
    dates: EvidenceDates
    hash: str = Field(pattern=SHA256_PATTERN)
    as_of: str = Field(pattern=UTC_TEXT)
    schema_version: Literal["1.0"]


class ReportItem(_Model):
    report_id: str
    report_kind: Literal["daily", "weekly", "topic"]
    published_at: str = Field(pattern=UTC_TEXT)
    generated_by_ai: bool


class ReportListResponse(_Model):
    items: list[ReportItem]
    next_cursor: str | None
    as_of: str = Field(pattern=UTC_TEXT)
    schema_version: Literal["1.0"]


class ReportEvidenceCard(_Model):
    evidence_id: str
    source_url: str = Field(pattern=HTTPS_URL_PATTERN)
    excerpt: str
    license: Literal["metadata_only", "reusable_excerpt", "fulltext_allowed"]


class ReportDetailResponse(_Model):
    content: str
    evidence: list[ReportEvidenceCard]
    generated_by_ai: bool
    as_of: str = Field(pattern=UTC_TEXT)
    schema_version: Literal["1.0"]


@dataclass(frozen=True)
class ReportRow:
    report_id: str
    report_kind: str
    published_at: str
    published_at_dt: datetime
    generated_by_ai: bool
    content: str
    evidence_ids: tuple[str, ...]
    trend_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceRow:
    evidence_id: str
    source_url: str
    excerpt: str
    license_status: str
    published_at: str | None
    collected_at: str
    digest: str


@dataclass(frozen=True)
class TrendRow:
    trend_id: str
    topic: str
    window_days: int
    score: float
    confidence: str
    scored_at: str
    scored_at_dt: datetime
    source_families: tuple[str, ...]
    claim: str
    features: dict[str, Any]
    evidence_ids: tuple[str, ...] = ()
    contrary_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Publication:
    reports: tuple[ReportRow, ...]
    trends: dict[str, TrendRow]
    evidence: dict[str, EvidenceRow]
    as_of: str


def open_readonly(database: Path) -> sqlite3.Connection:
    """Open SQLite so the connection cannot write."""
    uri = database.resolve().as_uri()
    connection = sqlite3.connect(f"{uri}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def _readonly(database: Path) -> Iterator[sqlite3.Connection]:
    try:
        connection = open_readonly(database)
    except sqlite3.Error as exc:
        raise ApiError(503, "unavailable", "database is not readable") from exc
    try:
        yield connection
    finally:
        connection.close()


def current_time(request: Request) -> datetime:
    clock = getattr(request.app.state, "clock", None)
    if callable(clock):
        value = clock()
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(UTC)
        raise ApiError(503, "unavailable", "clock is invalid")
    return datetime.now(UTC)


def format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> datetime | None:
    if not _TIME.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def normalize_time(value: str) -> tuple[datetime, str] | None:
    parsed = parse_time(value)
    if parsed is None:
        return None
    trimmed = parsed.replace(microsecond=0)
    return trimmed, format_time(trimmed)


def json_error(request: Request, exc: ApiError) -> JSONResponse:
    payload = ErrorBody(
        code=exc.code,
        message=exc.message,
        request_id=request_id_of(request),
        as_of=format_time(current_time(request)),
        schema_version="1.0",
    )
    response = JSONResponse(
        status_code=exc.status_code,
        content=payload.model_dump(mode="json"),
    )
    response.headers["X-Request-ID"] = payload.request_id
    response.headers["X-Schema-Version"] = SCHEMA_VERSION
    return response


health_router = APIRouter(
    dependencies=[Depends(apply_rate_limit)],
    tags=["status"],
    responses={429: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
reader_router = APIRouter(
    dependencies=[Depends(apply_rate_limit), Depends(require_reader)],
    tags=["read"],
    responses={
        401: {"model": ErrorBody},
        429: {"model": ErrorBody},
        503: {"model": ErrorBody},
    },
)
metrics_router = APIRouter(
    dependencies=[Depends(apply_rate_limit), Depends(require_internal)],
    tags=["status"],
    responses={403: {"model": ErrorBody}, 429: {"model": ErrorBody}},
)


@health_router.get("/health/live", response_model=LiveResponse)
def health_live(request: Request) -> LiveResponse:
    return LiveResponse(status="ok", as_of=format_time(current_time(request)), schema_version="1.0")


@health_router.get(
    "/health/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse}},
)
def health_ready(request: Request) -> JSONResponse:
    settings = settings_of(request)
    database: Literal["ok", "fail"] = "ok" if database_ready(settings.database) else "fail"
    objects: Literal["ok", "fail"] = "ok" if store_ready(request.app.state.store) else "fail"
    status: Literal["ok", "unavailable"] = (
        "ok" if database == "ok" and objects == "ok" else "unavailable"
    )
    payload = ReadyResponse(
        status=status,
        checks={"database": database, "object_store": objects},
        as_of=format_time(current_time(request)),
        schema_version="1.0",
    )
    code = 200 if status == "ok" else 503
    return JSONResponse(status_code=code, content=payload.model_dump(mode="json"))


@reader_router.get(
    "/v1/trends",
    response_model=TrendListResponse,
    responses={400: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
def list_trends(
    request: Request,
    topic: Annotated[str | None, Query(description="Exact topic match")] = None,
    window: Annotated[str | None, Query(description="7 or 28")] = None,
    limit: Annotated[str | None, Query(description="Page size from 1 to 50")] = None,
    cursor: Annotated[str | None, Query(description="Server-signed page cursor")] = None,
) -> TrendListResponse:
    selected_topic = _parse_topic(topic)
    selected_window = _parse_window(window)
    page_limit = _parse_limit(limit)
    query = _trend_query(selected_topic, selected_window)
    after = _cursor(cursor, request, kind="trends", query=query)
    publication = _publication(request)
    rows = [
        trend
        for trend in publication.trends.values()
        if (selected_topic is None or trend.topic == selected_topic)
        and (selected_window is None or trend.window_days == selected_window)
    ]
    chosen, next_key = _page(
        [(row.scored_at_dt, row.trend_id, row) for row in rows],
        page_limit,
        after,
    )
    next_cursor = _next_cursor(request, kind="trends", query=query, next_key=next_key)
    items = [
        TrendItem(
            trend_id=row.trend_id,
            topic=row.topic,
            window_days=_window_literal(row.window_days),
            score=row.score,
            confidence=_confidence_literal(row.confidence),
        )
        for row in chosen
    ]
    return TrendListResponse(
        items=items,
        next_cursor=next_cursor,
        as_of=publication.as_of,
        schema_version="1.0",
    )


@reader_router.get(
    "/v1/trends/{id}",
    response_model=TrendDetailResponse,
    responses={404: {"model": ErrorBody}},
)
def get_trend(id: str, request: Request) -> TrendDetailResponse:
    trend_id = _parse_id(id)
    publication = _publication(request)
    row = publication.trends.get(trend_id)
    if row is None:
        raise ApiError(404, "not_found", "trend not found")
    trend = TrendBody(
        trend_id=row.trend_id,
        topic=row.topic,
        window_days=_window_literal(row.window_days),
        score=row.score,
        confidence=_confidence_literal(row.confidence),
        source_families=list(row.source_families),
        claim=row.claim,
        supporting_evidence_ids=list(row.evidence_ids),
        contrary_evidence_ids=list(row.contrary_ids),
        numeric_checks_passed=row.features.get("numeric_checks_passed") is True,
        last_verified_at=row.scored_at,
    )
    return TrendDetailResponse(
        trend=trend,
        features=row.features,
        evidence_ids=list(row.evidence_ids),
        contrary_ids=list(row.contrary_ids),
        as_of=publication.as_of,
        schema_version="1.0",
    )


@reader_router.get(
    "/v1/evidence/{id}",
    response_model=EvidenceResponse,
    responses={404: {"model": ErrorBody}},
)
def get_evidence(id: str, request: Request) -> EvidenceResponse:
    evidence_id = _parse_id(id)
    publication = _publication(request)
    row = publication.evidence.get(evidence_id)
    if row is None:
        raise ApiError(404, "not_found", "evidence not found")
    return _evidence_response(row, publication.as_of)


@reader_router.get(
    "/v1/reports",
    response_model=ReportListResponse,
    responses={400: {"model": ErrorBody}},
)
def list_reports(
    request: Request,
    kind: Annotated[str | None, Query(description="daily, weekly, or topic")] = None,
    limit: Annotated[str | None, Query(description="Page size from 1 to 50")] = None,
    cursor: Annotated[str | None, Query(description="Server-signed page cursor")] = None,
) -> ReportListResponse:
    selected_kind = _parse_kind(kind)
    page_limit = _parse_limit(limit)
    query = _report_query(selected_kind)
    after = _cursor(cursor, request, kind="reports", query=query)
    publication = _publication(request)
    rows = [
        row
        for row in publication.reports
        if selected_kind is None or row.report_kind == selected_kind
    ]
    chosen, next_key = _page(
        [(row.published_at_dt, row.report_id, row) for row in rows],
        page_limit,
        after,
    )
    items = [
        ReportItem(
            report_id=row.report_id,
            report_kind=_kind_literal(row.report_kind),
            published_at=row.published_at,
            generated_by_ai=row.generated_by_ai,
        )
        for row in chosen
    ]
    return ReportListResponse(
        items=items,
        next_cursor=_next_cursor(request, kind="reports", query=query, next_key=next_key),
        as_of=publication.as_of,
        schema_version="1.0",
    )


@reader_router.get(
    "/v1/reports/{id}",
    response_model=ReportDetailResponse,
    responses={404: {"model": ErrorBody}},
)
def get_report(id: str, request: Request) -> ReportDetailResponse:
    report_id = _parse_id(id)
    publication = _publication(request)
    row = next((item for item in publication.reports if item.report_id == report_id), None)
    if row is None:
        raise ApiError(404, "not_found", "report not found")
    cards = [
        _evidence_card(publication.evidence[evidence_id])
        for evidence_id in row.evidence_ids
        if evidence_id in publication.evidence
    ]
    cards.sort(key=lambda card: card.evidence_id)
    return ReportDetailResponse(
        content=row.content,
        evidence=cards,
        generated_by_ai=row.generated_by_ai,
        as_of=publication.as_of,
        schema_version="1.0",
    )


@metrics_router.get(
    "/metrics",
    response_class=PlainTextResponse,
    responses={403: {"model": ErrorBody}},
)
def metrics(request: Request) -> PlainTextResponse:
    as_of = format_time(current_time(request))
    body = (
        f"# schema_version: {SCHEMA_VERSION}\n"
        f"# as_of: {as_of}\n"
        "# HELP techpulse_api_up Read-only API process is up.\n"
        "# TYPE techpulse_api_up gauge\n"
        "techpulse_api_up 1\n"
    )
    return PlainTextResponse(
        body,
        media_type=PROM_MEDIA_TYPE,
        headers={"X-As-Of": as_of, "X-Schema-Version": SCHEMA_VERSION},
    )


def database_ready(database: Path) -> bool:
    if not database.is_file():
        return False
    try:
        connection = open_readonly(database)
    except sqlite3.Error:
        return False
    try:
        for name in _TABLES:
            found = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
            if found is None:
                return False
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    return True


def store_ready(store: object) -> bool:
    root = getattr(store, "root", None)
    if isinstance(root, Path):
        return root.is_dir()
    get = getattr(store, "get", None)
    return callable(get)


def _publication(request: Request) -> Publication:
    settings = settings_of(request)
    store = store_of(request)
    try:
        with _readonly(settings.database) as connection:
            reports = _load_reports(connection, store)
            evidence = _load_evidence(
                connection, _bounded_ids(item.evidence_ids for item in reports)
            )
            trends = _load_trends(connection, _bounded_ids(item.trend_ids for item in reports))
            trends = _attach_links(connection, trends, evidence)
    except sqlite3.Error as exc:
        raise ApiError(503, "unavailable", "database is not readable") from exc
    as_of = format_time(current_time(request))
    if reports:
        latest = max(reports, key=lambda row: (row.published_at_dt, row.report_id))
        as_of = latest.published_at
    visible_evidence = _evidence_named_by(reports, evidence)
    visible_trends = {
        trend_id: row for trend_id, row in trends.items() if _trend_is_named(row, reports)
    }
    return Publication(
        reports=tuple(reports),
        trends=visible_trends,
        evidence=visible_evidence,
        as_of=as_of,
    )


def _load_reports(connection: sqlite3.Connection, store: ObjectStore) -> list[ReportRow]:
    try:
        rows = connection.execute(
            """
            SELECT report_id, report_kind, content_uri, qa_manifest_uri,
                   evidence_snapshot_uri, published_at, generated_by_ai
            FROM published_reports
            ORDER BY published_at, report_id
            LIMIT ?
            """,
            (MAX_REPORTS,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ApiError(503, "unavailable", "database is not readable") from exc
    loaded: list[ReportRow] = []
    for row in rows:
        report = _report_row(row, store)
        if report is not None:
            loaded.append(report)
    return loaded


def _report_row(row: sqlite3.Row, store: ObjectStore) -> ReportRow | None:
    report_id = _text(row, "report_id")
    report_kind = _text(row, "report_kind")
    content_uri = _text(row, "content_uri")
    manifest_uri = _text(row, "qa_manifest_uri")
    snapshot_uri = _text(row, "evidence_snapshot_uri")
    published_raw = _text(row, "published_at")
    generated = _bit(row["generated_by_ai"])
    if (
        report_id is None
        or report_kind not in _KINDS
        or content_uri is None
        or manifest_uri is None
        or snapshot_uri is None
        or published_raw is None
        or generated is None
    ):
        return None
    stamped = normalize_time(published_raw)
    if stamped is None:
        return None
    manifest = _json_object(store, manifest_uri)
    snapshot = _json_object(store, snapshot_uri)
    content = _text_object(store, content_uri)
    if manifest is None or snapshot is None or content is None:
        return None
    if not _signed_pass(manifest, report_id):
        return None
    published_at_dt, published_at = stamped
    return ReportRow(
        report_id=report_id,
        report_kind=report_kind,
        published_at=published_at,
        published_at_dt=published_at_dt,
        generated_by_ai=generated,
        content=content,
        evidence_ids=_string_ids(snapshot.get("evidence_ids")),
        trend_ids=_string_ids(snapshot.get("trend_ids")),
    )


def _signed_pass(manifest: Mapping[str, object], report_id: str) -> bool:
    return (
        manifest.get("qa_pass") is True
        and manifest.get("signed") is True
        and manifest.get("report_id") == report_id
    )


def _load_evidence(
    connection: sqlite3.Connection,
    evidence_ids: Sequence[str],
) -> dict[str, EvidenceRow]:
    if not evidence_ids:
        return {}
    marks = ",".join("?" for _ in evidence_ids)
    query = (
        "SELECT e.evidence_id, e.excerpt, e.license_status, e.sha256, "  # nosec B608  # placeholders only; values bound
        "r.canonical_url, r.published_at, r.collected_at "
        "FROM evidence_items AS e "
        "JOIN raw_records AS r ON r.id = e.source_record_id "
        f"WHERE e.evidence_id IN ({marks})"
    )
    found: dict[str, EvidenceRow] = {}
    for row in connection.execute(query, list(evidence_ids)).fetchall():
        item = _evidence_row(row)
        if item is not None:
            found[item.evidence_id] = item
    return found


def _evidence_row(row: sqlite3.Row) -> EvidenceRow | None:
    evidence_id = _text(row, "evidence_id")
    excerpt = _text(row, "excerpt")
    license_status = _text(row, "license_status")
    digest = _text(row, "sha256")
    source_url = _text(row, "canonical_url")
    collected_raw = _text(row, "collected_at")
    published_raw = row["published_at"]
    if (
        evidence_id is None
        or excerpt is None
        or license_status not in _PUBLIC_LICENSES
        or digest is None
        or re.fullmatch(SHA256_PATTERN, digest) is None
        or source_url is None
        or re.fullmatch(HTTPS_URL_PATTERN, source_url) is None
        or collected_raw is None
    ):
        return None
    collected = normalize_time(collected_raw)
    if collected is None:
        return None
    published_at: str | None = None
    if published_raw is not None:
        if not isinstance(published_raw, str):
            return None
        published = normalize_time(published_raw)
        if published is None:
            return None
        published_at = published[1]
    clipped = _clip_excerpt(excerpt, license_status)
    if not clipped:
        return None
    return EvidenceRow(
        evidence_id=evidence_id,
        source_url=source_url,
        excerpt=clipped,
        license_status=license_status,
        published_at=published_at,
        collected_at=collected[1],
        digest=digest.lower(),
    )


def _load_trends(connection: sqlite3.Connection, trend_ids: Sequence[str]) -> dict[str, TrendRow]:
    if not trend_ids:
        return {}
    marks = ",".join("?" for _ in trend_ids)
    query = (
        "SELECT trend_id, topic, window_days, score, confidence, scored_at, "  # nosec B608  # placeholders only; values bound
        "features_json, source_families FROM trend_scores "
        f"WHERE trend_id IN ({marks})"
    )
    found: dict[str, TrendRow] = {}
    for row in connection.execute(query, list(trend_ids)).fetchall():
        item = _trend_row(row)
        if item is not None:
            found[item.trend_id] = item
    return found


def _trend_row(row: sqlite3.Row) -> TrendRow | None:
    trend_id = _text(row, "trend_id")
    topic = _text(row, "topic")
    window = row["window_days"]
    score = _finite_float(row["score"])
    confidence = _text(row, "confidence")
    scored_raw = _text(row, "scored_at")
    features_raw = _text(row, "features_json")
    families_raw = _text(row, "source_families")
    if (
        trend_id is None
        or topic is None
        or len(topic) > MAX_TOPIC
        or isinstance(window, bool)
        or not isinstance(window, int)
        or window not in _WINDOWS
        or score is None
        or confidence not in _CONFIDENCE
        or scored_raw is None
        or features_raw is None
        or families_raw is None
    ):
        return None
    stamped = normalize_time(scored_raw)
    features = _public_json(features_raw)
    families = _families(families_raw)
    claim = features.get("claim") if features is not None else None
    if stamped is None or features is None or not families or not isinstance(claim, str):
        return None
    clipped_claim = claim.strip()[:EXCERPT_SHORT]
    if not clipped_claim:
        return None
    features["claim"] = clipped_claim
    scored_at_dt, scored_at = stamped
    return TrendRow(
        trend_id=trend_id,
        topic=topic,
        window_days=window,
        score=score,
        confidence=confidence,
        scored_at=scored_at,
        scored_at_dt=scored_at_dt,
        source_families=families,
        claim=clipped_claim,
        features=features,
    )


def _attach_links(
    connection: sqlite3.Connection,
    trends: dict[str, TrendRow],
    evidence: Mapping[str, EvidenceRow],
) -> dict[str, TrendRow]:
    if not trends:
        return {}
    trend_ids = list(trends)[:MAX_IDS]
    marks = ",".join("?" for _ in trend_ids)
    query = (
        f"SELECT trend_id, evidence_id, relation FROM trend_evidence WHERE trend_id IN ({marks})"  # nosec B608  # placeholders only; values bound
    )
    support: dict[str, set[str]] = {trend_id: set() for trend_id in trend_ids}
    contrary: dict[str, set[str]] = {trend_id: set() for trend_id in trend_ids}
    for row in connection.execute(query, trend_ids).fetchall():
        trend_id = _text(row, "trend_id")
        evidence_id = _text(row, "evidence_id")
        relation = _text(row, "relation")
        if trend_id is None or evidence_id is None or evidence_id not in evidence:
            continue
        if relation == "support":
            support.setdefault(trend_id, set()).add(evidence_id)
        elif relation == "contradict":
            contrary.setdefault(trend_id, set()).add(evidence_id)
    return {
        trend_id: replace(
            row,
            evidence_ids=tuple(sorted(support.get(trend_id, ()))),
            contrary_ids=tuple(sorted(contrary.get(trend_id, ()))),
        )
        for trend_id, row in trends.items()
    }


def _evidence_named_by(
    reports: Sequence[ReportRow],
    evidence: Mapping[str, EvidenceRow],
) -> dict[str, EvidenceRow]:
    named = {evidence_id for report in reports for evidence_id in report.evidence_ids}
    return {evidence_id: row for evidence_id, row in evidence.items() if evidence_id in named}


def _trend_is_named(row: TrendRow, reports: Sequence[ReportRow]) -> bool:
    return any(row.trend_id in report.trend_ids for report in reports)


def _bounded_ids(groups: Iterable[Sequence[str]]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if item in seen:
                continue
            seen.add(item)
            found.append(item)
            if len(found) >= MAX_IDS:
                return found
    return found


def _page[T](
    rows: Sequence[tuple[datetime, str, T]],
    limit: int,
    after: tuple[str, str] | None,
) -> tuple[list[T], tuple[str, str] | None]:
    ordered = sorted(rows, key=lambda item: (item[0], item[1]))
    start: tuple[datetime, str] | None = None
    if after is not None:
        parsed = parse_time(after[0])
        if parsed is None:
            raise ApiError(400, "invalid_cursor", "cursor is invalid")
        start = (parsed.replace(microsecond=0), after[1])
    chosen: list[tuple[datetime, str, T]] = []
    for item in ordered:
        if start is not None and (item[0], item[1]) <= start:
            continue
        chosen.append(item)
        if len(chosen) == limit:
            break
    if len(chosen) < limit:
        return [item[2] for item in chosen], None
    last = chosen[-1]
    has_more = any((item[0], item[1]) > (last[0], last[1]) for item in ordered)
    if not has_more:
        return [item[2] for item in chosen], None
    return [item[2] for item in chosen], (format_time(last[0]), last[1])


def _cursor(
    token: str | None,
    request: Request,
    *,
    kind: str,
    query: Mapping[str, str],
) -> tuple[str, str] | None:
    if token is None:
        return None
    if token == "" or len(token) > 2048:  # nosec B105  # empty-string cursor validation, not a password
        raise ApiError(400, "invalid_cursor", "cursor is invalid")
    settings = settings_of(request)
    return verify_cursor(token, key=settings.cursor_key, kind=kind, query=query, now=time.time())


def _next_cursor(
    request: Request,
    *,
    kind: str,
    query: Mapping[str, str],
    next_key: tuple[str, str] | None,
) -> str | None:
    if next_key is None:
        return None
    settings = settings_of(request)
    return sign_cursor(
        key=settings.cursor_key,
        kind=kind,
        query=query,
        after_sort=next_key[0],
        after_id=next_key[1],
        now=time.time(),
        ttl=settings.cursor_ttl_seconds,
    )


def _trend_query(topic: str | None, window: int | None) -> dict[str, str]:
    return {
        "topic": "" if topic is None else topic,
        "window": "" if window is None else str(window),
    }


def _report_query(kind: str | None) -> dict[str, str]:
    return {"kind": "" if kind is None else kind}


def _parse_topic(raw: str | None) -> str | None:
    if raw is None:
        return None
    if not raw or len(raw) > MAX_TOPIC or any(ord(char) < 32 for char in raw):
        raise ApiError(400, "invalid_topic", "topic is invalid")
    return raw


def _parse_window(raw: str | None) -> int | None:
    if raw is None:
        return None
    if raw not in {"7", "28"}:
        raise ApiError(400, "invalid_window", "window must be 7 or 28")
    return int(raw)


def _parse_kind(raw: str | None) -> str | None:
    if raw is None:
        return None
    if raw not in _KINDS:
        raise ApiError(400, "invalid_kind", "kind must be daily, weekly, or topic")
    return raw


def _parse_limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    if _LIMIT.fullmatch(raw) is None:
        raise ApiError(400, "invalid_limit", "limit must be an integer from 1 to 50")
    value = int(raw)
    if value > MAX_LIMIT:
        raise ApiError(400, "invalid_limit", "limit must be an integer from 1 to 50")
    return value


def _parse_id(raw: str) -> str:
    if not raw or len(raw) > MAX_ID or any(ord(char) < 32 for char in raw):
        raise ApiError(400, "invalid_id", "id is invalid")
    return raw


def _evidence_response(row: EvidenceRow, as_of: str) -> EvidenceResponse:
    return EvidenceResponse(
        source_url=row.source_url,
        excerpt=row.excerpt,
        license=_license_literal(row.license_status),
        dates=EvidenceDates(published_at=row.published_at, collected_at=row.collected_at),
        hash=row.digest,
        as_of=as_of,
        schema_version="1.0",
    )


def _evidence_card(row: EvidenceRow) -> ReportEvidenceCard:
    return ReportEvidenceCard(
        evidence_id=row.evidence_id,
        source_url=row.source_url,
        excerpt=row.excerpt,
        license=_license_literal(row.license_status),
    )


def _window_literal(value: int) -> Literal[7, 28]:
    if value == 7:
        return 7
    return 28


def _confidence_literal(value: str) -> Literal["high", "medium", "low"]:
    if value == "high":
        return "high"
    if value == "low":
        return "low"
    return "medium"


def _kind_literal(value: str) -> Literal["daily", "weekly", "topic"]:
    if value == "daily":
        return "daily"
    if value == "topic":
        return "topic"
    return "weekly"


def _license_literal(
    value: str,
) -> Literal["metadata_only", "reusable_excerpt", "fulltext_allowed"]:
    if value == "reusable_excerpt":
        return "reusable_excerpt"
    if value == "fulltext_allowed":
        return "fulltext_allowed"
    return "metadata_only"


def _clip_excerpt(text: str, license_status: str) -> str:
    # metadata_only and reusable_excerpt stay short. fulltext_allowed is still capped.
    limit = EXCERPT_FULL if license_status == "fulltext_allowed" else EXCERPT_SHORT
    stripped = text.strip()
    return stripped[:limit]


def _json_object(store: ObjectStore, uri: str) -> dict[str, object] | None:
    body = _read_bytes(store, uri)
    if body is None:
        return None
    try:
        payload: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {str(key): value for key, value in payload.items()}


def _text_object(store: ObjectStore, uri: str) -> str | None:
    body = _read_bytes(store, uri)
    if body is None:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text == "":
        return None
    return text[:MAX_CONTENT]


def _read_bytes(store: ObjectStore, uri: str) -> bytes | None:
    try:
        return store.get(uri)
    except (OSError, ValueError):
        return None


def _public_json(raw: str) -> dict[str, Any] | None:
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    cleaned: dict[str, Any] = {}
    for key, value in list(payload.items())[:50]:
        if isinstance(key, str) and key and len(key) <= 64:
            cleaned[key] = _public_value(value, depth=0)
    return cleaned


def _public_value(value: object, depth: int) -> Any:
    if depth > 4:
        return None
    if isinstance(value, str):
        return value[:EXCERPT_FULL]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_public_value(item, depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        nested: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            if isinstance(key, str) and len(key) <= 64:
                nested[key] = _public_value(item, depth + 1)
        return nested
    return None


def _families(raw: str) -> tuple[str, ...] | None:
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    found: list[str] = []
    for item in payload:
        if isinstance(item, str) and item in _FAMILIES and item not in found:
            found.append(item)
    if not found:
        return None
    return tuple(found)


def _string_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    found: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_ID or item in seen:
            continue
        seen.add(item)
        found.append(item)
        if len(found) == 100:
            break
    return tuple(found)


def _text(row: sqlite3.Row, name: str) -> str | None:
    value = row[name]
    if not isinstance(value, str) or value == "":
        return None
    return value


def _bit(value: object) -> bool | None:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        return None
    return value == 1


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number
