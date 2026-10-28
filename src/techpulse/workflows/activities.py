"""D10 raw archive and lineage.

Collector NDJSON is staged through an object-store interface. This process
has no S3 endpoint, so the shipped backend writes under the artifacts root.
SQLite holds the raw-record index. An S3 backend can implement the same
``ObjectStore`` methods later.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from techpulse.contracts import TaskStatus
from techpulse.governance.policy import redact_secrets

_SECRET_HEADER = re.compile(
    r"authorization|cookie|token|api[-_]?key|secret|password",
    re.IGNORECASE,
)
_SOURCE_ROWS: dict[str, tuple[str, str]] = {
    "openalex": ("academic_papers", "https://api.openalex.org"),
    "crossref": ("academic_papers", "https://api.crossref.org"),
    "arxiv": ("academic_papers", "https://export.arxiv.org/api/query"),
    "github": ("code_repositories", "https://api.github.com"),
    "hn": ("community_discussions", "https://hacker-news.firebaseio.com/v0/"),
    "rss": ("syndicated_feeds", "not stated"),
    "sec": ("regulatory_filings", "https://data.sec.gov"),
    "cninfo": ("regulatory_filings", "http://webapi.cninfo.com.cn"),
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_registry (
    source_id TEXT PRIMARY KEY,
    source_family TEXT NOT NULL,
    base_url TEXT NOT NULL,
    policy_snapshot_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    requests_per_minute INTEGER NOT NULL DEFAULT 10,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_records (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_registry (source_id),
    source_record_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    abstract_or_excerpt TEXT,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    license_status TEXT NOT NULL,
    policy_snapshot_id TEXT NOT NULL,
    raw_uri TEXT NOT NULL,
    UNIQUE (source_id, source_record_id, content_sha256)
);
CREATE INDEX IF NOT EXISTS ix_raw_records_raw_uri ON raw_records (raw_uri);
CREATE INDEX IF NOT EXISTS ix_raw_records_source_id ON raw_records (source_id);
CREATE INDEX IF NOT EXISTS ix_raw_records_collected_at ON raw_records (collected_at);
CREATE INDEX IF NOT EXISTS ix_raw_records_published_at ON raw_records (published_at);
CREATE INDEX IF NOT EXISTS ix_raw_records_policy_snapshot_id
    ON raw_records (policy_snapshot_id);
CREATE TABLE IF NOT EXISTS raw_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    raw_record_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_dlq (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (run_id, line_no)
);
CREATE TABLE IF NOT EXISTS archive_watermarks (
    source_id TEXT PRIMARY KEY,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class ObjectStore(Protocol):
    """Bytes addressed by a key. ``put`` returns the URI ``get`` accepts."""

    def put(self, key: str, body: bytes) -> str: ...

    def get(self, uri: str) -> bytes: ...


class LocalObjectStore:
    """Write object bytes under ``root`` (the artifacts directory)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, key: str, body: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        return "local:" + key

    def get(self, uri: str) -> bytes:
        key, _line = split_object_uri(uri)
        return self._path(key).read_bytes()

    def _path(self, key: str) -> Path:
        relative = _relative_key(key)
        root = self.root.resolve()
        path = (root / relative).resolve()
        if path != root and root not in path.parents:
            raise ValueError("invalid object key")
        return path


@dataclass(frozen=True)
class ArchiveResult:
    status: TaskStatus
    run_id: str
    inserted: int
    duplicates: int
    dlq: int
    watermark_advanced: bool
    object_uri: str
    object_sha256: str
    manifest_dir: str
    errors: tuple[str, ...]
    metrics: dict[str, int | str | bool]


@dataclass(frozen=True)
class _Accepted:
    source_id: str
    source_record_id: str
    canonical_url: str
    title: str
    abstract_or_excerpt: str
    published_at: str | None
    collected_at: str
    content_sha256: str
    license_status: str
    policy_snapshot_id: str


def default_raw_schema_path() -> Path:
    return Path(__file__).resolve().parents[3] / "contracts" / "raw_record.schema.json"


