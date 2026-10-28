"""Shared read-only collector primitives.

Watermarks and seen-record provenance live in SQLite. A failed page does not
advance the watermark. Every outbound GET uses the S20 guarded transport.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from techpulse.contracts import RawRecord, TaskStatus
from techpulse.governance.egress import GuardedTransport
from techpulse.governance.policy import PolicyBlocked, normalize_domain

COLLECTOR_ROLE = "collector"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

Clock = Callable[[], float]
Sleep = Callable[[float], None]


class CollectorFailure(Exception):
    """A collect attempt stopped. The watermark is left on the last success."""

    def __init__(self, status: TaskStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    base_url: str
    policy_snapshot_id: str
    enabled: bool
    user_agent: str
    mailto: str
    min_interval_seconds: float
    extra_headers: Mapping[str, str]
    domains: frozenset[str]
    per_page_max: int | None = None
    slice_max: int | None = None
    total_max: int | None = None


@dataclass(frozen=True)
class SeenRow:
    source_record_id: str
    content_sha256: str
    provenance_json: str
    collected_at: str


@dataclass
class ParsedItem:
    record: RawRecord
    provenance_json: str


@dataclass
class ParsedPage:
    items: list[ParsedItem]
    next_cursor: str
    warnings: list[str] = field(default_factory=list)
    total: int | None = None


@dataclass
class Fetched:
    status_code: int
    headers: dict[str, str]
    text: str


@dataclass
class CollectResult:
    status: TaskStatus
    records: list[RawRecord]
    new_count: int
    cursor: str | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, int | float | str] = field(default_factory=dict)


class ManualClock:
    """Monotonic clock that advances only when sleep is called."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class IntervalLimiter:
    """Keep a minimum gap between requests. Tests inject clock and sleep.

    Header feedback and Retry-After can only lengthen the gap.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        self.min_interval_seconds = min_interval_seconds
        self._clock = time.monotonic if clock is None else clock
        self._sleep = time.sleep if sleep is None else sleep
        self._next_allowed = 0.0
        self.waits: list[float] = []
        self.retry_afters: list[float] = []

    def wait(self) -> float:
        now = self._clock()
        delay = self._next_allowed - now
        if delay < 0:
            delay = 0.0
        if delay > 0:
            self._sleep(delay)
            now = self._clock()
        self.waits.append(delay)
        boundary = now + self.min_interval_seconds
        if boundary > self._next_allowed:
            self._next_allowed = boundary
        return delay

    def note_retry_after(self, seconds: float) -> None:
        """The next wait() sleeps at least this Retry-After, and the floor."""
        if seconds < 0:
            raise ValueError("Retry-After must be >= 0")
        self.retry_afters.append(seconds)
        now = self._clock()
        target = now + max(seconds, self.min_interval_seconds)
        if target > self._next_allowed:
            self._next_allowed = target

    def observe_headers(self, headers: Mapping[str, str]) -> None:
        """Slow down from the source's own rate-limit headers. Never speed up."""
        lowered = {key.lower(): value for key, value in headers.items()}
        limit_raw = lowered.get("x-rate-limit-limit")
        interval_raw = lowered.get("x-rate-limit-interval")
        if limit_raw and interval_raw:
            required = _required_interval(limit_raw, interval_raw)
            if required is not None and required > self.min_interval_seconds:
                self.min_interval_seconds = required
                now = self._clock()
                target = now + required
                if target > self._next_allowed:
                    self._next_allowed = target
        remaining = lowered.get("x-ratelimit-remaining")
        reset = lowered.get("x-ratelimit-reset")
        if remaining is None or reset is None:
            return
        try:
            left = float(remaining)
            reset_s = float(reset)
        except ValueError:
            return
        if left <= 0 and reset_s > 0:
            now = self._clock()
            target = now + reset_s
            if target > self._next_allowed:
                self._next_allowed = target


