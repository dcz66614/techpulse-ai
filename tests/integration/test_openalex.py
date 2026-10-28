"""OpenAlex collector tests. HTTP responses come from recorded fixtures only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from techpulse.collectors.base import (
    DEFAULT_TIMEOUT,
    CollectorFailure,
    CursorStore,
    IntervalLimiter,
    ManualClock,
    build_client,
    guarded_get,
    iso_z,
    load_source_config,
    parse_retry_after,
    query_key,
)
from techpulse.collectors.openalex import collect_openalex
from techpulse.contracts import RawRecord, TaskStatus
from techpulse.governance.policy import PolicyBlocked

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "source_registry.yaml"
FIXTURES = ROOT / "tests" / "fixtures" / "openalex"
WHEN = datetime(2026, 10, 27, tzinfo=UTC)
LATER = datetime(2026, 10, 28, tzinfo=UTC)


def _schema() -> dict[str, Any]:
    raw = json.loads((ROOT / "contracts" / "raw_record.schema.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AssertionError("raw record schema is not an object")
    return raw


VALIDATOR = Draft202012Validator(_schema())


def load_recorded(path: Path) -> httpx.Response:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"{path.name} must be a JSON object")
    status_raw = loaded.get("status")
    body = loaded.get("body")
    headers_raw = loaded.get("headers", {})
    if isinstance(status_raw, bool) or not isinstance(status_raw, int):
        raise AssertionError(f"{path.name} status must be an int")
    if not isinstance(headers_raw, dict):
        raise AssertionError(f"{path.name} headers must be an object")
    headers: dict[str, str] = {}
    for key, value in headers_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise AssertionError(f"{path.name} headers must be strings")
        headers[key] = value
    if isinstance(body, str):
        content = body
    else:
        content = json.dumps(body)
        headers.setdefault("Content-Type", "application/json")
    return httpx.Response(status_raw, headers=headers, text=content)


def _key(per_page: int = 100, keyword: str = "retrieval", updated_since: str = "") -> str:
    return query_key(
        {
            "source": "openalex",
            "keyword": keyword,
            "per_page": str(per_page),
            "updated_since": updated_since,
        }
    )


def _requests_for(
    paths_by_cursor: dict[str, Path],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = request.url.params["cursor"]
        path = paths_by_cursor.get(str(cursor))
        if path is None:
            raise AssertionError(f"unexpected cursor {cursor}")
        return load_recorded(path)

    return httpx.MockTransport(handler), seen


def _fixed(path: Path) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return load_recorded(path)

    return httpx.MockTransport(handler), seen


def _assert_raw(record: RawRecord) -> None:
    payload = record.model_dump(mode="json")
    assert list(VALIDATOR.iter_errors(payload)) == []


def _assert_gets(requests: list[httpx.Request]) -> None:
    assert requests
    for request in requests:
        assert request.method == "GET"
        assert request.url.host == "api.openalex.org"
        assert request.url.path == "/works"


def test_hundred_record_fixture_validates_and_replays() -> None:
    fixture = FIXTURES / "works_100.json"
    assert fixture.stat().st_size < 100_000
    transport, seen = _fixed(fixture)
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with CursorStore(root / "cursors.sqlite") as store:
            first = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                cursor="*",
                per_page=100,
                collected_at=WHEN,
                artifacts_dir=root / "artifacts",
                run_id="c10-run",
            )
            assert first.status is TaskStatus.SUCCESS
            assert first.new_count == 100
            assert len(first.records) == 100
            assert first.metrics["third_party_writes"] == 0
            config = load_source_config("openalex", REGISTRY)
            empties = 0
            missing_dates = 0
            for record in first.records:
                _assert_raw(record)
                assert record.source_id.value == "openalex"
                assert record.license_status.value == "metadata_only"
                assert record.policy_snapshot_id == config.policy_snapshot_id
                assert record.collected_at == WHEN
                if record.abstract_or_excerpt == "":
                    empties += 1
                if record.published_at is None:
                    missing_dates += 1
                    assert record.published_at_missing_reason == "source omitted publication date"
            assert empties > 0
            assert missing_dates == 1
            lead = next(
                record for record in first.records if record.source_record_id == "W0000000001"
            )
            assert lead.abstract_or_excerpt == "Retrieval augmented ranking"
            provenance = json.loads(store.rows("openalex", "W0000000001")[0].provenance_json)
            assert provenance["institution_ids"] == ["I136199984"]
            assert provenance["topic_ids"] == ["T10001"]
            raw_path = root / "artifacts" / "c10-run" / "C10" / "raw.jsonl"
            manifest = json.loads((raw_path.parent / "manifest.json").read_text(encoding="utf-8"))
            metrics = json.loads((raw_path.parent / "metrics.json").read_text(encoding="utf-8"))
            lines = raw_path.read_text(encoding="utf-8").splitlines()
            assert len(lines) == 100
            assert manifest["rows"] == 100
            assert metrics["new_count"] == 100
            assert metrics["third_party_writes"] == 0
            second = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                cursor="*",
                per_page=100,
                collected_at=LATER,
            )
            assert second.status is TaskStatus.SUCCESS
            assert second.new_count == 0
            assert second.records == []
            assert store.seen_count("openalex") == 100
    _assert_gets(seen)


def test_overlap_across_pages_is_not_counted_twice() -> None:
    transport, seen = _requests_for(
        {"*": FIXTURES / "page1.json", "page-2": FIXTURES / "page2.json"}
    )
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            first = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                cursor="*",
                per_page=2,
                collected_at=WHEN,
            )
            assert first.status is TaskStatus.SUCCESS
            assert first.new_count == 3
            assert store.seen_count("openalex") == 3
            empty = next(
                record for record in first.records if record.source_record_id == "W0000000002"
            )
            assert empty.abstract_or_excerpt == ""
            again = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                cursor="*",
                per_page=2,
                collected_at=LATER,
            )
            assert again.new_count == 0
            assert store.seen_count("openalex") == 3
    _assert_gets(seen)


def test_failure_does_not_advance_past_the_failed_page() -> None:
    seen: list[httpx.Request] = []
    page2_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = str(request.url.params["cursor"])
        if cursor == "*":
            return load_recorded(FIXTURES / "page1.json")
        if cursor != "page-2":
            raise AssertionError(cursor)
        page2_calls["n"] += 1
        if page2_calls["n"] <= 3:
            return load_recorded(FIXTURES / "unavailable.json")
        return load_recorded(FIXTURES / "page2.json")

    transport = httpx.MockTransport(handler)
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            clock = ManualClock()
            failed = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                per_page=2,
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert failed.status is TaskStatus.RETRYABLE_ERROR
            assert failed.cursor == "page-2"
            assert store.get_cursor("openalex", _key(2)) == "page-2"
            assert failed.new_count == 2
            assert store.seen_count("openalex") == 2
            resumed = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                per_page=2,
                collected_at=LATER,
            )
            assert resumed.status is TaskStatus.SUCCESS
            assert resumed.new_count == 1
            assert {record.source_record_id for record in resumed.records} == {"W0000000003"}
            assert store.seen_count("openalex") == 3
            assert store.get_cursor("openalex", _key(2)) == ""
    assert page2_calls["n"] == 4
    _assert_gets(seen)


def test_429_honours_retry_after_then_collects() -> None:
    seen: list[httpx.Request] = []
    clock = ManualClock()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        calls["n"] += 1
        if calls["n"] == 1:
            return load_recorded(FIXTURES / "too_many.json")
        return load_recorded(FIXTURES / "one_work.json")

    recorded = json.loads((FIXTURES / "too_many.json").read_text(encoding="utf-8"))
    headers = recorded["headers"]
    assert isinstance(headers, dict)
    expected = float(str(headers["Retry-After"]))
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 1
            assert store.get_cursor("openalex", _key()) == ""
    assert clock.slept == [expected]
    _assert_gets(seen)


def test_exhausted_429_does_not_advance_the_cursor() -> None:
    seen: list[httpx.Request] = []
    clock = ManualClock()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return load_recorded(FIXTURES / "too_many.json")

    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.RETRYABLE_ERROR
            assert result.errors == ["retryable_error: 429"]
            assert result.new_count == 0
            assert store.seen_count("openalex") == 0
            assert store.get_cursor("openalex", _key()) is None
    assert len(seen) == 3
    assert clock.slept == [5.0, 5.0]


@pytest.mark.parametrize(
    "name",
    ["unauthorized.json", "forbidden.json", "captcha.json"],
)
def test_auth_and_captcha_are_policy_blocked(name: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return load_recorded(FIXTURES / name)

    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.POLICY_BLOCKED
            assert result.records == []
            assert store.seen_count("openalex") == 0
            assert store.get_cursor("openalex", _key()) is None
    assert len(seen) == 1
    _assert_gets(seen)


def test_rate_limit_reset_header_delays_the_next_page() -> None:
    transport, seen = _requests_for(
        {"*": FIXTURES / "low_remaining.json", "after": FIXTURES / "after_reset.json"}
    )
    clock = ManualClock()
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 2
    assert clock.slept == [4.0]
    _assert_gets(seen)


def test_updated_work_keeps_the_old_provenance() -> None:
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            first_transport, _ = _fixed(FIXTURES / "work_v1.json")
            first = collect_openalex(
                keyword="catalog",
                store=store,
                registry_path=REGISTRY,
                transport=first_transport,
                cursor="*",
                collected_at=WHEN,
            )
            assert first.new_count == 1
            before = store.rows("openalex", "W2741809807")
            assert len(before) == 1
            snapshot = before[0]
            second_transport, _ = _fixed(FIXTURES / "work_v2.json")
            second = collect_openalex(
                keyword="catalog",
                store=store,
                registry_path=REGISTRY,
                transport=second_transport,
                cursor="*",
                collected_at=LATER,
            )
            assert second.new_count == 1
            after = store.rows("openalex", "W2741809807")
            assert len(after) == 2
            assert after[0] == snapshot
            assert after[0].collected_at == iso_z(WHEN)
            assert "First catalog of open abstracts" in after[0].provenance_json
            assert "Revised catalog of open abstracts" in after[1].provenance_json
            assert "I136199984" in after[1].provenance_json
            assert "T10009" in after[1].provenance_json


def test_requests_use_registry_user_agent_and_mailto(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "ghp_" + ("c" * 36)
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    transport, seen = _fixed(FIXTURES / "one_work.json")
    config = load_source_config("openalex", REGISTRY)
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                updated_since="2024-01-01",
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.SUCCESS
    request = seen[0]
    assert request.headers["User-Agent"] == config.user_agent
    assert request.url.params["mailto"] == config.mailto
    assert request.url.params["filter"] == "default.search:retrieval,from_updated_date:2024-01-01"
    assert "authorization" not in {key.lower() for key in request.headers}
    blob = str(request.url) + "".join(request.headers.values())
    assert secret not in blob
    _assert_gets(seen)


def test_collector_client_stays_on_the_openalex_host() -> None:
    config = load_source_config("openalex", REGISTRY)
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, text="ok")

    client = build_client(
        config,
        transport=httpx.MockTransport(handler),
        registry_path=REGISTRY,
        timeout=DEFAULT_TIMEOUT,
        accept="application/json",
    )
    try:
        with pytest.raises(PolicyBlocked):
            client.get("https://example.com/works")
        with pytest.raises(CollectorFailure) as caught:
            guarded_get(
                client,
                IntervalLimiter(0),
                config,
                "https://api.crossref.org/works",
                params={},
                max_retries=0,
            )
        assert caught.value.status is TaskStatus.POLICY_BLOCKED
    finally:
        client.close()
    assert called["n"] == 0
    assert client.timeout is not None
    assert client.timeout.connect == 10.0
    assert client.timeout.read == 30.0
    assert DEFAULT_TIMEOUT.connect == 10.0


def test_disabled_source_makes_no_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return load_recorded(FIXTURES / "one_work.json")

    with TemporaryDirectory() as directory:
        root = Path(directory)
        registry = root / "registry.yaml"
        registry.write_text(
            "\n".join(
                [
                    "sources:",
                    "  openalex:",
                    "    source_id: openalex",
                    '    base_url: "https://api.openalex.org"',
                    '    policy_snapshot_id: "sha256-disabled"',
                    "    enabled: false",
                    "    domain_whitelist:",
                    '      - "api.openalex.org"',
                    "    required_headers:",
                    '      user_agent: "TestAgent/1.0"',
                    '      mailto: "info@techpulse.example.com"',
                    "      extra_headers: {}",
                    "    rate_limits:",
                    "      requests_per_second: 10",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        with CursorStore(root / "cursors.sqlite") as store:
            result = collect_openalex(
                keyword="retrieval",
                store=store,
                registry_path=registry,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.POLICY_BLOCKED
            assert result.errors == ["policy_blocked: source disabled"]
            assert store.seen_count("openalex") == 0
    assert seen == []


def test_retry_after_parses_seconds_and_http_date() -> None:
    now = datetime(2015, 10, 21, 7, 28, tzinfo=UTC)
    assert parse_retry_after({"Retry-After": "5"}, now=now) == 5
    delay = parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:30 GMT"}, now=now)
    assert delay == 30
