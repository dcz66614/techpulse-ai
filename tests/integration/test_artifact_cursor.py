"""Artifact durability for the shared collect_pages cursor."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

from techpulse.collectors.base import CollectResult, CursorStore, ManualClock, query_key
from techpulse.collectors.openalex import collect_openalex
from techpulse.contracts import TaskStatus

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "source_registry.yaml"
FIXTURES = ROOT / "tests" / "fixtures" / "openalex"
WHEN = datetime(2026, 10, 27, tzinfo=UTC)
KEY = query_key(
    {
        "source": "openalex",
        "keyword": "retrieval",
        "per_page": "2",
        "updated_since": "",
    }
)


def _response(path: Path) -> httpx.Response:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(path.name)
    status = loaded.get("status")
    body = loaded.get("body")
    headers_raw = loaded.get("headers", {})
    if isinstance(status, bool) or not isinstance(status, int):
        raise AssertionError(path.name)
    if not isinstance(headers_raw, dict):
        raise AssertionError(path.name)
    headers: dict[str, str] = {}
    for key, value in headers_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise AssertionError(path.name)
        headers[key] = value
    if isinstance(body, str):
        content = body
    else:
        content = json.dumps(body)
        headers.setdefault("Content-Type", "application/json")
    return httpx.Response(status, headers=headers, text=content)


def _transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    paths = {"*": FIXTURES / "page1.json", "page-2": FIXTURES / "page2.json"}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = str(request.url.params["cursor"])
        path = paths.get(cursor)
        if path is None:
            raise AssertionError(f"unexpected cursor {cursor}")
        return _response(path)

    return httpx.MockTransport(handler), seen


def _collect(
    store: CursorStore,
    transport: httpx.BaseTransport,
    artifacts: Path | None,
    run_id: str | None,
) -> CollectResult:
    clock = ManualClock()
    return collect_openalex(
        keyword="retrieval",
        store=store,
        registry_path=REGISTRY,
        transport=transport,
        per_page=2,
        collected_at=WHEN,
        clock=clock.clock,
        sleep=clock.sleep,
        artifacts_dir=artifacts,
        run_id=run_id,
    )


def test_artifact_fsync_failure_keeps_the_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAlex, Crossref, and arXiv share this collect_pages success path."""
    real_fsync = os.fsync
    gate = {"armed": True}

    def flaky_fsync(fd: int) -> None:
        if gate["armed"]:
            raise OSError("fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky_fsync)
    transport, seen = _transport()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        artifacts = root / "artifacts"
        with CursorStore(root / "failed.sqlite") as store:
            with pytest.raises(OSError, match="fsync failed"):
                _collect(store, transport, artifacts, "cafix-run")
            assert len(seen) == 2
            assert store.get_cursor("openalex", KEY) is None
            assert store.seen_count("openalex") == 0
            gate["armed"] = False
            replay = _collect(store, transport, artifacts, "cafix-run")
            replay_cursor = store.get_cursor("openalex", KEY)
            replay_seen = store.seen_count("openalex")
        with CursorStore(root / "fresh.sqlite") as fresh_store:
            fresh = _collect(fresh_store, transport, None, None)
            fresh_cursor = fresh_store.get_cursor("openalex", KEY)
            fresh_seen = fresh_store.seen_count("openalex")
        assert replay.status is TaskStatus.SUCCESS
        assert fresh.status is TaskStatus.SUCCESS
        assert replay.new_count > 0
        assert replay.records == fresh.records
        assert replay.new_count == fresh.new_count
        assert replay_cursor == fresh_cursor
        assert replay_seen == fresh_seen
        raw_path = artifacts / "cafix-run" / "C10" / "raw.jsonl"
        assert raw_path.is_file()
        assert (raw_path.parent / "manifest.json").is_file()
        assert (raw_path.parent / "metrics.json").is_file()
        assert len(raw_path.read_text(encoding="utf-8").splitlines()) == replay.new_count