class CursorStore:
    """SQLite watermarks and provenance. INSERT OR IGNORE keeps older rows."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS watermarks (
                source_id TEXT NOT NULL,
                query_key TEXT NOT NULL,
                cursor TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_id, query_key)
            );
            CREATE TABLE IF NOT EXISTS seen_records (
                source_id TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                PRIMARY KEY (source_id, source_record_id, content_sha256)
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CursorStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get_cursor(self, source_id: str, query_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT cursor FROM watermarks WHERE source_id = ? AND query_key = ?",
            (source_id, query_key),
        ).fetchone()
        if row is None:
            return None
        value = row["cursor"]
        if not isinstance(value, str):
            return None
        return value

    def seen_count(self, source_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM seen_records WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return 0
        return int(row["n"])

    def rows(self, source_id: str, source_record_id: str) -> list[SeenRow]:
        found = self._conn.execute(
            """
            SELECT source_record_id, content_sha256, provenance_json, collected_at
            FROM seen_records
            WHERE source_id = ? AND source_record_id = ?
            ORDER BY rowid
            """,
            (source_id, source_record_id),
        ).fetchall()
        rows: list[SeenRow] = []
        for item in found:
            record_id = item["source_record_id"]
            digest = item["content_sha256"]
            provenance = item["provenance_json"]
            collected_at = item["collected_at"]
            if not all(
                isinstance(part, str) for part in (record_id, digest, provenance, collected_at)
            ):
                continue
            rows.append(
                SeenRow(
                    source_record_id=str(record_id),
                    content_sha256=str(digest),
                    provenance_json=str(provenance),
                    collected_at=str(collected_at),
                )
            )
        return rows

    def commit_success(
        self,
        source_id: str,
        query_key: str,
        cursor: str,
        items: list[ParsedItem],
        *,
        id_dedup: bool,
        updated_at: str,
    ) -> list[tuple[str, str]]:
        """Insert new rows and advance the watermark in one transaction.

        An existing (source, id, hash) row is left as it was. When id_dedup is
        set, a source_record_id that is already stored is skipped entirely.
        """
        inserted: list[tuple[str, str]] = []
        try:
            with self._conn:
                seen_ids: set[str] = set()
                for item in items:
                    record_id = item.record.source_record_id
                    digest = item.record.content_sha256
                    if id_dedup and (record_id in seen_ids or self._has_id(source_id, record_id)):
                        continue
                    seen_ids.add(record_id)
                    before = self._conn.total_changes
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO seen_records (
                            source_id, source_record_id, content_sha256,
                            provenance_json, collected_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            source_id,
                            record_id,
                            digest,
                            item.provenance_json,
                            iso_z(item.record.collected_at),
                        ),
                    )
                    if self._conn.total_changes > before:
                        inserted.append((record_id, digest))
                self._conn.execute(
                    """
                    INSERT INTO watermarks (source_id, query_key, cursor, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source_id, query_key) DO UPDATE SET
                        cursor = excluded.cursor,
                        updated_at = excluded.updated_at
                    """,
                    (source_id, query_key, cursor, updated_at),
                )
        except sqlite3.Error as exc:
            raise CollectorFailure(
                TaskStatus.RETRYABLE_ERROR, "retryable_error: cursor store"
            ) from exc
        return inserted

    def _has_id(self, source_id: str, source_record_id: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM seen_records
            WHERE source_id = ? AND source_record_id = ?
            LIMIT 1
            """,
            (source_id, source_record_id),
        ).fetchone()
        return row is not None


def default_registry_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "source_registry.yaml"


