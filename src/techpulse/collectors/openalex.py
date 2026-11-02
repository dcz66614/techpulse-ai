"""OpenAlex read-only works collector."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
_SOURCE_ID = "openalex"


def collect_openalex(
    *,
    keyword: str,
    store: CursorStore,
    registry_path: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    cursor: str | None = None,
    per_page: int = 100,
    updated_since: str | None = None,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
    collected_at: datetime | None = None,
    timeout: httpx.Timeout | None = None,
    max_retries: int = 2,
    limiter: IntervalLimiter | None = None,
    artifacts_dir: Path | None = None,
    run_id: str | None = None,
    api_key: str | None = None,
    max_pages: int | None = None,
) -> CollectResult:
    """Collect works for one keyword. Cursor paging stops on the last page.

    `per_page` is capped at the registry maximum. A page is committed only
    after it parses. 429 waits for Retry-After and stays retryable.
    """
    text = _search_text(keyword, "keyword")
    updated = None if updated_since is None else _search_text(updated_since, "updated_since")
    page_size = _page_size(per_page)
    when = datetime.now(UTC) if collected_at is None else collected_at
    if when.tzinfo is None:
        raise ValueError("collected_at must be timezone-aware")
    path = registry_path
    try:
        config = load_source_config(_SOURCE_ID, path)
    except ValueError:
        return registry_failure("policy_blocked: source registry")
    if config.per_page_max is not None:
        page_size = min(page_size, config.per_page_max)
    key = query_key(
        {
            "source": _SOURCE_ID,
            "keyword": text,
            "per_page": str(page_size),
            "updated_since": "" if updated is None else updated,
        }
    )
    if not config.enabled:
        return disabled_result(config, store, key)
    registry = config_path(path)
    effective_api_key = (
        api_key
        if api_key is not None
        else os.environ.get("OPENALEX_API_KEY", "").strip() or None
    )
    effective_updated = updated
    wm_ts = store.get_watermark(_SOURCE_ID, key)
    if effective_updated is None and wm_ts is not None:
        try:
            wm_dt = parse_timestamp(wm_ts)
            effective_updated = (wm_dt - timedelta(days=2)).strftime("%Y-%m-%d")
        except Exception:
            effective_updated = wm_ts.split("T")[0]

    stored_cursor = store.get_cursor(_SOURCE_ID, key)
    start_cursor = cursor
    if start_cursor is None and wm_ts is not None:
        if stored_cursor == "" or (effective_api_key is None and stored_cursor and stored_cursor.startswith("Il")):
            start_cursor = "*"

    pace = limiter
    if pace is None:
        pace = IntervalLimiter(config.min_interval_seconds, clock=clock, sleep=sleep)
    client = build_client(
        config,
        transport=transport,
        registry_path=registry,
        timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
        accept="application/json",
    )
    try:

        def fetch_page(page_cursor: str) -> ParsedPage:
            params = {
                "mailto": config.mailto,
                "per-page": str(page_size),
                "cursor": page_cursor,
                "filter": _filter(text, effective_updated),
                "sort": "publication_date:desc",
            }
            if effective_api_key:
                params["api_key"] = effective_api_key
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
            explicit_cursor=start_cursor,
            initial_cursor="*",
            fetch_page=fetch_page,
            id_dedup=False,
            collected_at=when,
            artifacts_dir=artifacts_dir,
            run_id=run_id,
            ticket="C10",
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
    if body is None:
        raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: invalid json")
    meta = _as_dict(body.get("meta"))
    if meta is None:
        raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: missing meta")
    results = body.get("results")
    if not isinstance(results, list):
        raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: missing results")
    items: list[ParsedItem] = []
    warnings: list[str] = []
    for result in results:
        work = _as_dict(result)
        if work is None:
            warnings.append("skipped a non-object work")
            continue
        item = _work_item(work, collected_at=collected_at, policy_snapshot_id=policy_snapshot_id)
        if item is None:
            warnings.append("skipped a work without an id or title")
            continue
        items.append(item)
    return ParsedPage(
        items=items, next_cursor=_next_cursor(meta.get("next_cursor")), warnings=warnings
    )


def config_path(path: Path | None) -> Path:
    return default_registry_path() if path is None else path


def _work_item(
    work: dict[str, object],
    *,
    collected_at: datetime,
    policy_snapshot_id: str,
) -> ParsedItem | None:
    work_id = _short_id(work.get("id"))
    title_raw = work.get("display_name")
    if not isinstance(title_raw, str) or not title_raw.strip():
        title_raw = work.get("title")
    if work_id is None or not isinstance(title_raw, str) or not title_raw.strip():
        return None
    title = " ".join(title_raw.split())
    abstract = _abstract(work.get("abstract_inverted_index"))
    published_at, missing_reason = _published(work.get("publication_date"))
    language = _language(work.get("language"))
    updated = work.get("updated_date")
    updated_text = updated.strip() if isinstance(updated, str) else ""
    institutions = _institution_ids(work.get("authorships"))
    topics = _topic_ids(work.get("topics"))
    doi = work.get("doi")
    doi_text = doi.strip() if isinstance(doi, str) else ""
    canonical = work.get("id")
    if isinstance(canonical, str) and canonical.startswith("https://"):
        url = canonical
    else:
        url = f"https://openalex.org/{work_id}"
    published_text = None if published_at is None else iso_z(published_at)
    digest = content_sha256(
        {
            "abstract": abstract,
            "institution_ids": institutions,
            "published_at": published_text,
            "title": title,
            "topic_ids": topics,
            "updated_date": updated_text,
            "work_id": work_id,
        }
    )
    # JsonDatetime accepts ISO strings, matching the JSON Schema contract.
    record = RawRecord.model_validate(
        {
            "schema_version": "1.0",
            "source_id": SourceId.OPENALEX.value,
            "source_record_id": work_id,
            "canonical_url": url,
            "title": title,
            "abstract_or_excerpt": abstract,
            "published_at": published_text,
            "published_at_missing_reason": missing_reason,
            "collected_at": iso_z(collected_at),
            "language": language,
            "license_status": LicenseStatus.METADATA_ONLY.value,
            "content_sha256": digest,
            "policy_snapshot_id": policy_snapshot_id,
        }
    )
    provenance = dump_provenance(
        {
            "doi": doi_text,
            "institution_ids": institutions,
            "title": title,
            "topic_ids": topics,
            "updated_date": updated_text,
            "work_id": work_id,
        }
    )
    return ParsedItem(record=record, provenance_json=provenance)


def _published(value: object) -> tuple[datetime | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, "source omitted publication date"
    try:
        return parse_timestamp(value), None
    except ValueError:
        return None, "source publication date is not ISO"


def _abstract(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return ""
    positions: list[tuple[int, str]] = []
    for word, slots in value.items():
        if not isinstance(word, str) or not isinstance(slots, list):
            continue
        for slot in slots:
            if isinstance(slot, bool) or not isinstance(slot, int):
                continue
            positions.append((slot, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def _institution_ids(value: object) -> list[str]:
    found: list[str] = []
    if not isinstance(value, list):
        return found
    for authorship in value:
        mapped = _as_dict(authorship)
        if mapped is None:
            continue
        institutions = mapped.get("institutions")
        if not isinstance(institutions, list):
            continue
        for institution in institutions:
            item = _as_dict(institution)
            if item is None:
                continue
            short = _short_id(item.get("id"))
            if short and short not in found:
                found.append(short)
    return found


def _topic_ids(value: object) -> list[str]:
    found: list[str] = []
    if not isinstance(value, list):
        return found
    for topic in value:
        mapped = _as_dict(topic)
        if mapped is None:
            continue
        short = _short_id(mapped.get("id"))
        if short and short not in found:
            found.append(short)
    return found


def _short_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    short = value.strip().rstrip("/").split("/")[-1]
    return short or None


def _language(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "und"


def _next_cursor(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: bad cursor")


def _filter(keyword: str, updated_since: str | None) -> str:
    text = f"default.search:{keyword}"
    if updated_since is not None:
        date_str = updated_since.strip().split("T")[0]
        return f"{text},from_publication_date:{date_str}"
    return text


def _search_text(value: str, label: str) -> str:
    text = value.strip()
    if not text or "," in text:
        raise ValueError(f"{label} must be a non-empty string without commas")
    return text


def _page_size(requested: int) -> int:
    if isinstance(requested, bool) or requested < 1:
        raise ValueError("per_page must be >= 1")
    return requested


def _as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    mapped: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            mapped[key] = item
    return mapped
