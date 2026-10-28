"""arXiv collector tests. HTTP responses come from recorded fixtures only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from techpulse.collectors.arxiv import BULK_MESSAGE, collect_arxiv, split_arxiv_id
from techpulse.collectors.base import (
    CursorStore,
    IntervalLimiter,
    ManualClock,
    iso_z,
    load_source_config,
    query_key,
)
from techpulse.contracts import RawRecord, TaskStatus

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "source_registry.yaml"
FIXTURES = ROOT / "tests" / "fixtures" / "arxiv"
WHEN = datetime(2026, 10, 27, tzinfo=UTC)
LATER = datetime(2026, 10, 28, tzinfo=UTC)
WINDOW_START = datetime(2024, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2024, 3, 1, tzinfo=UTC)
BOUNDS_END = datetime(2024, 2, 1, tzinfo=UTC)
QUERY = "cat:cs.IR"


def _schema() -> dict[str, Any]:
    raw = json.loads((ROOT / "contracts" / "raw_record.schema.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AssertionError("raw record schema is not an object")
    return raw


VALIDATOR = Draft202012Validator(_schema())


def load_recorded(path: Path) -> httpx.Response:
    if path.suffix == ".xml":
        return httpx.Response(
            200,
            headers={"Content-Type": "application/atom+xml"},
            text=path.read_text(encoding="utf-8"),
        )
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


def _key(
    *,
    page_size: int,
    search_query: str = QUERY,
    window_start: datetime = WINDOW_START,
    window_end: datetime = WINDOW_END,
) -> str:
    return query_key(
        {
            "page_size": str(page_size),
            "search_query": search_query,
            "source": "arxiv",
            "window_end": iso_z(window_end),
            "window_start": iso_z(window_start),
        }
    )


def _by_start(paths: dict[str, Path]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        start = str(request.url.params["start"])
        path = paths.get(start)
        if path is None:
            raise AssertionError(f"unexpected start {start}")
        return load_recorded(path)

    return httpx.MockTransport(handler), seen


def _assert_raw(record: RawRecord) -> None:
    assert list(VALIDATOR.iter_errors(record.model_dump(mode="json"))) == []


def _assert_metadata_only(requests: list[httpx.Request]) -> None:
    assert requests
    for request in requests:
        assert request.method == "GET"
        assert request.url.host == "export.arxiv.org"
        assert request.url.path == "/api/query"
        url = str(request.url)
        assert "/pdf" not in url
        assert "/src" not in url
        assert "/html" not in url
        assert "/e-print" not in url


@pytest.mark.parametrize(
    ("raw", "base", "version"),
    [
        ("http://arxiv.org/abs/2401.00001v2", "2401.00001", 2),
        ("https://arxiv.org/pdf/2401.00001v1", "2401.00001", 1),
        ("2401.00001", "2401.00001", 1),
        ("hep-th/9901001v3", "hep-th/9901001", 3),
    ],
)
def test_split_arxiv_id(raw: str, base: str, version: int) -> None:
    assert split_arxiv_id(raw) == (base, version)


def test_interval_limiter_stays_at_least_three_seconds() -> None:
    clock = ManualClock()
    limiter = IntervalLimiter(3.0, clock=clock.clock, sleep=clock.sleep)
    limiter.observe_headers({"X-Rate-Limit-Limit": "50", "X-Rate-Limit-Interval": "1s"})
    assert limiter.min_interval_seconds == 3.0
    limiter.wait()
    limiter.wait()
    assert clock.slept == [3.0]


def test_pages_overlap_once_and_stay_on_the_export_api(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "ghp_" + ("d" * 36)
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    transport, seen = _by_start({"0": FIXTURES / "page1.xml", "2": FIXTURES / "page2.xml"})
    clock = ManualClock()
    config = load_source_config("arxiv", REGISTRY)
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_arxiv(
                search_query=QUERY,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                page_size=2,
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 3
            assert result.metrics["third_party_writes"] == 0
            assert {record.source_record_id for record in result.records} == {
                "2401.00001v1",
                "2401.00002v1",
                "2401.00003v1",
            }
            for record in result.records:
                _assert_raw(record)
                assert record.source_id.value == "arxiv"
                assert record.license_status.value == "metadata_only"
                assert record.language == "und"
                assert record.policy_snapshot_id == config.policy_snapshot_id
            first = store.rows("arxiv", "2401.00001v1")
            assert len(first) == 1
            provenance = json.loads(first[0].provenance_json)
            assert provenance["arxiv_id"] == "2401.00001"
            assert provenance["version"] == 1
            assert provenance["categories"] == ["cs.IR", "cs.LG"]
            assert provenance["primary_category"] == "cs.IR"
            assert provenance["authors"] == ["Ada Example"]
            assert "ada@example.com" not in first[0].provenance_json
            assert len(store.rows("arxiv", "2401.00002v1")) == 1
            assert store.seen_count("arxiv") == 3
            again = collect_arxiv(
                search_query=QUERY,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                page_size=2,
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert again.new_count == 0
            assert again.status is TaskStatus.SUCCESS
            assert store.seen_count("arxiv") == 3
            assert store.get_cursor("arxiv", _key(page_size=2)) == ""
    assert clock.slept == [config.min_interval_seconds]
    assert clock.slept[0] >= 3
    assert [str(request.url.params["start"]) for request in seen] == ["0", "2"]
    assert seen[0].url.params["max_results"] == "2"
    assert seen[0].url.params["sortBy"] == "submittedDate"
    assert seen[0].url.params["sortOrder"] == "ascending"
    expected_query = "cat:cs.IR AND submittedDate:[202401010000 TO 202403010000]"
    assert seen[0].url.params["search_query"] == expected_query
    assert seen[0].headers["User-Agent"] == config.user_agent
    assert seen[0].headers["Accept"] == "application/atom+xml"
    blob = str(seen[0].url) + "".join(seen[0].headers.values())
    assert secret not in blob
    _assert_metadata_only(seen)


def test_time_window_is_half_open() -> None:
    transport, seen = _by_start({"0": FIXTURES / "bounds.xml"})
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with CursorStore(root / "cursors.sqlite") as store:
            result = collect_arxiv(
                search_query=QUERY,
                window_start=WINDOW_START,
                window_end=BOUNDS_END,
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                page_size=10,
                collected_at=WHEN,
                artifacts_dir=root / "artifacts",
                run_id="c12-run",
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 2
            assert {record.source_record_id for record in result.records} == {
                "2401.00002v1",
                "2401.00003v1",
            }
            for record in result.records:
                _assert_raw(record)
            assert store.rows("arxiv", "2401.00001v1") == []
            assert store.rows("arxiv", "2401.00004v1") == []
            raw_path = root / "artifacts" / "c12-run" / "C12" / "raw.jsonl"
            assert len(raw_path.read_text(encoding="utf-8").splitlines()) == 2
            assert (root / "artifacts" / "c12-run" / "C12" / "manifest.json").is_file()
            assert (root / "artifacts" / "c12-run" / "C12" / "metrics.json").is_file()
    _assert_metadata_only(seen)
    assert seen[0].url.params["search_query"] == (
        "cat:cs.IR AND submittedDate:[202401010000 TO 202402010000]"
    )


def test_version_merge_keeps_old_provenance() -> None:
    seen: list[httpx.Request] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        calls["n"] += 1
        if calls["n"] == 1:
            return load_recorded(FIXTURES / "version_v1.xml")
        return load_recorded(FIXTURES / "version_v2.xml")

    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            first = collect_arxiv(
                search_query=QUERY,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
            )
            assert first.status is TaskStatus.SUCCESS
            assert first.new_count == 1
            assert first.records[0].source_record_id == "2401.00010v1"
            assert first.records[0].canonical_url == "https://arxiv.org/abs/2401.00010v1"
            _assert_raw(first.records[0])
            saved = store.rows("arxiv", "2401.00010v1")[0]
            assert "ada@example.com" not in saved.provenance_json
            assert "Sparse retrieval, version one" in saved.provenance_json
            second = collect_arxiv(
                search_query=QUERY,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                cursor="0",
                collected_at=LATER,
            )
            assert second.status is TaskStatus.SUCCESS
            assert second.new_count == 1
            assert second.records[0].source_record_id == "2401.00010v2"
            assert second.records[0].title == "Sparse retrieval, version two"
            _assert_raw(second.records[0])
            assert store.rows("arxiv", "2401.00010v1") == [saved]
            revised = json.loads(store.rows("arxiv", "2401.00010v2")[0].provenance_json)
            assert revised["version"] == 2
            assert revised["authors"] == ["Ada Example", "Lin Query"]
            assert revised["categories"] == ["cs.IR", "cs.LG"]
            assert "ada@example.com" not in store.rows("arxiv", "2401.00010v2")[0].provenance_json
            assert store.seen_count("arxiv") == 2
    _assert_metadata_only(seen)


def test_429_honours_retry_after() -> None:
    seen: list[httpx.Request] = []
    clock = ManualClock()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        calls["n"] += 1
        if calls["n"] == 1:
            return load_recorded(FIXTURES / "too_many.json")
        return load_recorded(FIXTURES / "version_v1.xml")

    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_arxiv(
                search_query=QUERY,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 1
            assert store.get_cursor("arxiv", _key(page_size=100)) == ""
    assert clock.slept == [5.0]
    _assert_metadata_only(seen)


def test_timeout_is_retryable_and_does_not_advance() -> None:
    seen: list[httpx.Request] = []
    clock = ManualClock()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.TimeoutException("timed out")

    config = load_source_config("arxiv", REGISTRY)
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_arxiv(
                search_query=QUERY,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.RETRYABLE_ERROR
            assert result.errors == ["retryable_error: timeout"]
            assert result.new_count == 0
            assert store.get_cursor("arxiv", _key(page_size=100)) is None
            assert store.seen_count("arxiv") == 0
    assert len(seen) == 3
    assert clock.slept == [config.min_interval_seconds, config.min_interval_seconds]
    assert all(delay >= 3 for delay in clock.slept)
    _assert_metadata_only(seen)


def test_over_official_total_stops_without_storing() -> None:
    transport, seen = _by_start({"0": FIXTURES / "bulk.xml"})
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_arxiv(
                search_query=QUERY,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.POLICY_BLOCKED
            assert result.errors == [BULK_MESSAGE]
            assert result.new_count == 0
            assert result.records == []
            assert store.get_cursor("arxiv", _key(page_size=100)) is None
            assert store.seen_count("arxiv") == 0
    assert len(seen) == 1
    _assert_metadata_only(seen)


def test_page_size_is_capped_at_the_registry_slice() -> None:
    transport, seen = _by_start({"0": FIXTURES / "version_v1.xml"})
    clock = ManualClock()
    limiter = IntervalLimiter(0.1, clock=clock.clock, sleep=clock.sleep)
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_arxiv(
                search_query=QUERY,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                page_size=5000,
                collected_at=WHEN,
                limiter=limiter,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 1
            assert store.get_cursor("arxiv", _key(page_size=2000)) == ""
    assert seen[0].url.params["max_results"] == "2000"
    assert limiter.min_interval_seconds == 3.0
    _assert_metadata_only(seen)
