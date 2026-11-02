"""Crossref collector tests. HTTP responses come from recorded fixtures only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from techpulse.collectors.base import CursorStore, ManualClock, load_source_config, query_key
from techpulse.collectors.crossref import collect_crossref, normalize_doi
from techpulse.contracts import RawRecord, TaskStatus

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "source_registry.yaml"
FIXTURES = ROOT / "tests" / "fixtures" / "crossref"
WHEN = datetime(2026, 10, 27, tzinfo=UTC)


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


def _key(
    *,
    rows: int = 20,
    query: str = "retrieval",
    doi: str = "",
    updated_from: str = "",
    updated_until: str = "",
) -> str:
    return query_key(
        {
            "doi": doi,
            "query": query,
            "rows": str(rows),
            "source": "crossref",
            "updated_from": updated_from,
            "updated_until": updated_until,
        }
    )


def _fixed(path: Path) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return load_recorded(path)

    return httpx.MockTransport(handler), seen


def _by_cursor(paths: dict[str, Path]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = str(request.url.params["cursor"])
        path = paths.get(cursor)
        if path is None:
            raise AssertionError(f"unexpected cursor {cursor}")
        return load_recorded(path)

    return httpx.MockTransport(handler), seen


def _assert_raw(record: RawRecord) -> None:
    assert list(VALIDATOR.iter_errors(record.model_dump(mode="json"))) == []


def _assert_gets(requests: list[httpx.Request]) -> None:
    assert requests
    for request in requests:
        assert request.method == "GET"
        assert request.url.host == "api.crossref.org"
        assert request.url.path == "/works"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.5555/AbC", "10.5555/abc"),
        ("HTTPS://DOI.ORG/10.5555/AbC", "10.5555/abc"),
        ("https://dx.doi.org/10.5555/AbC", "10.5555/abc"),
        ("http://dx.doi.org/10.5555/AbC", "10.5555/abc"),
        ("doi:10.5555/AbC", "10.5555/abc"),
    ],
)
def test_doi_case_and_url_forms_normalise(raw: str, expected: str) -> None:
    assert normalize_doi(raw) == expected


def test_same_doi_spellings_on_one_page_count_once() -> None:
    transport, seen = _fixed(FIXTURES / "variants.json")
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_crossref(
                query="identifiers",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 1
            assert len(result.records) == 1
            record = result.records[0]
            _assert_raw(record)
            assert record.source_record_id == "10.5555/abc.1"
            assert record.canonical_url == "https://doi.org/10.5555/abc.1"
            assert record.title == "Case Folding for Bibliographic Identifiers"
            assert record.abstract_or_excerpt == "Identifiers should compare independent of case."
            assert record.license_status.value == "metadata_only"
            provenance = json.loads(store.rows("crossref", "10.5555/abc.1")[0].provenance_json)
            assert provenance["doi"] == "10.5555/abc.1"
            assert provenance["type"] == "journal-article"
            assert provenance["title"] == record.title
            assert provenance["link"] == ["https://doi.org/10.5555/AbC.1"]
            assert provenance["license"] == ["https://creativecommons.org/licenses/by/4.0/"]
            assert provenance["published_at"] == "2024-03-02T00:00:00Z"
            assert store.seen_count("crossref") == 1
    _assert_gets(seen)


def test_pages_dedup_the_same_doi_and_validate() -> None:
    transport, seen = _by_cursor(
        {"*": FIXTURES / "page1.json", "cr-page-2": FIXTURES / "page2.json"}
    )
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with CursorStore(root / "cursors.sqlite") as store:
            result = collect_crossref(
                query="identifiers",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                rows=2,
                collected_at=WHEN,
                artifacts_dir=root / "artifacts",
                run_id="c11-run",
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 3
            assert result.metrics["third_party_writes"] == 0
            assert {record.source_record_id for record in result.records} == {
                "10.5555/abc.1",
                "10.5555/def.2",
                "10.5555/ghi.3",
            }
            for record in result.records:
                _assert_raw(record)
                assert record.source_id.value == "crossref"
            kept = store.rows("crossref", "10.5555/abc.1")
            assert len(kept) == 1
            assert "Case Folding for Bibliographic Identifiers" in kept[0].provenance_json
            assert "Duplicate page copy" not in kept[0].provenance_json
            raw_path = root / "artifacts" / "c11-run" / "C11" / "raw.jsonl"
            assert len(raw_path.read_text(encoding="utf-8").splitlines()) == 3
            again = collect_crossref(
                query="identifiers",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                rows=2,
                cursor="*",
                collected_at=WHEN,
            )
            assert again.new_count == 0
            assert store.seen_count("crossref") == 3
    _assert_gets(seen)


def test_incomplete_date_is_not_invented() -> None:
    transport, _seen = _fixed(FIXTURES / "incomplete.json")
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_crossref(
                query="year",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                collected_at=WHEN,
            )
            assert result.new_count == 1
            record = result.records[0]
            _assert_raw(record)
            assert record.published_at is None
            assert record.published_at_missing_reason == "source published date is incomplete"


def test_429_uses_retry_after() -> None:
    seen: list[httpx.Request] = []
    clock = ManualClock()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        calls["n"] += 1
        if calls["n"] == 1:
            return load_recorded(FIXTURES / "too_many.json")
        return load_recorded(FIXTURES / "one_work.json")

    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_crossref(
                query="identifiers",
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 1
    assert clock.slept == [5.0]
    _assert_gets(seen)


def test_network_errors_retry_at_most_twice() -> None:
    seen: list[httpx.Request] = []
    clock = ManualClock()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ConnectError("connection refused")

    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_crossref(
                query="retrieval",
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.RETRYABLE_ERROR
            assert result.errors == ["retryable_error: network"]
            assert store.get_cursor("crossref", _key()) is None
            assert store.seen_count("crossref") == 0
    assert len(seen) == 3


def test_network_error_recovers_within_two_retries() -> None:
    seen: list[httpx.Request] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("connection refused")
        return load_recorded(FIXTURES / "one_work.json")

    clock = ManualClock()
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_crossref(
                query="identifiers",
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 1
    assert len(seen) == 3


def test_forbidden_is_policy_blocked_and_does_not_advance() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = str(request.url.params["cursor"])
        if cursor == "*":
            return load_recorded(FIXTURES / "page1.json")
        return load_recorded(FIXTURES / "forbidden.json")

    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_crossref(
                query="identifiers",
                store=store,
                registry_path=REGISTRY,
                transport=httpx.MockTransport(handler),
                rows=2,
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.POLICY_BLOCKED
            assert result.new_count == 2
            assert store.get_cursor("crossref", _key(rows=2, query="identifiers")) == "cr-page-2"
            assert store.seen_count("crossref") == 2
            assert store.rows("crossref", "10.5555/ghi.3") == []
    assert len(seen) == 2
    _assert_gets(seen)


def test_registry_headers_mailto_and_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "ghp_" + ("d" * 36)
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    transport, seen = _fixed(FIXTURES / "one_work.json")
    config = load_source_config("crossref", REGISTRY)
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_crossref(
                query="retrieval",
                doi="HTTPS://dx.doi.org/10.5555/AbC.1",
                updated_from="2024-01-01",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                rows=5000,
                collected_at=WHEN,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.metrics["third_party_writes"] == 0
    request = seen[0]
    assert request.headers["User-Agent"] == config.user_agent
    assert request.headers["Accept"] == "application/json"
    assert request.url.params["mailto"] == config.mailto
    assert request.url.params["rows"] == "1000"
    assert request.url.params["filter"] == "doi:10.5555/abc.1,from-update-date:2024-01-01"
    blob = str(request.url) + "".join(request.headers.values())
    assert secret not in blob
    _assert_gets(seen)


def test_rate_limit_headers_slow_the_next_page() -> None:
    transport, seen = _by_cursor(
        {"*": FIXTURES / "low_rate.json", "after": FIXTURES / "after_rate.json"}
    )
    clock = ManualClock()
    with TemporaryDirectory() as directory:
        with CursorStore(Path(directory) / "cursors.sqlite") as store:
            result = collect_crossref(
                query="pool",
                store=store,
                registry_path=REGISTRY,
                transport=transport,
                collected_at=WHEN,
                clock=clock.clock,
                sleep=clock.sleep,
            )
            assert result.status is TaskStatus.SUCCESS
            assert result.new_count == 2
    assert clock.slept == [2.0]
    _assert_gets(seen)