def load_source_config(source_id: str, registry_path: Path | None = None) -> SourceConfig:
    path = default_registry_path() if registry_path is None else registry_path
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("source registry cannot be read") from exc
    sources = _mapping(_mapping(loaded).get("sources"))
    if source_id not in sources:
        raise ValueError(f"source {source_id} is not registered")
    entry = _mapping(sources.get(source_id))
    base_url = _required_str(entry, "base_url")
    headers = _mapping(entry.get("required_headers"))
    rate = _mapping(entry.get("rate_limits"))
    paging_raw = rate.get("paging_limits")
    paging = {} if paging_raw is None else _mapping(paging_raw)
    domains = {_normalize_host(item) for item in _str_list(entry.get("domain_whitelist"))}
    domains.add(_normalize_host(base_url))
    rps = rate.get("requests_per_second")
    if isinstance(rps, bool) or not isinstance(rps, int | float) or rps <= 0:
        raise ValueError(f"source {source_id} has no requests_per_second")
    extra: dict[str, str] = {}
    raw_extra = headers.get("extra_headers", {})
    if isinstance(raw_extra, dict):
        for key, value in raw_extra.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if "<" in value or ">" in value:
                continue
            extra[key] = value
    return SourceConfig(
        source_id=source_id,
        base_url=base_url.rstrip("/"),
        policy_snapshot_id=_required_str(entry, "policy_snapshot_id"),
        enabled=entry.get("enabled") is True,
        user_agent=_required_str(headers, "user_agent"),
        mailto=_required_str(headers, "mailto"),
        min_interval_seconds=1.0 / float(rps),
        extra_headers=extra,
        domains=frozenset(domains),
        per_page_max=_optional_int(paging, "per_page_max", "rows_max"),
        slice_max=_optional_int(paging, "max_results_per_slice"),
        total_max=_optional_int(paging, "max_results_per_query"),
    )


def build_client(
    config: SourceConfig,
    *,
    transport: httpx.BaseTransport | None,
    registry_path: Path,
    timeout: httpx.Timeout,
    accept: str,
) -> httpx.Client:
    """httpx client whose transport rejects anything the collector role cannot GET."""
    headers = {"User-Agent": config.user_agent, "Accept": accept}
    for key, value in config.extra_headers.items():
        headers[key] = value
    inner = httpx.HTTPTransport() if transport is None else transport
    guarded = GuardedTransport(COLLECTOR_ROLE, inner, registry_path=registry_path)
    return httpx.Client(transport=guarded, timeout=timeout, headers=headers, follow_redirects=False)


def guarded_get(
    client: httpx.Client,
    limiter: IntervalLimiter,
    config: SourceConfig,
    url: str,
    *,
    params: Mapping[str, str],
    max_retries: int,
) -> Fetched:
    """GET one URL. 429 waits Retry-After. 401/403/captcha is policy_blocked.

    Recoverable network errors and 5xx retry at most `max_retries` times, then
    raise retryable_error. Redirects are not followed.
    """
    require_source_host(config, url)
    attempt = 0
    while True:
        limiter.wait()
        try:
            response = client.get(url, params=params)
        except httpx.TimeoutException as exc:
            if attempt >= max_retries:
                raise CollectorFailure(
                    TaskStatus.RETRYABLE_ERROR, "retryable_error: timeout"
                ) from exc
            attempt += 1
            continue
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise CollectorFailure(
                    TaskStatus.RETRYABLE_ERROR, "retryable_error: network"
                ) from exc
            attempt += 1
            continue
        except PolicyBlocked as exc:
            raise CollectorFailure(TaskStatus.POLICY_BLOCKED, "policy_blocked: egress") from exc
        fetched = _fetched(response)
        if _is_captcha(fetched.text):
            raise CollectorFailure(TaskStatus.POLICY_BLOCKED, "policy_blocked: captcha")
        if fetched.status_code in {401, 403}:
            raise CollectorFailure(
                TaskStatus.POLICY_BLOCKED,
                f"policy_blocked: {fetched.status_code}",
            )
        if 300 <= fetched.status_code < 400:
            raise CollectorFailure(TaskStatus.POLICY_BLOCKED, "policy_blocked: redirect")
        if fetched.status_code == 429:
            if attempt >= max_retries:
                raise CollectorFailure(TaskStatus.RETRYABLE_ERROR, "retryable_error: 429")
            delay = parse_retry_after(fetched.headers)
            limiter.note_retry_after(1.0 if delay is None else delay)
            attempt += 1
            continue
        if fetched.status_code >= 500:
            if attempt >= max_retries:
                raise CollectorFailure(
                    TaskStatus.RETRYABLE_ERROR,
                    f"retryable_error: http {fetched.status_code}",
                )
            attempt += 1
            continue
        if fetched.status_code >= 400:
            raise CollectorFailure(
                TaskStatus.POLICY_BLOCKED,
                f"policy_blocked: {fetched.status_code}",
            )
        limiter.observe_headers(fetched.headers)
        return fetched