def idempotency_key(
    run_id: str,
    source_id: str,
    source_record_id: str,
    content_sha256: str,
) -> str:
    """Identity of one raw row inside one run. A replay inserts nothing new."""
    text = "|".join((run_id, source_id, source_record_id, content_sha256))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def task_idempotency_key(task_id: str, run_id: str, input_hashes: Sequence[str]) -> str:
    """Section 9.2 key: sha256 of task_id, run_id, and the sorted input hashes."""
    parts = [task_id, run_id, *sorted(input_hashes)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def split_object_uri(uri: str) -> tuple[str, int | None]:
    prefix = "local:"
    if not uri.startswith(prefix):
        raise ValueError("unsupported object uri")
    body = uri[len(prefix) :]
    line: int | None = None
    marker = "#line="
    if marker in body:
        body, raw_line = body.rsplit(marker, 1)
        if not raw_line.isdigit():
            raise ValueError("unsupported object uri")
        line = int(raw_line)
    _relative_key(body)
    return body, line


def read_watermark(database: Path, source_id: str) -> str | None:
    if not database.is_file():
        return None
    conn = sqlite3.connect(database)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'archive_watermarks'"
        ).fetchone()
        if found is None:
            return None
        row = conn.execute(
            "SELECT cursor FROM archive_watermarks WHERE source_id = ?",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    cursor = row[0]
    if not isinstance(cursor, str):
        return None
    return cursor


def archive_collector_ndjson(
    source: Path | bytes,
    *,
    run_id: str,
    database: Path,
    store: ObjectStore,
    artifacts_dir: Path,
    schema_path: Path | None = None,
    expected_sha256: str | None = None,
    response_headers: Mapping[str, str] | None = None,
    proposed_cursors: Mapping[str, str] | None = None,
    archived_at: datetime | None = None,
) -> ArchiveResult:
    """Stage one collector NDJSON file and index the records that validate.

    A declared SHA-256 mismatch is ``fatal_error`` and is not staged. An
    object-store failure is ``retryable_error``. Schema errors are written
    to the DLQ. The archive watermark moves only after a clean commit.
    """
    checked_run = _run_id(run_id)
    payload = source.read_bytes() if isinstance(source, Path) else source
    digest = hashlib.sha256(payload).hexdigest()
    task_key = task_idempotency_key("D10", checked_run, [digest])
    headers = _public_headers(response_headers)
    cursors = {} if proposed_cursors is None else dict(proposed_cursors)
    when = _archived_at(archived_at)
    directory = artifacts_dir / checked_run / "D10"
    schema = default_raw_schema_path() if schema_path is None else schema_path
    if expected_sha256 is not None and expected_sha256.lower() != digest:
        return _emit(
            directory=directory,
            run_id=checked_run,
            status=TaskStatus.FATAL_ERROR,
            inserted=0,
            duplicates=0,
            records=[],
            dlq=[],
            rows_read=0,
            object_uri="",
            object_sha256=digest,
            task_key=task_key,
            headers=headers,
            watermark_advanced=False,
            errors=("fatal_error: sha256 mismatch",),
        )
    key = f"{checked_run}/D10/staging/raw.ndjson"
    try:
        object_uri = store.put(key, payload)
    except OSError:
        return _emit(
            directory=directory,
            run_id=checked_run,
            status=TaskStatus.RETRYABLE_ERROR,
            inserted=0,
            duplicates=0,
            records=[],
            dlq=[],
            rows_read=0,
            object_uri="",
            object_sha256=digest,
            task_key=task_key,
            headers=headers,
            watermark_advanced=False,
            errors=("retryable_error: object store",),
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    try:
        conn.execute("BEGIN")
        loaded = _load(
            conn,
            text,
            validator=_validator(schema),
            run_id=checked_run,
            object_uri=object_uri,
        )
        advanced = False
        if not loaded[3] and cursors:
            _advance(conn, cursors, _iso_z(when))
            advanced = True
        result = _emit(
            directory=directory,
            run_id=checked_run,
            status=TaskStatus.SUCCESS,
            inserted=loaded[0],
            duplicates=loaded[1],
            records=loaded[2],
            dlq=loaded[3],
            rows_read=loaded[4],
            object_uri=object_uri,
            object_sha256=digest,
            task_key=task_key,
            headers=headers,
            watermark_advanced=advanced,
            errors=("data errors isolated to DLQ",) if loaded[3] else (),
        )
        conn.commit()
    except (OSError, sqlite3.Error):
        conn.rollback()
        return _emit(
            directory=directory,
            run_id=checked_run,
            status=TaskStatus.RETRYABLE_ERROR,
            inserted=0,
            duplicates=0,
            records=[],
            dlq=[],
            rows_read=0,
            object_uri=object_uri,
            object_sha256=digest,
            task_key=task_key,
            headers=headers,
            watermark_advanced=False,
            errors=("retryable_error: archive commit",),
        )
    else:
        return result
    finally:
        conn.close()


def _load(
    conn: sqlite3.Connection,
    text: str | None,
    *,
    validator: Draft202012Validator,
    run_id: str,
    object_uri: str,
) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]], int]:
    if text is None:
        dlq = [{"line": 0, "reason": "invalid_utf8", "payload": ""}]
        _insert_dlq(conn, run_id, dlq[0])
        return 0, 0, [], dlq, 0
    inserted = 0
    duplicates = 0
    records: list[dict[str, Any]] = []
    dlq = []
    lines = text.splitlines()
    for line_no, line in enumerate(lines, start=1):
        parsed, reason = _classify(line, validator)
        if reason is not None or parsed is None:
            item = {
                "line": line_no,
                "reason": "invalid_json" if reason is None else reason,
                "payload": line,
            }
            dlq.append(item)
            _insert_dlq(conn, run_id, item)
            continue
        accepted = _accepted(parsed)
        if accepted is None:
            item = {"line": line_no, "reason": "schema: record fields", "payload": line}
            dlq.append(item)
            _insert_dlq(conn, run_id, item)
            continue
        raw_uri = f"{object_uri}#line={line_no}"
        sql_id, duplicate = _upsert(conn, run_id, accepted, raw_uri)
        if duplicate:
            duplicates += 1
        else:
            inserted += 1
        records.append(_lineage(line_no, sql_id, run_id, accepted, raw_uri))
    return inserted, duplicates, records, dlq, len(lines)


