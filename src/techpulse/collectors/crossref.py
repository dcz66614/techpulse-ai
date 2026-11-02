"""Crossref read-only works collector."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from techpulse.collectors.base import (
    DEFAULT_TIMEOUT,
    CollectorFailure,
    CollectResult,
    CursorStore,
    IntervalLimiter,
    ParsedItem,
    ParsedPage,
    build_client,
    collect_pages,
    content_sha256,
    default_registry_path,
    disabled_result,
    dump_provenance,
    guarded_get,
    iso_z,
    load_source_config,
    query_key,
    registry_failure,
)
from techpulse.contracts import LicenseStatus, RawRecord, SourceId, TaskStatus

Clock = Callable[[], float]
Sleep = Callable[[float], None]
_SOURCE_ID = "crossref"
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def normalize_doi(value: str) -> str:
    """Lower-case a DOI and strip doi.org / dx.doi.org / doi: prefixes."""
    text = value.strip()
    text = _DOI_PREFIX.sub("", text)
    text = text.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
    text = text.rstrip(".")
    lowered = text.lower()
    if "/" not in lowered or lowered.startswith("/") or lowered.endswith("/"):
        raise ValueError("not a DOI")
    return lowered


def collect_crossref(
    *,
    store: CursorStore,
    registry_path: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    query: str | None = None,
    doi: str | None = None,
    updated_from: str | None = None,
    updated_until: str | None = None,
    cursor: str | None = None,
    rows: int = 20,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
    collected_at: datetime | None = None,
    timeout: httpx.Timeout | None = None,
    max_retries: int = 2,
    limiter: IntervalLimiter | None = None,
    artifacts_dir: Path | None = None,
    run_id: str | None = None,
    max_pages: int | None = None,
) -> CollectResult:
    """Cursor-page Crossref works. The same DOI on two pages is stored once."""
    if query is None and doi is None:
        raise ValueError("query or doi is required")
    search = None if query is None else query.strip()
    if search == "":
        raise ValueError("query must not be empty")
    normalized_doi = None if doi is None else normalize_doi(doi)
    date_from = _plain_date(updated_from, "updated_from")
    date_until = _plain_date(updated_until, "updated_until")
    page_size = _page_size(rows)
    when = datetime.now(UTC) if collected_at is None else collected_at
    if when.tzinfo is None:
        raise ValueError("collected_at must be timezone-aware")
    try:
        config = load_source_config(_SOURCE_ID, registry_path)
    except ValueError:
        return registry_failure("policy_blocked: source registry")
    if config.per_page_max is not None:
        page_size = min(page_size, config.per_page_max)
    key = query_key(
        {
            "doi": "" if normalized_doi is None else normalized_doi,
            "query": "" if search is None else search,
            "rows": str(page_size),
            "source": _SOURCE_ID,
            "updated_from": "" if date_from is None else date_from,
            "updated_until": "" if date_until is None else date_until,
        }
    )
    if not config.enabled:
        return disabled_result(config, store, key)
    pace = limiter
    if pace is None:
        pace = IntervalLimiter(config.min_interval_seconds, clock=clock, sleep=sleep)
    registry = default_registry_path() if registry_path is None else registry_path
    client = build_client(
        config,
        transport=transport,
        registry_path=registry,
        timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
        accept="application/json",
    )
    try:

        def fetch_page(page_cursor: str) -> ParsedPage:
            params: dict[str, str] = {
                "cursor": page_cursor,
                "mailto": config.mailto,
                "rows": str(page_size),
            }
            if search is not None:
                params["query"] = search
            filters = _filters(normalized_doi, date_from, date_until)
            if filters:
                params["filter"] = filters
            fetched = guarded_get(
                client,
                pace,
                config,
                f"{config.base_url}/works",
                params=params,
                max_retries=max_retries,
            )
            return parse_works_page(
                fetched.text,
                collected_at=when,
                policy_snapshot_id=config.policy_snapshot_id,
            )

        return collect_pages(
            config=config,
            store=store,
            query_key=key,
            explicit_cursor=cursor,
            initial_cursor="*",
            fetch_page=fetch_page,
            id_dedup=True,
            collected_at=when,
            artifacts_dir=artifacts_dir,
            run_id=run_id,
            ticket="C11",
            max_pages=max_pages,
        )
    finally:
        client.close()


def parse_works_page(text: str, *, collected_at: datetime, policy_snapshot_id: str) -> ParsedPage:
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: invalid json") from exc
    body = _as_dict(loaded)
    message = None if body is None else _as_dict(body.get("message"))
    if message is None:
        raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: invalid json")
    raw_items = message.get("items")
    if not isinstance(raw_items, list):
        raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: missing items")
    if not raw_items:
        return ParsedPage(items=[], next_cursor="")
    items: list[ParsedItem] = []
    warnings: list[str] = []
    for raw in raw_items:
        work = _as_dict(raw)
        if work is None:
            warnings.append("skipped a non-object work")
            continue
        try:
            item = _work_item(
                work,
                collected_at=collected_at,
                policy_snapshot_id=policy_snapshot_id,
            )
        except ValueError:
            warnings.append("skipped a work without a DOI")
            continue
        if item is None:
            warnings.append("skipped a work without a title")
            continue
        items.append(item)
    return ParsedPage(
        items=items,
        next_cursor=_next_cursor(message.get("next-cursor")),
        warnings=warnings,
    )


def _work_item(
    work: dict[str, object],
    *,
    collected_at: datetime,
    policy_snapshot_id: str,
) -> ParsedItem | None:
    raw_doi = work.get("DOI")
    if not isinstance(raw_doi, str) or not raw_doi.strip():
        raise ValueError("not a DOI")
    doi = normalize_doi(raw_doi)
    title = _title(work.get("title"))
    if title is None:
        return None
    published_at, missing_reason = _published(work)
    published_text = None if published_at is None else iso_z(published_at)
    type_name = work.get("type")
    type_text = type_name.strip() if isinstance(type_name, str) else ""
    links = _urls(work.get("link"))
    licenses = _urls(work.get("license"))
    abstract = _excerpt(work.get("abstract"))
    language = work.get("language")
    language_text = language.strip() if isinstance(language, str) and language.strip() else "und"
    digest = content_sha256(
        {
            "abstract": abstract,
            "doi": doi,
            "license": licenses,
            "link": links,
            "published_at": published_text,
            "title": title,
            "type": type_text,
        }
    )
    record = RawRecord.model_validate(
        {
            "schema_version": "1.0",
            "source_id": SourceId.CROSSREF.value,
            "source_record_id": doi,
            "canonical_url": f"https://doi.org/{doi}",
            "title": title,
            "abstract_or_excerpt": abstract,
            "published_at": published_text,
            "published_at_missing_reason": missing_reason,
            "collected_at": iso_z(collected_at),
            "language": language_text,
            "license_status": LicenseStatus.METADATA_ONLY.value,
            "content_sha256": digest,
            "policy_snapshot_id": policy_snapshot_id,
        }
    )
    provenance = dump_provenance(
        {
            "doi": doi,
            "license": licenses,
            "link": links,
            "published_at": "" if published_text is None else published_text,
            "title": title,
            "type": type_text,
        }
    )
    return ParsedItem(record=record, provenance_json=provenance)


def _title(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())
    if not isinstance(value, list):
        return None
    parts = [part.strip() for part in value if isinstance(part, str) and part.strip()]
    if not parts:
        return None
    return " ".join(" ".join(part.split()) for part in parts)


def _published(work: dict[str, object]) -> tuple[datetime | None, str | None]:
    saw_partial = False
    for key in ("published", "issued", "published-print", "published-online"):
        block = _as_dict(work.get(key))
        if block is None:
            continue
        parts = block.get("date-parts")
        if not isinstance(parts, list) or not parts:
            continue
        first = parts[0]
        if not isinstance(first, list) or not first:
            continue
        numbers: list[int] = []
        valid = True
        for item in first:
            if isinstance(item, bool) or not isinstance(item, int):
                valid = False
                break
            numbers.append(item)
        if not valid:
            continue
        if len(numbers) < 3:
            saw_partial = True
            continue
        try:
            return datetime(numbers[0], numbers[1], numbers[2], tzinfo=UTC), None
        except ValueError:
            return None, "source published date is incomplete"
    if saw_partial:
        return None, "source published date is incomplete"
    return None, "source omitted publication date"


def _urls(value: object) -> list[str]:
    found: list[str] = []
    if not isinstance(value, list):
        return found
    for item in value:
        mapped = _as_dict(item)
        if mapped is None:
            continue
        url = mapped.get("URL")
        if isinstance(url, str) and url.strip() and url.strip() not in found:
            found.append(url.strip())
    return found


def _excerpt(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = html.unescape(_TAG.sub(" ", value))
    return " ".join(text.split())


def _filters(doi: str | None, updated_from: str | None, updated_until: str | None) -> str:
    parts: list[str] = []
    if doi is not None:
        parts.append(f"doi:{doi}")
    if updated_from is not None:
        parts.append(f"from-update-date:{updated_from}")
    if updated_until is not None:
        parts.append(f"until-update-date:{updated_until}")
    return ",".join(parts)


def _plain_date(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text or "," in text:
        raise ValueError(f"{label} must be a date without commas")
    return text


def _page_size(requested: int) -> int:
    if isinstance(requested, bool) or requested < 1:
        raise ValueError("rows must be >= 1")
    return requested


def _next_cursor(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: bad cursor")


def _as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    mapped: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            mapped[key] = item
    return mapped