def collect_pages(
    *,
    config: SourceConfig,
    store: CursorStore,
    query_key: str,
    explicit_cursor: str | None,
    initial_cursor: str,
    fetch_page: Callable[[str], ParsedPage],
    id_dedup: bool,
    collected_at: datetime,
    artifacts_dir: Path | None,
    run_id: str | None,
    ticket: str,
) -> CollectResult:
    """Walk pages. Commit each successful page before requesting the next one."""
    stored = store.get_cursor(config.source_id, query_key)
    if explicit_cursor is None and stored == "":
        result = _result(
            status=TaskStatus.SUCCESS,
            records=[],
            new_count=0,
            cursor="",
            pages=0,
        )
        _write_if_requested(result, artifacts_dir, run_id, ticket)
        return result
    if explicit_cursor is not None:
        cursor = explicit_cursor
    elif stored is not None:
        cursor = stored
    else:
        cursor = initial_cursor
    records: list[RawRecord] = []
    warnings: list[str] = []
    new_count = 0
    pages = 0
    try:
        while True:
            page = fetch_page(cursor)
            pages += 1
            warnings.extend(page.warnings)
            inserted = store.commit_success(
                config.source_id,
                query_key,
                page.next_cursor,
                _unique_items(page.items),
                id_dedup=id_dedup,
                updated_at=iso_z(collected_at),
            )
            inserted_keys = set(inserted)
            for item in page.items:
                key = (item.record.source_record_id, item.record.content_sha256)
                if key in inserted_keys:
                    records.append(item.record)
                    inserted_keys.remove(key)
            new_count += len(inserted)
            cursor = page.next_cursor
            if cursor == "":
                break
    except CollectorFailure as exc:
        result = _result(
            status=exc.status,
            records=records,
            new_count=new_count,
            cursor=store.get_cursor(config.source_id, query_key),
            errors=[exc.message],
            warnings=warnings,
            pages=pages,
        )
        _write_if_requested(result, artifacts_dir, run_id, ticket)
        return result
    result = _result(
        status=TaskStatus.SUCCESS,
        records=records,
        new_count=new_count,
        cursor=cursor,
        warnings=warnings,
        pages=pages,
    )
    _write_if_requested(result, artifacts_dir, run_id, ticket)
    return result


def require_source_host(config: SourceConfig, url: str) -> None:
    parts = urlsplit(url.strip())
    if parts.hostname is None:
        raise CollectorFailure(TaskStatus.POLICY_BLOCKED, "policy_blocked: missing host")
    try:
        host = normalize_domain(parts.hostname)
    except ValueError as exc:
        raise CollectorFailure(TaskStatus.POLICY_BLOCKED, "policy_blocked: bad host") from exc
    if host not in config.domains:
        raise CollectorFailure(TaskStatus.POLICY_BLOCKED, "policy_blocked: unapproved host")


def parse_retry_after(headers: Mapping[str, str], *, now: datetime | None = None) -> float | None:
    lowered = {key.lower(): value for key, value in headers.items()}
    raw = lowered.get("retry-after")
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        seconds = float(text)
    except ValueError:
        seconds = -1.0
    if seconds >= 0:
        return seconds
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = datetime.now(UTC) if now is None else now
    return max(0.0, (when - current).total_seconds())


