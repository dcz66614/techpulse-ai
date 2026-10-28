"""D10 archive tests. Inputs are the collector fixtures. No live HTTP."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from techpulse.contracts import TaskStatus
from techpulse.workflows.activities import (
    LocalObjectStore,
    ObjectStore,
    archive_collector_ndjson,
    idempotency_key,
    read_watermark,
    split_object_uri,
    task_idempotency_key,
)
from tests.integration.collector_fixtures import load_fixture_records, ndjson_bytes

_SECRET = "fixture-header-secret"
_INDEXED = {
    "raw_uri",
    "source_id",
    "collected_at",
    "published_at",
    "policy_snapshot_id",
}


class _BoomStore:
    def put(self, key: str, body: bytes) -> str:
        raise OSError("staging write failed")

    def get(self, uri: str) -> bytes:
        raise OSError("staging write failed")


def test_fixture_archive_is_traceable_and_replay_inserts_nothing() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = load_fixture_records(root / "collect")
        payload = ndjson_bytes(records)
        digest = hashlib.sha256(payload).hexdigest()
        artifacts = root / "artifacts"
        database = root / "archive.sqlite"
        store = LocalObjectStore(artifacts)
        cursors = {record.source_id.value: "fixture-end" for record in records}
        headers = {
            "Authorization": f"Bearer {_SECRET}",
            "Content-Type": "application/x-ndjson",
            "X-Request-Note": f"token={_SECRET}",
        }
        first = archive_collector_ndjson(
            payload,
            run_id="d10-run",
            database=database,
            store=store,
            artifacts_dir=artifacts,
            expected_sha256=digest,
            response_headers=headers,
            proposed_cursors=cursors,
        )
        assert first.status is TaskStatus.SUCCESS
        assert first.errors == ()
        assert first.inserted == len(records)
        assert first.duplicates == 0
        assert first.dlq == 0
        assert first.watermark_advanced is True
        assert first.metrics["third_party_writes"] == 0
        assert first.object_sha256 == digest
        for source_id in cursors:
            assert read_watermark(database, source_id) == "fixture-end"
        _assert_indexes(database)
        _assert_trace(database, store)
        _assert_enabled_stubs(database)
        directory = artifacts / "d10-run" / "D10"
        text = _tree_text(directory)
        assert _SECRET not in text
        manifest = _read(directory / "raw_manifest.json")
        response_headers = _mapping(manifest["response_headers"])
        assert response_headers["Content-Type"] == "application/x-ndjson"
        assert "Authorization" not in response_headers
        assert response_headers["X-Request-Note"] == "[REDACTED]"
        assert manifest["task_idempotency_key"] == task_idempotency_key("D10", "d10-run", [digest])
        assert (directory / "manifest.json").is_file()
        assert (directory / "metrics.json").is_file()
        assert (directory / "log.jsonl").is_file()
        staged = store.get(first.object_uri)
        assert hashlib.sha256(staged).hexdigest() == digest
        again = archive_collector_ndjson(
            payload,
            run_id="d10-run",
            database=database,
            store=store,
            artifacts_dir=artifacts,
            expected_sha256=digest,
            proposed_cursors=cursors,
        )
        assert again.inserted == 0
        assert again.duplicates == len(records)
        assert again.dlq == 0
        assert _count(database, "raw_records") == len(records)
        assert _count(database, "raw_idempotency") == len(records)


def test_schema_errors_go_to_the_dlq_and_do_not_advance_the_watermark() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = load_fixture_records(root / "collect")
        good = records[0]
        source_id = good.source_id.value
        artifacts = root / "artifacts"
        database = root / "archive.sqlite"
        store = LocalObjectStore(artifacts)
        first = archive_collector_ndjson(
            ndjson_bytes([good]),
            run_id="d10-good",
            database=database,
            store=store,
            artifacts_dir=artifacts,
            proposed_cursors={source_id: "page-1"},
        )
        assert first.status is TaskStatus.SUCCESS
        assert read_watermark(database, source_id) == "page-1"
        mixed = (
            good.model_dump_json() + "\n" + '{"schema_version":"1.0"}\n' + "{not-json\n"
        ).encode()
        second = archive_collector_ndjson(
            mixed,
            run_id="d10-bad",
            database=database,
            store=store,
            artifacts_dir=artifacts,
            proposed_cursors={source_id: "page-2"},
        )
        assert second.status is TaskStatus.SUCCESS
        assert second.errors == ("data errors isolated to DLQ",)
        assert second.inserted == 0
        assert second.duplicates == 1
        assert second.dlq == 2
        assert second.watermark_advanced is False
        assert read_watermark(database, source_id) == "page-1"
        assert _count(database, "raw_records") == 1
        assert _count(database, "raw_dlq") == 2
        third = archive_collector_ndjson(
            mixed,
            run_id="d10-bad",
            database=database,
            store=store,
            artifacts_dir=artifacts,
            proposed_cursors={source_id: "page-3"},
        )
        assert third.dlq == 2
        assert third.inserted == 0
        assert _count(database, "raw_dlq") == 2
        assert read_watermark(database, source_id) == "page-1"
        reasons = {
            str(item["reason"])
            for item in _mappings(_read(artifacts / "d10-bad" / "D10" / "raw_manifest.json")["dlq"])
        }
        assert any(reason.startswith("schema:") for reason in reasons)
        assert "invalid_json" in reasons


def test_object_store_failure_is_retryable_and_leaves_the_watermark() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = load_fixture_records(root / "collect")
        database = root / "archive.sqlite"
        store: ObjectStore = _BoomStore()
        result = archive_collector_ndjson(
            ndjson_bytes(records),
            run_id="d10-store",
            database=database,
            store=store,
            artifacts_dir=root / "artifacts",
            proposed_cursors={"openalex": "page-9"},
        )
        assert result.status is TaskStatus.RETRYABLE_ERROR
        assert result.errors == ("retryable_error: object store",)
        assert result.watermark_advanced is False
        assert result.inserted == 0
        assert not database.exists()
        assert read_watermark(database, "openalex") is None
        manifest = _read(root / "artifacts" / "d10-store" / "D10" / "manifest.json")
        assert manifest["status"] == "retryable_error"


def test_declared_sha256_mismatch_is_fatal_and_is_not_staged() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = load_fixture_records(root / "collect")
        artifacts = root / "artifacts"
        database = root / "archive.sqlite"
        result = archive_collector_ndjson(
            ndjson_bytes(records),
            run_id="d10-hash",
            database=database,
            store=LocalObjectStore(artifacts),
            artifacts_dir=artifacts,
            expected_sha256="0" * 64,
            proposed_cursors={"openalex": "page-9"},
        )
        assert result.status is TaskStatus.FATAL_ERROR
        assert result.errors == ("fatal_error: sha256 mismatch",)
        assert result.object_uri == ""
        assert not database.exists()
        assert read_watermark(database, "openalex") is None
        assert not (artifacts / "d10-hash" / "D10" / "staging" / "raw.ndjson").exists()
        manifest = _read(artifacts / "d10-hash" / "D10" / "manifest.json")
        assert manifest["status"] == "fatal_error"


def test_invalid_utf8_is_isolated_and_does_not_advance_the_watermark() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "archive.sqlite"
        result = archive_collector_ndjson(
            b"\xff\xfe",
            run_id="d10-utf8",
            database=database,
            store=LocalObjectStore(artifacts),
            artifacts_dir=artifacts,
            proposed_cursors={"openalex": "later"},
        )
        assert result.status is TaskStatus.SUCCESS
        assert result.dlq == 1
        assert result.inserted == 0
        assert result.watermark_advanced is False
        assert read_watermark(database, "openalex") is None
        assert _count(database, "raw_records") == 0
        assert _count(database, "raw_dlq") == 1


def _assert_trace(database: Path, store: LocalObjectStore) -> None:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT source_id, source_record_id, canonical_url, collected_at, published_at,
                   policy_snapshot_id, content_sha256, raw_uri
            FROM raw_records
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()
    assert rows
    for row in rows:
        raw_uri = row["raw_uri"]
        assert isinstance(raw_uri, str)
        body = store.get(raw_uri)
        _key, line_no = split_object_uri(raw_uri)
        assert line_no is not None
        payload = json.loads(body.decode("utf-8").splitlines()[line_no - 1])
        assert isinstance(payload, dict)
        assert payload["source_id"] == row["source_id"]
        assert payload["source_record_id"] == row["source_record_id"]
        assert payload["canonical_url"] == row["canonical_url"]
        assert payload["collected_at"] == row["collected_at"]
        assert payload["published_at"] == row["published_at"]
        assert payload["policy_snapshot_id"] == row["policy_snapshot_id"]
        assert payload["content_sha256"] == row["content_sha256"]
        source_id = row["source_id"]
        source_record_id = row["source_record_id"]
        content_sha256 = row["content_sha256"]
        assert isinstance(source_id, str)
        assert isinstance(source_record_id, str)
        assert isinstance(content_sha256, str)
        key = idempotency_key("d10-run", source_id, source_record_id, content_sha256)
        assert key in _tree_text(Path(store.root) / "d10-run" / "D10")


def _assert_indexes(database: Path) -> None:
    conn = sqlite3.connect(database)
    try:
        listed = conn.execute("PRAGMA index_list('raw_records')").fetchall()
        found: set[str] = set()
        for index in listed:
            name = index[1]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            if name.startswith("sqlite_"):
                continue
            info = conn.execute(f"PRAGMA index_info('{name}')").fetchall()
            for column in info:
                column_name = column[2]
                if isinstance(column_name, str):
                    found.add(column_name)
    finally:
        conn.close()
    assert _INDEXED <= found


def _assert_enabled_stubs(database: Path) -> None:
    conn = sqlite3.connect(database)
    try:
        rows = conn.execute("SELECT enabled FROM source_registry").fetchall()
    finally:
        conn.close()
    assert rows
    assert all(row[0] == 0 for row in rows)


def _count(database: Path, table: str) -> int:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise AssertionError(table)
    conn = sqlite3.connect(database)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object")
    parsed: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise AssertionError("expected string keys")
        parsed[key] = item
    return parsed


def _mappings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AssertionError("expected a list")
    return [_mapping(item) for item in value]


def _read(path: Path) -> dict[str, object]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(path.name)
    parsed: dict[str, object] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise AssertionError(path.name)
        parsed[key] = value
    return parsed


def _tree_text(directory: Path) -> str:
    parts: list[str] = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)
