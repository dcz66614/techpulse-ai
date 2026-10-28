"""arXiv read-only Atom API collector.

Descriptive metadata only. PDF, source, and HTML links in the feed are not requested.
Requests are at least three seconds apart.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
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
    parse_timestamp,
    query_key,
    registry_failure,
)
from techpulse.contracts import LicenseStatus, RawRecord, SourceId, TaskStatus

Clock = Callable[[], float]
Sleep = Callable[[float], None]
_SOURCE_ID = "arxiv"
MIN_INTERVAL_SECONDS = 3.0
BULK_MESSAGE = (
    "policy_blocked: result count exceeds the official API limit; "
    "re-verify the official bulk method"
)
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
_ARXIV_ID = re.compile(
    r"^(?:https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/)?"
    r"(?P<base>\d{4}\.\d{4,5}|[a-z][a-z.\-]*/\d{7})"
    r"(?:v(?P<version>\d+))?$",
    re.IGNORECASE,
)


def split_arxiv_id(value: str) -> tuple[str, int]:
    """Return the base arXiv id and version. A missing version is version 1.

    `pdf` URLs are accepted only so an id can be read. The collector does not GET them.
    """
    match = _ARXIV_ID.fullmatch(value.strip())
    if match is None:
        raise ValueError("not an arXiv id")
    base = match.group("base")
    version_text = match.group("version")
    version = 1 if version_text is None else int(version_text)
    if version < 1:
        raise ValueError("not an arXiv id")
    return base, version


def collect_arxiv(
    *,
    search_query: str,
    window_start: datetime,
    window_end: datetime,
    store: CursorStore,
    registry_path: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    cursor: str | None = None,
    page_size: int = 100,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
    collected_at: datetime | None = None,
    timeout: httpx.Timeout | None = None,
    max_retries: int = 2,
    limiter: IntervalLimiter | None = None,
    artifacts_dir: Path | None = None,
    run_id: str | None = None,
) -> CollectResult:
    """Page the export API for one query and time window. The end instant is excluded."""
    text = search_query.strip()
    if not text:
        raise ValueError("search_query must not be empty")
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("window bounds must be timezone-aware")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")
    size = _page_size(page_size)
    when = datetime.now(UTC) if collected_at is None else collected_at
    if when.tzinfo is None:
        raise ValueError("collected_at must be timezone-aware")
    try:
        config = load_source_config(_SOURCE_ID, registry_path)
    except ValueError:
        return registry_failure("policy_blocked: source registry")
    if config.slice_max is not None:
        size = min(size, config.slice_max)
    key = query_key(
        {
            "page_size": str(size),
            "search_query": text,
            "source": _SOURCE_ID,
            "window_end": iso_z(window_end),
            "window_start": iso_z(window_start),
        }
    )
    if not config.enabled:
        return disabled_result(config, store, key)
    floor = max(config.min_interval_seconds, MIN_INTERVAL_SECONDS)
    pace = limiter
    if pace is None:
        pace = IntervalLimiter(floor, clock=clock, sleep=sleep)
    elif pace.min_interval_seconds < MIN_INTERVAL_SECONDS:
        pace.min_interval_seconds = MIN_INTERVAL_SECONDS
    registry = default_registry_path() if registry_path is None else registry_path
    client = build_client(
        config,
        transport=transport,
        registry_path=registry,
        timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
        accept="application/atom+xml",
    )
    try:

        def fetch_page(page_cursor: str) -> ParsedPage:
            params = {
                "max_results": str(size),
                "search_query": _with_window(text, window_start, window_end),
                "sortBy": "submittedDate",
                "sortOrder": "ascending",
                "start": page_cursor,
            }
            fetched = guarded_get(
                client,
                pace,
                config,
                config.base_url,
                params=params,
                max_retries=max_retries,
            )
            page = parse_feed(
                fetched.text,
                start=_start_index(page_cursor),
                page_size=size,
                window_start=window_start,
                window_end=window_end,
                collected_at=when,
                policy_snapshot_id=config.policy_snapshot_id,
            )
            if (
                page.total is not None
                and config.total_max is not None
                and page.total > config.total_max
            ):
                raise CollectorFailure(TaskStatus.POLICY_BLOCKED, BULK_MESSAGE)
            return page

        return collect_pages(
            config=config,
            store=store,
            query_key=key,
            explicit_cursor=cursor,
            initial_cursor="0",
            fetch_page=fetch_page,
            id_dedup=False,
            collected_at=when,
            artifacts_dir=artifacts_dir,
            run_id=run_id,
            ticket="C12",
        )
    finally:
        client.close()


def parse_feed(
    text: str,
    *,
    start: int,
    page_size: int,
    window_start: datetime,
    window_end: datetime,
    collected_at: datetime,
    policy_snapshot_id: str,
) -> ParsedPage:
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except ElementTree.ParseError as exc:
        raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: invalid feed") from exc
    entries = root.findall(f"{_ATOM}entry")
    items: list[ParsedItem] = []
    warnings: list[str] = []
    for entry in entries:
        try:
            item = _entry_item(
                entry,
                collected_at=collected_at,
                policy_snapshot_id=policy_snapshot_id,
            )
        except ValueError:
            warnings.append("skipped an entry without an arXiv id or title")
            continue
        published = item.record.published_at
        if published is None or published < window_start or published >= window_end:
            continue
        items.append(item)
    received = len(entries)
    next_start = start + received
    total = _total_results(root)
    if received < page_size or (total is not None and next_start >= total):
        next_cursor = ""
    else:
        next_cursor = str(next_start)
    return ParsedPage(items=items, next_cursor=next_cursor, warnings=warnings, total=total)


def _entry_item(
    entry: ElementTree.Element,
    *,
    collected_at: datetime,
    policy_snapshot_id: str,
) -> ParsedItem:
    id_text = _text(entry.find(f"{_ATOM}id"))
    if not id_text:
        raise ValueError("not an arXiv id")
    base, version = split_arxiv_id(id_text)
    title = _text(entry.find(f"{_ATOM}title"))
    if not title:
        raise ValueError("missing title")
    summary = _text(entry.find(f"{_ATOM}summary"))
    published_raw = _text(entry.find(f"{_ATOM}published"))
    updated_raw = _text(entry.find(f"{_ATOM}updated"))
    published_at = _optional_time(published_raw)
    updated_at = _optional_time(updated_raw)
    missing_reason = None if published_at is not None else "source omitted publication date"
    categories, primary = _categories(entry)
    authors = _authors(entry)
    record_id = f"{base}v{version}"
    published_text = None if published_at is None else iso_z(published_at)
    updated_text = "" if updated_at is None else iso_z(updated_at)
    digest = content_sha256(
        {
            "arxiv_id": base,
            "categories": categories,
            "summary": summary,
            "title": title,
            "updated_at": updated_text,
            "version": version,
        }
    )
    record = RawRecord.model_validate(
        {
            "schema_version": "1.0",
            "source_id": SourceId.ARXIV.value,
            "source_record_id": record_id,
            "canonical_url": f"https://arxiv.org/abs/{record_id}",
            "title": title,
            "abstract_or_excerpt": summary,
            "published_at": published_text,
            "published_at_missing_reason": missing_reason,
            "collected_at": iso_z(collected_at),
            "language": "und",
            "license_status": LicenseStatus.METADATA_ONLY.value,
            "content_sha256": digest,
            "policy_snapshot_id": policy_snapshot_id,
        }
    )
    provenance = dump_provenance(
        {
            "arxiv_id": base,
            "authors": authors,
            "categories": categories,
            "primary_category": primary,
            "published_at": "" if published_text is None else published_text,
            "title": title,
            "updated_at": updated_text,
            "version": version,
        }
    )
    return ParsedItem(record=record, provenance_json=provenance)


def _with_window(search_query: str, window_start: datetime, window_end: datetime) -> str:
    begin = window_start.astimezone(UTC).strftime("%Y%m%d%H%M")
    finish = window_end.astimezone(UTC).strftime("%Y%m%d%H%M")
    return f"{search_query} AND submittedDate:[{begin} TO {finish}]"


def _categories(entry: ElementTree.Element) -> tuple[list[str], str]:
    found: list[str] = []
    primary = ""
    primary_el = entry.find(f"{_ARXIV}primary_category")
    if primary_el is not None:
        primary = primary_el.attrib.get("term", "").strip()
    if primary:
        found.append(primary)
    for element in entry.findall(f"{_ATOM}category"):
        term = element.attrib.get("term", "").strip()
        if term and term not in found:
            found.append(term)
    return found, primary


def _authors(entry: ElementTree.Element) -> list[str]:
    found: list[str] = []
    for author in entry.findall(f"{_ATOM}author"):
        name = _text(author.find(f"{_ATOM}name"))
        if name and name not in found:
            found.append(name)
    return found


def _total_results(root: ElementTree.Element) -> int | None:
    raw = _text(root.find(f"{_OPENSEARCH}totalResults"))
    if not raw or not raw.isdigit():
        return None
    return int(raw)


def _optional_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def _text(element: ElementTree.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return " ".join(element.text.split())


def _start_index(cursor: str) -> int:
    if not cursor.isdigit():
        raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: bad cursor")
    return int(cursor)


def _page_size(requested: int) -> int:
    if isinstance(requested, bool) or requested < 1:
        raise ValueError("page_size must be >= 1")
    return requested