def content_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def dump_provenance(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def query_key(parts: Mapping[str, str]) -> str:
    return content_sha256(dict(parts))


def iso_z(value: datetime) -> str:
    text = value.astimezone(UTC).isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def write_run_artifacts(
    artifacts_dir: Path,
    run_id: str,
    ticket: str,
    records: list[RawRecord],
    metrics: Mapping[str, int | float | str],
) -> Path:
    directory = artifacts_dir / run_id / ticket
    directory.mkdir(parents=True, exist_ok=True)
    lines = [record.model_dump_json() for record in records]
    body = "".join(f"{line}\n" for line in lines)
    raw_path = directory / "raw.jsonl"
    raw_path.write_text(body, encoding="utf-8")
    manifest = {
        "ticket": ticket,
        "rows": len(records),
        "raw_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "raw_name": "raw.jsonl",
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "metrics.json").write_text(
        json.dumps(dict(metrics), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return directory


def disabled_result(config: SourceConfig, store: CursorStore, key: str) -> CollectResult:
    return _result(
        status=TaskStatus.POLICY_BLOCKED,
        records=[],
        new_count=0,
        cursor=store.get_cursor(config.source_id, key),
        errors=["policy_blocked: source disabled"],
        pages=0,
    )


def registry_failure(message: str) -> CollectResult:
    return _result(
        status=TaskStatus.POLICY_BLOCKED,
        records=[],
        new_count=0,
        cursor=None,
        errors=[message],
        pages=0,
    )


def _result(
    *,
    status: TaskStatus,
    records: list[RawRecord],
    new_count: int,
    cursor: str | None,
    pages: int,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> CollectResult:
    return CollectResult(
        status=status,
        records=records,
        new_count=new_count,
        cursor=cursor,
        errors=[] if errors is None else errors,
        warnings=[] if warnings is None else warnings,
        metrics={
            "pages": pages,
            "new_count": new_count,
            "third_party_writes": 0,
            "status": status.value,
        },
    )


def _write_if_requested(
    result: CollectResult,
    artifacts_dir: Path | None,
    run_id: str | None,
    ticket: str,
) -> None:
    if artifacts_dir is None or run_id is None or not run_id.strip():
        return
    write_run_artifacts(artifacts_dir, run_id, ticket, result.records, result.metrics)


def _unique_items(items: list[ParsedItem]) -> list[ParsedItem]:
    unique: list[ParsedItem] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.record.source_record_id, item.record.content_sha256)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _fetched(response: httpx.Response) -> Fetched:
    try:
        text = response.text
        headers = {key: value for key, value in response.headers.items()}
        status_code = response.status_code
    finally:
        response.close()
    return Fetched(status_code=status_code, headers=headers, text=text)


def _is_captcha(body: str) -> bool:
    return "captcha" in body.lower()


def _required_interval(limit_raw: str, interval_raw: str) -> float | None:
    try:
        limit = float(limit_raw)
        interval = _parse_interval_seconds(interval_raw)
    except ValueError:
        return None
    if limit <= 0 or interval <= 0:
        return None
    return interval / limit


def _parse_interval_seconds(value: str) -> float:
    text = value.strip().lower()
    if text.endswith("ms"):
        return float(text[:-2]) / 1000.0
    if text.endswith("s"):
        return float(text[:-1])
    if text.endswith("m"):
        return float(text[:-1]) * 60.0
    return float(text)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected a mapping")
    mapped: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("expected string keys")
        mapped[key] = item
    return mapped


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing {key}")
    return value.strip()


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("expected a list")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("expected strings")
        items.append(item.strip())
    return items


def _optional_int(mapping: Mapping[str, object], *keys: str) -> int | None:
    for key in keys:
        if key not in mapping:
            continue
        value = mapping[key]
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value > 0:
            return value
    return None


def _normalize_host(value: str) -> str:
    return normalize_domain(value)
