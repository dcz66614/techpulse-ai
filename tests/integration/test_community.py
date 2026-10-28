"""C21 Hacker News and compliant RSS fixture tests; no live HTTP requests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from techpulse.collectors.github import CollectorError
from techpulse.collectors.hn import HNCollector
from techpulse.collectors.rss import FeedPolicy, RSSCollector
from techpulse.contracts import LicenseStatus, TaskStatus

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "source_registry.yaml"
POLICY = ROOT / "config" / "policy.yaml"
SCHEMA = Draft202012Validator(
    json.loads((ROOT / "contracts" / "raw_record.schema.json").read_text(encoding="utf-8"))
)


class SourceFields(BaseModel):
    source_url: str
    collected_at: str
    license_status: LicenseStatus


class CommunityTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "hacker-news.firebaseio.com":
            payload = json.loads(
                (ROOT / "tests" / "fixtures" / "hn" / "item.json").read_text(encoding="utf-8")
            )
            return httpx.Response(200, json=payload, request=request)
        content = (ROOT / "tests" / "fixtures" / "rss" / "feed.xml").read_bytes()
        return httpx.Response(200, content=content, request=request)


def test_hn_records_validate_and_discard_personal_fields() -> None:
    transport = CommunityTransport()
    with HNCollector(
        registry_path=REGISTRY,
        policy_path=POLICY,
        transport=transport,
        sleeper=lambda _seconds: None,
    ) as collector:
        result = collector.collect([4567001])
    record = result.records[0]
    serialized = record.model_dump_json()
    parsed = json.loads(serialized)
    SCHEMA.validate(parsed)
    source = SourceFields(
        source_url=record.canonical_url,
        collected_at=record.collected_at.isoformat(),
        license_status=record.license_status,
    )
    assert source.source_url == "https://example.org/benchmark"
    assert "unnecessary-username" not in serialized
    assert "private@example.test" not in serialized
    assert "Contact" not in serialized
    assert all(request.method == "GET" for request in transport.requests)


def test_hn_cursor_does_not_double_count_or_advance_on_failure() -> None:
    class FailingTransport(CommunityTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, request=request)

    with HNCollector(
        registry_path=REGISTRY,
        policy_path=POLICY,
        transport=FailingTransport(),
        sleeper=lambda _seconds: None,
    ) as collector:
        with pytest.raises(CollectorError) as failure:
            collector.collect([4567001], 4567000)
        assert failure.value.status is TaskStatus.RETRYABLE_ERROR

    with HNCollector(
        registry_path=REGISTRY,
        policy_path=POLICY,
        transport=CommunityTransport(),
        sleeper=lambda _seconds: None,
    ) as collector:
        result = collector.collect([4567001], 4567001)
    assert result.records == ()
    assert result.cursor == "4567001"


def test_rss_is_phase_2_disabled_and_unknown_feeds_make_no_requests() -> None:
    transport = CommunityTransport()
    feed = FeedPolicy(
        url="https://feeds.example.test/feed.xml",
        policy_snapshot_id="unknown-feed",
        enabled=True,
        robots_allowed=True,
        license_status=LicenseStatus.METADATA_ONLY,
    )
    with RSSCollector(registry_path=REGISTRY, policy_path=POLICY, transport=transport) as collector:
        with pytest.raises(CollectorError) as failure:
            collector.collect(feed)
    assert failure.value.status is TaskStatus.POLICY_BLOCKED
    assert transport.requests == []


def test_reviewed_rss_path_emits_metadata_only_records() -> None:
    with TemporaryDirectory() as directory:
        registry = Path(directory) / "source_registry.yaml"
        text = (
            REGISTRY.read_text(encoding="utf-8")
            .replace('    base_url: "not stated"', '    base_url: "https://feeds.example.test"')
            .replace(
                '    enabled: false\n    disable_reason: "Phase 2 source;',
                '    enabled: true\n    disable_reason: "Phase 2 source;',
            )
        )
        registry.write_text(text, encoding="utf-8")
        transport = CommunityTransport()
        feed = FeedPolicy(
            url="https://feeds.example.test/feed.xml",
            policy_snapshot_id="reviewed-feed-v1",
            enabled=True,
            robots_allowed=True,
            license_status=LicenseStatus.METADATA_ONLY,
        )
        with RSSCollector(
            registry_path=registry, policy_path=POLICY, transport=transport
        ) as collector:
            result = collector.collect(feed)
        record = result.records[0]
        serialized = record.model_dump_json()
        SCHEMA.validate(json.loads(serialized))
        assert record.license_status is LicenseStatus.METADATA_ONLY
        assert record.abstract_or_excerpt == ""
        assert "private@example.test" not in serialized
        assert "copyrighted article" not in serialized


def test_rss_cursor_replay_returns_no_previous_records() -> None:
    with TemporaryDirectory() as directory:
        registry = Path(directory) / "source_registry.yaml"
        text = (
            REGISTRY.read_text(encoding="utf-8")
            .replace('    base_url: "not stated"', '    base_url: "https://feeds.example.test"')
            .replace(
                '    enabled: false\n    disable_reason: "Phase 2 source;',
                '    enabled: true\n    disable_reason: "Phase 2 source;',
            )
        )
        registry.write_text(text, encoding="utf-8")
        feed = FeedPolicy(
            url="https://feeds.example.test/feed.xml",
            policy_snapshot_id="reviewed-feed-v1",
            enabled=True,
            robots_allowed=True,
            license_status=LicenseStatus.METADATA_ONLY,
        )
        with RSSCollector(
            registry_path=registry, policy_path=POLICY, transport=CommunityTransport()
        ) as collector:
            first = collector.collect(feed)
            replay = collector.collect(feed, first.cursor)
    assert first.records
    assert replay.records == ()
    assert replay.cursor == first.cursor


@pytest.mark.parametrize("source", ["hn", "rss"])
@pytest.mark.parametrize("retry_after", ["120", "http-date"])
def test_retry_after_accepts_seconds_and_http_date(source: str, retry_after: str) -> None:
    header = (
        format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
        if retry_after == "http-date"
        else retry_after
    )

    class LimitedTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": header}, request=request)

    transport = LimitedTransport()
    if source == "hn":
        with HNCollector(
            registry_path=REGISTRY,
            policy_path=POLICY,
            transport=transport,
            sleeper=lambda _seconds: None,
        ) as hn_collector:
            with pytest.raises(CollectorError) as failure:
                hn_collector.collect([4567001])
    else:
        with TemporaryDirectory() as directory:
            registry = Path(directory) / "source_registry.yaml"
            text = (
                REGISTRY.read_text(encoding="utf-8")
                .replace('    base_url: "not stated"', '    base_url: "https://feeds.example.test"')
                .replace(
                    '    enabled: false\n    disable_reason: "Phase 2 source;',
                    '    enabled: true\n    disable_reason: "Phase 2 source;',
                )
            )
            registry.write_text(text, encoding="utf-8")
            feed = FeedPolicy(
                url="https://feeds.example.test/feed.xml",
                policy_snapshot_id="reviewed-feed-v1",
                enabled=True,
                robots_allowed=True,
                license_status=LicenseStatus.METADATA_ONLY,
            )
            with RSSCollector(
                registry_path=registry, policy_path=POLICY, transport=transport
            ) as rss_collector:
                with pytest.raises(CollectorError) as failure:
                    rss_collector.collect(feed)
    assert failure.value.retry_after is not None
    assert 118 <= failure.value.retry_after <= 120
