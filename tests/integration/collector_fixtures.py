"""Build RawRecord NDJSON from the collector fixtures. No live HTTP."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from techpulse.collectors.base import CursorStore, ManualClock
from techpulse.collectors.crossref import collect_crossref
from techpulse.collectors.github import GitHubCollector
from techpulse.collectors.hn import HNCollector
from techpulse.collectors.openalex import collect_openalex
from techpulse.contracts import RawRecord, TaskStatus

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "source_registry.yaml"
POLICY = ROOT / "config" / "policy.yaml"
FIXTURES = ROOT / "tests" / "fixtures"
WHEN = datetime(2026, 10, 27, tzinfo=UTC)


def fixture_paths() -> tuple[Path, ...]:
    return (
        FIXTURES / "openalex" / "one_work.json",
        FIXTURES / "crossref" / "one_work.json",
        FIXTURES / "github" / "repository.json",
        FIXTURES / "github" / "releases.json",
        FIXTURES / "hn" / "item.json",
    )


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


def load_fixture_records(work: Path) -> list[RawRecord]:
    """Run the collectors against the checked-in fixtures and return their rows."""
    for path in fixture_paths():
        if path.stat().st_size >= 100_000:
            raise AssertionError(f"{path.name} is larger than 100 KB")
    clock = ManualClock()
    records: list[RawRecord] = []
    openalex_transport = httpx.MockTransport(
        lambda _request: load_recorded(FIXTURES / "openalex" / "one_work.json")
    )
    with CursorStore(work / "openalex.sqlite") as store:
        openalex = collect_openalex(
            keyword="retrieval",
            store=store,
            registry_path=REGISTRY,
            transport=openalex_transport,
            cursor="*",
            collected_at=WHEN,
            clock=clock.clock,
            sleep=clock.sleep,
        )
    if openalex.status is not TaskStatus.SUCCESS or not openalex.records:
        raise AssertionError(openalex.errors)
    records.extend(openalex.records)
    crossref_transport = httpx.MockTransport(
        lambda _request: load_recorded(FIXTURES / "crossref" / "one_work.json")
    )
    with CursorStore(work / "crossref.sqlite") as store:
        crossref = collect_crossref(
            query="identifiers",
            store=store,
            registry_path=REGISTRY,
            transport=crossref_transport,
            collected_at=WHEN,
            clock=clock.clock,
            sleep=clock.sleep,
        )
    if crossref.status is not TaskStatus.SUCCESS or not crossref.records:
        raise AssertionError(crossref.errors)
    records.extend(crossref.records)
    with GitHubCollector(
        registry_path=REGISTRY,
        policy_path=POLICY,
        transport=_GitHubTransport(),
        sleeper=lambda _seconds: None,
    ) as collector:
        github = collector.collect(["new-owner/signal-kit"])
    if github.status is not TaskStatus.SUCCESS or not github.records:
        raise AssertionError(github.status)
    records.extend(github.records)
    with HNCollector(
        registry_path=REGISTRY,
        policy_path=POLICY,
        transport=_HNTransport(),
        sleeper=lambda _seconds: None,
    ) as collector:
        hn = collector.collect([4567001])
    if hn.status is not TaskStatus.SUCCESS or not hn.records:
        raise AssertionError(hn.status)
    records.extend(hn.records)
    return records


def ndjson_bytes(records: list[RawRecord]) -> bytes:
    return "".join(f"{record.model_dump_json()}\n" for record in records).encode("utf-8")


class _GitHubTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases"):
            payload: Any = json.loads(
                (FIXTURES / "github" / "releases.json").read_text(encoding="utf-8")
            )
        else:
            payload = json.loads(
                (FIXTURES / "github" / "repository.json").read_text(encoding="utf-8")
            )
        return httpx.Response(200, json=payload, request=request)


class _HNTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        payload: Any = json.loads((FIXTURES / "hn" / "item.json").read_text(encoding="utf-8"))
        return httpx.Response(200, json=payload, request=request)
