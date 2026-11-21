"""Compliant RSS collector; the phase-2 source is disabled by default."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from defusedxml import ElementTree

from techpulse.collectors.github import CollectorError
from techpulse.contracts import LicenseStatus, RawRecord, SourceId, TaskStatus
from techpulse.governance.egress import GuardedTransport
from techpulse.governance.policy import PolicyBlocked

_ROLE = "CollectorAgent"
_USER_AGENT = "TechPulse-AI/1.0 (https://github.com/techpulse; contact: info@techpulse.example.com)"


@dataclass(frozen=True)
class FeedPolicy:
    """A separately reviewed feed, including its robots and licence decision."""

    url: str
    policy_snapshot_id: str
    enabled: bool = False
    robots_allowed: bool = False
    license_status: LicenseStatus = LicenseStatus.UNKNOWN


@dataclass(frozen=True)
class RSSCollection:
    records: tuple[RawRecord, ...]
    cursor: str | None
    status: TaskStatus = TaskStatus.SUCCESS


@dataclass
class RSSCollector:
    registry_path: Path | None = None
    policy_path: Path | None = None
    transport: httpx.BaseTransport | None = None
    timeout: float = 10.0

    def __post_init__(self) -> None:
        self._enabled = self._registry_enabled()
        self._client = httpx.Client(
            transport=GuardedTransport(
                _ROLE,
                self.transport,
                registry_path=self.registry_path,
                policy_path=self.policy_path,
            ),
            headers={"User-Agent": _USER_AGENT},
            timeout=self.timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RSSCollector:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def collect(self, feed: FeedPolicy, cursor: str | None = None) -> RSSCollection:
        """Fetch one explicitly approved feed; unknown or unclear feeds stay blocked."""
        host = urlsplit(feed.url).hostname
        if (
            not self._enabled
            or not feed.enabled
            or not feed.robots_allowed
            or feed.license_status is not LicenseStatus.METADATA_ONLY
            or not host
        ):
            raise CollectorError(
                "RSS feed is disabled or has unclear terms", TaskStatus.POLICY_BLOCKED
            )
        try:
            response = self._client.get(feed.url, timeout=self.timeout)
        except PolicyBlocked:
            raise
        except httpx.HTTPError as exc:
            raise CollectorError("RSS network failure", TaskStatus.RETRYABLE_ERROR) from exc
        if response.status_code == 429:
            raise CollectorError(
                "RSS rate limit", TaskStatus.RETRYABLE_ERROR, _retry_after(response)
            )
        if response.status_code in {401, 403} or "captcha" in response.text.lower():
            raise CollectorError("RSS request blocked by policy", TaskStatus.POLICY_BLOCKED)
        if response.status_code >= 500:
            raise CollectorError("RSS server failure", TaskStatus.RETRYABLE_ERROR)
        if response.status_code >= 400:
            raise CollectorError("RSS request failed", TaskStatus.FATAL_ERROR)
        previous = _decode_cursor(cursor)
        candidates = tuple(self._record(item, feed) for item in _feed_items(response.content))
        records = tuple(record for record in candidates if _record_key(record) > previous)
        latest = max((_record_key(record) for record in candidates), default=previous)
        return RSSCollection(records, json.dumps(latest, separators=(",", ":")))

    def _record(self, item: dict[str, str], feed: FeedPolicy) -> RawRecord:
        source_url = item["link"]
        if urlsplit(source_url).scheme != "https":
            raise CollectorError("RSS item URL must use HTTPS", TaskStatus.POLICY_BLOCKED)
        title = item.get("title") or "Untitled feed item"
        published_at = _published_at(item.get("published") or item.get("updated"))
        return _raw_record(
            schema_version="1.0",
            source_id=SourceId.RSS,
            source_record_id=item.get("id") or hashlib.sha256(source_url.encode()).hexdigest(),
            canonical_url=source_url,
            title=title,
            abstract_or_excerpt="",
            published_at=published_at,
            published_at_missing_reason=None if published_at else "feed item has no timestamp",
            collected_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            language="und",
            license_status=feed.license_status,
            content_sha256=hashlib.sha256(f"{title}\n{source_url}".encode()).hexdigest(),
            policy_snapshot_id=feed.policy_snapshot_id,
        )

    def _registry_enabled(self) -> bool:
        default_path = Path(__file__).resolve().parents[3] / "config" / "source_registry.yaml"
        document = yaml.safe_load((self.registry_path or default_path).read_text(encoding="utf-8"))
        return document["sources"]["rss"]["enabled"] is True


def _feed_items(content: bytes) -> list[dict[str, str]]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise CollectorError("Invalid RSS or Atom document", TaskStatus.FATAL_ERROR) from exc
    items: list[dict[str, str]] = []
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name not in {"item", "entry"}:
            continue
        item: dict[str, str] = {}
        for child in element:
            key = child.tag.rsplit("}", 1)[-1]
            if key == "link":
                item[key] = child.get("href") or (child.text or "").strip()
            elif key in {"title", "guid", "id", "pubDate", "published", "updated"}:
                item["id" if key == "guid" else "published" if key == "pubDate" else key] = (
                    child.text or ""
                ).strip()
        if item.get("link"):
            items.append(item)
    return items


def _record_key(record: RawRecord) -> tuple[str, str]:
    return (record.published_at.isoformat() if record.published_at else "", record.source_record_id)


def _decode_cursor(cursor: str | None) -> tuple[str, str]:
    if not cursor:
        return ("", "")
    value = json.loads(cursor)
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) for item in value)
    ):
        raise ValueError("invalid RSS cursor")
    return (value[0], value[1])


def _published_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _raw_record(**values: Any) -> RawRecord:
    return RawRecord(**values)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