def _classify(
    line: str, validator: Draft202012Validator
) -> tuple[dict[str, Any] | None, str | None]:
    if line.strip() == "":
        return None, "empty_line"
    try:
        loaded: object = json.loads(line)
    except json.JSONDecodeError:
        return None, "invalid_json"
    record = _as_dict(loaded)
    if record is None:
        return None, "schema: record must be an object"
    found = list(validator.iter_errors(record))
    if found:
        return None, "schema: " + found[0].message
    return record, None


def _accepted(record: Mapping[str, Any]) -> _Accepted | None:
    source_id = _text_field(record, "source_id")
    source_record_id = _text_field(record, "source_record_id")
    canonical_url = _text_field(record, "canonical_url")
    title = _text_field(record, "title")
    abstract = _text_field(record, "abstract_or_excerpt", allow_empty=True)
    collected_at = _text_field(record, "collected_at")
    content_sha256 = _text_field(record, "content_sha256")
    license_status = _text_field(record, "license_status")
    policy_snapshot_id = _text_field(record, "policy_snapshot_id")
    if (
        source_id is None
        or source_record_id is None
        or canonical_url is None
        or title is None
        or abstract is None
        or collected_at is None
        or content_sha256 is None
        or license_status is None
        or policy_snapshot_id is None
        or source_id not in _SOURCE_ROWS
    ):
        return None
    published_raw = record.get("published_at")
    if published_raw is None:
        published_at: str | None = None
    elif isinstance(published_raw, str) and published_raw:
        published_at = published_raw
    else:
        return None
    return _Accepted(
        source_id=source_id,
        source_record_id=source_record_id,
        canonical_url=canonical_url,
        title=title,
        abstract_or_excerpt=abstract,
        published_at=published_at,
        collected_at=collected_at,
        content_sha256=content_sha256,
        license_status=license_status,
        policy_snapshot_id=policy_snapshot_id,
    )


