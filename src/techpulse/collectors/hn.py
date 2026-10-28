"""Hacker News read-only collector."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from techpulse.collectors.github import CollectorError
from techpulse.contracts import LicenseStatus, RawRecord, SourceId, TaskStatus
from techpulse.governance.egress import GuardedTransport
from techpulse.governance.policy import PolicyBlocked

_ROLE = "CollectorAgent"
_API_ROOT = "https://hacker-news.firebaseio.com/v0"
_USER_AGENT = "TechPulse-AI/1.0 (https://github.com/techpulse; contact: info@techpulse.example.com)"


@dataclass(frozen=True)
class HNCollection:
    records: tuple[RawRecord, ...]
    cursor: str
    status: TaskStatus = TaskStatus.SUCCESS


@dataclass
class HNCollector:
    registry_path: Path | None = None
    policy_path: Path | None = None
    transport: httpx.BaseTransport | None = None
    sleeper: Callable[[float], None] = time.sleep
    timeout: float = 10.0

    def __post_init__(self) -> None:
        self._enabled, self._snapshot, self._interval = self._load_policy()
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
        self._last_request_at: float | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HNCollector:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def collect(self, item_ids: list[int], cursor: int | None = None) -> HNCollection:
        """Collect only public item metadata; user profiles and authors are discarded."""
        if not self._enabled:
            raise CollectorError("Hacker News source is disabled", TaskStatus.POLICY_BLOCKED)
        highest = cursor or 0
        records: list[RawRecord] = []
        for item_id in item_ids:
            if item_id <= (cursor or 0):
                continue
            item = self._get_json(f"/item/{item_id}.json")
            records.append(self._record(item_id, item))
            highest = max(highest, item_id)
        return HNCollection(tuple(records), str(highest))

    def _record(self, item_id: int, item: Any) -> RawRecord:
        if not isinstance(item, dict):
            raise CollectorError("Invalid Hacker News item", TaskStatus.FATAL_ERROR)
        kind = item.get("type")
        if kind not in {"story", "comment", "job", "poll", "pollopt"}:
            raise CollectorError("Invalid Hacker News item type", TaskStatus.FATAL_ERROR)
        title = str(item.get("title") or f"Hacker News {kind} {item_id}")
        source_url = item.get("url") or f"https://news.ycombinator.com/item?id={item_id}"
        timestamp = item.get("time")
        published_at = (
            datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")
            if isinstance(timestamp, int)
            else None
        )
        excerpt = (
            f"Hacker News {kind}; score: {item.get('score', 0)}; "
            f"comments: {item.get('descendants', 0)}. Public metadata only."
        )
        return _raw_record(
            schema_version="1.0",
            source_id=SourceId.HN,
            source_record_id=str(item_id),
            canonical_url=str(source_url),
            title=title,
            abstract_or_excerpt=excerpt,
            published_at=published_at,
            published_at_missing_reason=None if published_at else "item has no public timestamp",
            collected_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            language="en",
            license_status=LicenseStatus.METADATA_ONLY,
            content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
            policy_snapshot_id=self._snapshot,
        )

    def _get_json(self, path: str) -> Any:
        self._throttle()
        try:
            response = self._client.get(f"{_API_ROOT}{path}", timeout=self.timeout)
        except PolicyBlocked:
            raise
        except httpx.HTTPError as exc:
            raise CollectorError("Hacker News network failure", TaskStatus.RETRYABLE_ERROR) from exc
        if response.status_code == 429:
            raise CollectorError(
                "Hacker News rate limit", TaskStatus.RETRYABLE_ERROR, _retry_after(response)
            )
        if response.status_code in {401, 403} or "captcha" in response.text.lower():
            raise CollectorError("Hacker News request blocked by policy", TaskStatus.POLICY_BLOCKED)
        if response.status_code >= 500:
            raise CollectorError("Hacker News server failure", TaskStatus.RETRYABLE_ERROR)
        if response.status_code >= 400:
            raise CollectorError("Hacker News request failed", TaskStatus.FATAL_ERROR)
        return response.json()

    def _throttle(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            self.sleeper(max(0.0, self._interval - (now - self._last_request_at)))
        self._last_request_at = time.monotonic()

    def _load_policy(self) -> tuple[bool, str, float]:
        default_path = Path(__file__).resolve().parents[3] / "config" / "source_registry.yaml"
        document = yaml.safe_load((self.registry_path or default_path).read_text(encoding="utf-8"))
        source = document["sources"]["hackernews"]
        throttle = source["rate_limits"]["self_imposed_throttle"]
        return (
            source["enabled"] is True,
            str(source["policy_snapshot_id"]),
            60.0 / float(throttle["requests_per_minute"]),
        )


def _raw_record(**values: Any) -> RawRecord:
    return RawRecord(**values)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None