def _upsert(
    conn: sqlite3.Connection, run_id: str, accepted: _Accepted, raw_uri: str
) -> tuple[int, bool]:
    key = idempotency_key(
        run_id,
        accepted.source_id,
        accepted.source_record_id,
        accepted.content_sha256,
    )
    existing = conn.execute(
        "SELECT raw_record_id FROM raw_idempotency WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if existing is not None:
        return _cell_int(existing, "raw_record_id"), True
    prior = conn.execute(
        """
        SELECT id FROM raw_records
        WHERE source_id = ? AND source_record_id = ? AND content_sha256 = ?
        """,
        (accepted.source_id, accepted.source_record_id, accepted.content_sha256),
    ).fetchone()
    if prior is not None:
        sql_id = _cell_int(prior, "id")
        _remember(conn, key, run_id, sql_id, accepted)
        return sql_id, True
    family, base_url = _SOURCE_ROWS[accepted.source_id]
    conn.execute(
        """
        INSERT OR IGNORE INTO source_registry (
            source_id, source_family, base_url, policy_snapshot_id,
            enabled, requests_per_minute, checked_at
        ) VALUES (?, ?, ?, ?, 0, 10, ?)
        """,
        (accepted.source_id, family, base_url, accepted.policy_snapshot_id, accepted.collected_at),
    )
    cursor = conn.execute(
        """
        INSERT INTO raw_records (
            source_id, source_record_id, canonical_url, title, abstract_or_excerpt,
            published_at, collected_at, content_sha256, license_status,
            policy_snapshot_id, raw_uri
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            accepted.source_id,
            accepted.source_record_id,
            accepted.canonical_url,
            accepted.title,
            accepted.abstract_or_excerpt,
            accepted.published_at,
            accepted.collected_at,
            accepted.content_sha256,
            accepted.license_status,
            accepted.policy_snapshot_id,
            raw_uri,
        ),
    )
    row_id = cursor.lastrowid
    if row_id is None:
        raise sqlite3.Error("insert did not return a row id")
    _remember(conn, key, run_id, row_id, accepted)
    return row_id, False


def _remember(
    conn: sqlite3.Connection,
    key: str,
    run_id: str,
    sql_id: int,
    accepted: _Accepted,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_idempotency (
            idempotency_key, run_id, raw_record_id, source_id, source_record_id, content_sha256
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            run_id,
            sql_id,
            accepted.source_id,
            accepted.source_record_id,
            accepted.content_sha256,
        ),
    )


def _lineage(
    line_no: int, sql_id: int, run_id: str, accepted: _Accepted, raw_uri: str
) -> dict[str, Any]:
    return {
        "line": line_no,
        "sql_id": sql_id,
        "idempotency_key": idempotency_key(
            run_id,
            accepted.source_id,
            accepted.source_record_id,
            accepted.content_sha256,
        ),
        "raw_uri": raw_uri,
        "source_id": accepted.source_id,
        "source_record_id": accepted.source_record_id,
        "canonical_url": accepted.canonical_url,
        "collected_at": accepted.collected_at,
        "published_at": accepted.published_at,
        "policy_snapshot_id": accepted.policy_snapshot_id,
        "content_sha256": accepted.content_sha256,
    }


def _insert_dlq(conn: sqlite3.Connection, run_id: str, item: Mapping[str, Any]) -> None:
    line_no = item["line"]
    reason = item["reason"]
    payload = item["payload"]
    if isinstance(line_no, bool) or not isinstance(line_no, int):
        raise sqlite3.Error("dlq line must be an integer")
    if not isinstance(reason, str) or not isinstance(payload, str):
        raise sqlite3.Error("dlq reason and payload must be text")
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_dlq (run_id, line_no, reason, payload)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, line_no, reason, payload),
    )


def _advance(conn: sqlite3.Connection, cursors: Mapping[str, str], updated_at: str) -> None:
    for source_id, cursor in cursors.items():
        conn.execute(
            """
            INSERT INTO archive_watermarks (source_id, cursor, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                cursor = excluded.cursor,
                updated_at = excluded.updated_at
            """,
            (source_id, cursor, updated_at),
        )


def _emit(
    *,
    directory: Path,
    run_id: str,
    status: TaskStatus,
    inserted: int,
    duplicates: int,
    records: list[dict[str, Any]],
    dlq: list[dict[str, Any]],
    rows_read: int,
    object_uri: str,
    object_sha256: str,
    task_key: str,
    headers: dict[str, str],
    watermark_advanced: bool,
    errors: tuple[str, ...],
) -> ArchiveResult:
    metrics: dict[str, int | str | bool] = {
        "rows_read": rows_read,
        "inserted": inserted,
        "duplicates": duplicates,
        "dlq": len(dlq),
        "watermark_advanced": watermark_advanced,
        "object_sha256": object_sha256,
        "third_party_writes": 0,
        "status": status.value,
    }
    directory.mkdir(parents=True, exist_ok=True)
    raw_manifest = {
        "ticket": "D10",
        "run_id": run_id,
        "object_uri": object_uri,
        "object_sha256": object_sha256,
        "task_idempotency_key": task_key,
        "response_headers": headers,
        "watermark_advanced": watermark_advanced,
        "records": records,
        "dlq": [{"line": item["line"], "reason": item["reason"]} for item in dlq],
    }
    manifest = {
        "ticket": "D10",
        "run_id": run_id,
        "status": status.value,
        "rows": len(records),
        "object_uri": object_uri,
        "object_sha256": object_sha256,
        "task_idempotency_key": task_key,
        "raw_manifest": "raw_manifest.json",
        "watermark_advanced": watermark_advanced,
        "errors": list(errors),
    }
    _write_fsynced(directory / "raw_manifest.json", _json(raw_manifest))
    _write_fsynced(directory / "manifest.json", _json(manifest))
    _write_fsynced(directory / "metrics.json", _json(metrics))
    dlq_lines = [
        json.dumps(
            {"line": item["line"], "reason": item["reason"], "payload": item["payload"]},
            ensure_ascii=False,
        )
        for item in dlq
    ]
    dlq_body = "".join(f"{line}\n" for line in dlq_lines)
    _write_fsynced(directory / "dlq.ndjson", dlq_body)
    log = {
        "event": "d10_archive",
        "run_id": run_id,
        "status": status.value,
        "inserted": inserted,
        "dlq": len(dlq),
        "watermark_advanced": watermark_advanced,
    }
    _write_fsynced(directory / "log.jsonl", json.dumps(log, sort_keys=True) + "\n")
    return ArchiveResult(
        status=status,
        run_id=run_id,
        inserted=inserted,
        duplicates=duplicates,
        dlq=len(dlq),
        watermark_advanced=watermark_advanced,
        object_uri=object_uri,
        object_sha256=object_sha256,
        manifest_dir=str(directory),
        errors=errors,
        metrics=metrics,
    )


def _validator(path: Path) -> Draft202012Validator:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    schema = _as_dict(loaded)
    if schema is None:
        raise ValueError("raw record schema is not an object")
    return Draft202012Validator(schema)


def _public_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if headers is None:
        return {}
    safe: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if _SECRET_HEADER.search(key):
            continue
        safe[key] = redact_secrets(value)
    return dict(sorted(safe.items()))


def _text_field(record: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str | None:
    value = record.get(key)
    if not isinstance(value, str):
        return None
    if value == "" and not allow_empty:
        return None
    return value


def _as_dict(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        parsed[key] = item
    return parsed


def _cell_int(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise sqlite3.Error(f"expected integer {key}")
    return value


def _relative_key(key: str) -> Path:
    if not key or "\\" in key or key.startswith("/"):
        raise ValueError("invalid object key")
    parts = key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid object key")
    return Path(*parts)


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."}:
        raise ValueError("invalid run_id")
    if any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _archived_at(value: datetime | None) -> datetime:
    when = datetime.now(UTC) if value is None else value
    if when.tzinfo is None:
        raise ValueError("archived_at must be timezone-aware")
    return when


def _iso_z(value: datetime) -> str:
    text = value.astimezone(UTC).isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def _json(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _write_fsynced(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
