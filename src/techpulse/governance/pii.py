"""D21 PII detection, irreversible redaction, and deletion mapping.

Cleaned text is scanned for phone numbers, email addresses, id-document
shapes, and token or password assignments. Matches are replaced with a
fixed marker. The matched span is not stored. Name fields used for
deduplication are stored only as HMAC-SHA256 digests. The key is read from
the process environment (``TECHPULSE_PII_HMAC_KEY``). This module does not
open a ``.env`` file and does not call a network.

Chunk and vector bytes go through ``ObjectStore``. ``LocalObjectStore``
writes under the artifacts root. There is no S3 client here. The deletion
rows live in SQLite, not Postgres. A leak in text offered as embedding
input is ``fatal_error``: nothing is stored, the run is marked isolated,
and an ops notice is written.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import importlib.util
import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from techpulse.contracts import TaskStatus
from techpulse.governance.policy import redact_secrets
from techpulse.workflows.activities import ObjectStore

HMAC_ENV = "TECHPULSE_PII_HMAC_KEY"
PARQUET_NOTE = "pyarrow is not installed; wrote sanitized.jsonl instead of sanitized.parquet"
_MARKER = "[REDACTED]"
_LEAK = "fatal_error: pii leak in embedding input"
_NAME_KEYS = frozenset(
    {
        "author",
        "authors",
        "author_name",
        "author_names",
        "name",
        "display_name",
        "full_name",
    }
)
_DROP_KEYS = frozenset(
    {
        "email",
        "e_mail",
        "author_email",
        "contact_email",
        "email_address",
        "phone",
        "phone_number",
        "mobile",
        "telephone",
        "address",
        "home_address",
        "ip",
        "ip_address",
        "username",
        "user_name",
        "login",
        "contact",
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "access_token",
        "ssn",
        "national_id",
        "id_number",
        "passport",
        "passport_number",
        "birthday",
        "birth_date",
        "date_of_birth",
    }
)
_ID_KEYS = frozenset(
    {
        "source_id",
        "source_record_id",
        "content_sha256",
        "policy_snapshot_id",
        "raw_uri",
        "chunk_id",
        "vector_id",
        "language",
        "license_status",
        "retention",
        "schema_version",
        "published_at",
        "collected_at",
        "published_at_missing_reason",
        "extraction_failure_reason",
    }
)
_ASSIGNMENT = re.compile(r"(?i)\b[\w.-]*(?:api[_-]?key|secret|password|token)\b\s*[:=]\s*\S+")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
_LABELED_ID = re.compile(
    r"(?i)\b(?:ssn|passport|national(?:\s+id)?|id(?:\s+number)?)\b\s*[:#]?\s*[A-Za-z0-9]{6,20}\b"
)
_CN_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_E164 = re.compile(r"\+\d{1,3}(?:[\s.\-]\d{2,4}){2,4}")
_NANP = re.compile(r"(?<!\d)(?:\+1[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)")
_CN_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PATTERNS = (_EMAIL, _LABELED_ID, _CN_ID, _SSN, _E164, _NANP, _CN_MOBILE)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS deletion_map (
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    chunk_uri TEXT NOT NULL,
    vector_id TEXT NOT NULL,
    vector_uri TEXT NOT NULL,
    report_id TEXT,
    report_uri TEXT,
    PRIMARY KEY (source_id, source_record_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS ix_deletion_map_source_id ON deletion_map (source_id);
CREATE INDEX IF NOT EXISTS ix_deletion_map_source_record ON deletion_map (source_record_id);
CREATE TABLE IF NOT EXISTS run_guard (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    index_isolated INTEGER NOT NULL,
    ops_notified INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class SanitizeResult:
    status: TaskStatus
    run_id: str
    sanitized_rows: int
    leaks: int
    output_format: str
    note: str
    errors: tuple[str, ...]
    metrics: dict[str, int | float | str | bool | None]


@dataclass(frozen=True)
class DerivedArtifact:
    source_id: str
    source_record_id: str
    chunk_id: str
    chunk_uri: str
    vector_id: str
    vector_uri: str
    report_id: str | None
    report_uri: str | None


@dataclass(frozen=True)
class _Prepared:
    row: dict[str, object]
    embedding_input: str
    spans: int
    dropped: int
    names: int
    source_id: str
    source_record_id: str


def redact_text(text: str) -> tuple[str, int]:
    """Replace PII and credential spans. The original span is discarded."""
    total = 0
    current, found = _replace(_ASSIGNMENT, text)
    total += found
    current, found = _replace_secrets(current)
    total += found
    for pattern in _PATTERNS:
        current, found = _replace(pattern, current)
        total += found
    return current, total


def contains_pii(text: str) -> bool:
    """True when a phone, email, id, token, or password pattern is still present."""
    _redacted, count = redact_text(text)
    return count > 0


def sanitize_from_clean(
    *,
    run_id: str,
    artifacts_dir: Path,
    database: Path,
    store: ObjectStore,
) -> SanitizeResult:
    """Sanitize the D20 clean file for one run."""
    checked = _run_id(run_id)
    manifest_path = artifacts_dir / checked / "D20" / "manifest.json"
    loaded = _read_clean(manifest_path)
    if isinstance(loaded, str):
        return _fail(
            artifacts_dir / checked / "D21",
            checked,
            database,
            TaskStatus.FATAL_ERROR,
            (loaded,),
            isolate=False,
        )
    rows, error = loaded
    if error is not None:
        return _fail(
            artifacts_dir / checked / "D21",
            checked,
            database,
            TaskStatus.FATAL_ERROR,
            (error,),
            isolate=False,
        )
    return sanitize_records(
        rows,
        run_id=checked,
        artifacts_dir=artifacts_dir,
        database=database,
        store=store,
    )


def sanitize_records(
    records: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    artifacts_dir: Path,
    database: Path,
    store: ObjectStore,
) -> SanitizeResult:
    """Redact cleaned rows and write the sanitized file plus the deletion map."""
    checked = _run_id(run_id)
    directory = artifacts_dir / checked / "D21"
    prepared, prepare_error = _prepare_all(records)
    if prepare_error is not None:
        return _fail(
            directory,
            checked,
            database,
            TaskStatus.FATAL_ERROR,
            (prepare_error,),
            isolate=prepare_error == _LEAK,
        )
    if any(_row_leaks(item) for item in prepared):
        return _fail(
            directory,
            checked,
            database,
            TaskStatus.FATAL_ERROR,
            (_LEAK,),
            isolate=True,
        )
    return _commit(directory, checked, database, store, prepared)


def release_embedding_input(
    text: str,
    *,
    run_id: str,
    source_id: str,
    source_record_id: str,
    artifacts_dir: Path,
    database: Path,
    store: ObjectStore,
) -> SanitizeResult:
    """Store text as embedding input only when it has no PII or credential pattern."""
    if contains_pii(text):
        checked = _run_id(run_id)
        return _fail(
            artifacts_dir / checked / "D21",
            checked,
            database,
            TaskStatus.FATAL_ERROR,
            (_LEAK,),
            isolate=True,
        )
    row: dict[str, object] = {
        "source_id": source_id,
        "source_record_id": source_record_id,
        "title": text,
        "abstract_or_excerpt": "",
    }
    return sanitize_records(
        (row,),
        run_id=run_id,
        artifacts_dir=artifacts_dir,
        database=database,
        store=store,
    )


def locate_derived(database: Path, source_id: str) -> tuple[DerivedArtifact, ...]:
    """Return chunk, vector, and report rows for a source id or source record id."""
    if not database.is_file():
        return ()
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'deletion_map'"
        ).fetchone()
        if found is None:
            return ()
        rows = conn.execute(
            """
            SELECT source_id, source_record_id, chunk_id, chunk_uri,
                   vector_id, vector_uri, report_id, report_uri
            FROM deletion_map
            WHERE source_id = ? OR source_record_id = ?
            ORDER BY source_record_id, chunk_id
            """,
            (source_id, source_id),
        ).fetchall()
    finally:
        conn.close()
    return tuple(_derived(row) for row in rows)


def attach_report(
    database: Path,
    *,
    source_id: str,
    source_record_id: str,
    report_id: str,
    report_uri: str,
) -> None:
    """Record the report derived from a source record. Does not rewrite chunk bytes."""
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    try:
        conn.executescript(_SCHEMA)
        cursor = conn.execute(
            """
            UPDATE deletion_map
            SET report_id = ?, report_uri = ?
            WHERE source_id = ? AND source_record_id = ?
            """,
            (report_id, report_uri, source_id, source_record_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("unknown source record")
        conn.commit()
    finally:
        conn.close()


def _prepare_all(
    records: Sequence[Mapping[str, object]],
) -> tuple[list[_Prepared], str | None]:
    key = _hmac_key()
    prepared: list[_Prepared] = []
    for record in records:
        item, error = _prepare_one(record, key)
        if error is not None:
            return [], error
        if item is not None:
            prepared.append(item)
    return prepared, None


def _prepare_one(
    record: Mapping[str, object], key: str | None
) -> tuple[_Prepared | None, str | None]:
    source_id = _identifier(record.get("source_id"))
    source_record_id = _identifier(record.get("source_record_id"))
    if source_id is None or source_record_id is None:
        return None, "fatal_error: source id missing"
    if contains_pii(source_id) or contains_pii(source_record_id):
        return None, _LEAK
    names = _collect_names(record)
    if names and key is None:
        return None, "fatal_error: hmac key missing"
    digests = [] if key is None else _hmac_names(names, key)
    spans = 0
    dropped = 0
    row: dict[str, object] = {}
    for field, value in record.items():
        kind = _classify_key(field)
        if kind == "name":
            continue
        if kind == "drop":
            dropped += 1
            continue
        if not _keep_value(value):
            dropped += 1
            continue
        stored, found = _stored_value(field, value)
        spans += found
        row[field] = stored
    title = row.get("title")
    body = row.get("abstract_or_excerpt")
    title_text = title if isinstance(title, str) else ""
    body_text = body if isinstance(body, str) else ""
    embedding = "\n".join(part for part in (title_text, body_text) if part)
    chunk_id = _digest("chunk", source_id, source_record_id, embedding)
    vector_id = _digest("vector", chunk_id)
    row["source_id"] = source_id
    row["source_record_id"] = source_record_id
    row["name_hmacs"] = digests
    row["chunk_id"] = chunk_id
    row["vector_id"] = vector_id
    row["embedding_input"] = embedding
    item = _Prepared(
        row=row,
        embedding_input=embedding,
        spans=spans,
        dropped=dropped,
        names=len(digests),
        source_id=source_id,
        source_record_id=source_record_id,
    )
    return item, None


def _row_leaks(item: _Prepared) -> bool:
    if contains_pii(item.embedding_input):
        return True
    for field, value in item.row.items():
        if field in _ID_KEYS or field in {"name_hmacs", "embedding_input"}:
            continue
        if isinstance(value, str) and contains_pii(value):
            return True
    return False


def _commit(
    directory: Path,
    run_id: str,
    database: Path,
    store: ObjectStore,
    prepared: list[_Prepared],
) -> SanitizeResult:
    bodies: list[tuple[_Prepared, str, bytes, str, bytes]] = []
    for item in prepared:
        chunk_key = f"{run_id}/D21/chunks/{item.row['chunk_id']}.json"
        vector_key = f"{run_id}/D21/vectors/{item.row['vector_id']}.json"
        chunk_body = _pretty(
            {
                "chunk_id": item.row["chunk_id"],
                "embedding_input": item.embedding_input,
                "source_id": item.source_id,
                "source_record_id": item.source_record_id,
            }
        )
        vector_body = _pretty(
            {
                "chunk_id": item.row["chunk_id"],
                "source_id": item.source_id,
                "source_record_id": item.source_record_id,
                "vector_id": item.row["vector_id"],
            }
        )
        bodies.append((item, chunk_key, chunk_body, vector_key, vector_body))
    uris: list[tuple[_Prepared, str, str]] = []
    try:
        for item, chunk_key, chunk_body, vector_key, vector_body in bodies:
            chunk_uri = store.put(chunk_key, chunk_body)
            vector_uri = store.put(vector_key, vector_body)
            uris.append((item, chunk_uri, vector_uri))
    except OSError:
        return _fail(
            directory,
            run_id,
            database,
            TaskStatus.RETRYABLE_ERROR,
            ("retryable_error: object store",),
            isolate=False,
        )
    try:
        _insert_map(database, run_id, uris, TaskStatus.SUCCESS, isolated=False)
    except sqlite3.Error:
        return _fail(
            directory,
            run_id,
            database,
            TaskStatus.RETRYABLE_ERROR,
            ("retryable_error: deletion map",),
            isolate=False,
        )
    rows = [item.row for item, _chunk_uri, _vector_uri in uris]
    return _emit(
        directory=directory,
        run_id=run_id,
        status=TaskStatus.SUCCESS,
        rows=rows,
        spans=sum(item.spans for item in prepared),
        dropped=sum(item.dropped for item in prepared),
        names=sum(item.names for item in prepared),
        leaks=0,
        errors=(),
        isolated=False,
        write_rows=True,
    )


def _insert_map(
    database: Path,
    run_id: str,
    uris: Sequence[tuple[_Prepared, str, str]],
    status: TaskStatus,
    *,
    isolated: bool,
) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    try:
        conn.executescript(_SCHEMA)
        conn.execute("BEGIN")
        for item, chunk_uri, vector_uri in uris:
            chunk_id = item.row["chunk_id"]
            vector_id = item.row["vector_id"]
            if not isinstance(chunk_id, str) or not isinstance(vector_id, str):
                raise sqlite3.Error("chunk id")
            conn.execute(
                """
                INSERT INTO deletion_map (
                    source_id, source_record_id, chunk_id, chunk_uri,
                    vector_id, vector_uri, report_id, report_uri
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT (source_id, source_record_id, chunk_id) DO UPDATE SET
                    chunk_uri = excluded.chunk_uri,
                    vector_id = excluded.vector_id,
                    vector_uri = excluded.vector_uri
                """,
                (item.source_id, item.source_record_id, chunk_id, chunk_uri, vector_id, vector_uri),
            )
        _guard(conn, run_id, status, isolated=isolated, notified=False)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def _fail(
    directory: Path,
    run_id: str,
    database: Path,
    status: TaskStatus,
    errors: tuple[str, ...],
    isolate: bool,
) -> SanitizeResult:
    try:
        _guard_only(database, run_id, status, isolated=isolate)
    except sqlite3.Error:
        errors = (*errors, "retryable_error: deletion map")
    if isolate:
        _write_bytes(
            directory / "ops_notice.json",
            _pretty(
                {"action": "isolate_object_store_and_index", "event": "pii_leak", "run_id": run_id}
            ),
        )
    return _emit(
        directory=directory,
        run_id=run_id,
        status=status,
        rows=[],
        spans=0,
        dropped=0,
        names=0,
        leaks=1 if isolate else 0,
        errors=errors,
        isolated=isolate,
        write_rows=False,
    )


def _guard_only(database: Path, run_id: str, status: TaskStatus, *, isolated: bool) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    try:
        conn.executescript(_SCHEMA)
        _guard(conn, run_id, status, isolated=isolated, notified=isolated)
        conn.commit()
    finally:
        conn.close()


def _guard(
    conn: sqlite3.Connection,
    run_id: str,
    status: TaskStatus,
    *,
    isolated: bool,
    notified: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO run_guard (run_id, status, index_isolated, ops_notified)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (run_id) DO UPDATE SET
            status = excluded.status,
            index_isolated = excluded.index_isolated,
            ops_notified = excluded.ops_notified
        """,
        (run_id, status.value, 1 if isolated else 0, 1 if notified else 0),
    )


def _emit(
    *,
    directory: Path,
    run_id: str,
    status: TaskStatus,
    rows: list[dict[str, object]],
    spans: int,
    dropped: int,
    names: int,
    leaks: int,
    errors: tuple[str, ...],
    isolated: bool,
    write_rows: bool,
) -> SanitizeResult:
    directory.mkdir(parents=True, exist_ok=True)
    note = ""
    output_format = "parquet"
    sanitized_name: str | None = None
    sanitized_sha: str | None = None
    if write_rows:
        sanitized_name, output_format, note, sanitized_sha = _write_sanitized(directory, rows)
    elif importlib.util.find_spec("pyarrow") is None:
        note = PARQUET_NOTE
        output_format = "jsonl"
    metrics: dict[str, int | float | str | bool | None] = {
        "isolated": isolated,
        "leaks": leaks,
        "names_hmac": names,
        "ops_notified": isolated,
        "output_format": output_format,
        "parquet_note": note,
        "personal_fields_dropped": dropped,
        "pii_spans_redacted": spans,
        "rows": len(rows) if write_rows else 0,
        "sanitized_rows": len(rows) if write_rows else 0,
        "status": status.value,
        "third_party_writes": 0,
    }
    manifest = {
        "errors": list(errors),
        "isolated": isolated,
        "leaks": leaks,
        "note": note,
        "ops_notified": isolated,
        "output_format": output_format,
        "rows": len(rows) if write_rows else 0,
        "run_id": run_id,
        "sanitized_name": sanitized_name,
        "sanitized_sha256": sanitized_sha,
        "status": status.value,
        "ticket": "D21",
    }
    _write_bytes(directory / "manifest.json", _pretty(manifest))
    _write_bytes(directory / "metrics.json", _pretty(metrics))
    log = {
        "event": "d21_sanitize",
        "isolated": isolated,
        "leaks": leaks,
        "run_id": run_id,
        "sanitized_rows": len(rows) if write_rows else 0,
        "status": status.value,
    }
    _write_bytes(directory / "log.jsonl", (json.dumps(log, sort_keys=True) + "\n").encode("utf-8"))
    return SanitizeResult(
        status=status,
        run_id=run_id,
        sanitized_rows=len(rows) if write_rows else 0,
        leaks=leaks,
        output_format=output_format,
        note=note,
        errors=errors,
        metrics=metrics,
    )


def _write_sanitized(directory: Path, rows: list[dict[str, object]]) -> tuple[str, str, str, str]:
    if importlib.util.find_spec("pyarrow") is None:
        body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        data = body.encode("utf-8")
        _write_bytes(directory / "sanitized.jsonl", data)
        return "sanitized.jsonl", "jsonl", PARQUET_NOTE, hashlib.sha256(data).hexdigest()
    _write_parquet(directory / "sanitized.parquet", rows)
    data = (directory / "sanitized.parquet").read_bytes()
    return "sanitized.parquet", "parquet", "", hashlib.sha256(data).hexdigest()


def _write_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    pyarrow: Any = importlib.import_module("pyarrow")
    parquet: Any = importlib.import_module("pyarrow.parquet")
    table = pyarrow.Table.from_pylist(rows)
    parquet.write_table(table, path)


def _read_clean(path: Path) -> tuple[list[dict[str, object]], None] | str:
    if not path.is_file():
        return "fatal_error: clean manifest is incomplete"
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "fatal_error: clean manifest is incomplete"
    document = _as_dict(loaded)
    if document is None:
        return "fatal_error: clean manifest is incomplete"
    if document.get("status") != TaskStatus.SUCCESS.value:
        return "fatal_error: clean run is not success"
    name = document.get("clean_name")
    if not isinstance(name, str):
        return "fatal_error: clean manifest is incomplete"
    clean_path = path.parent / name
    try:
        payload = clean_path.read_bytes()
    except OSError:
        return "fatal_error: clean manifest is incomplete"
    declared = document.get("clean_sha256")
    if isinstance(declared, str) and declared.lower() != hashlib.sha256(payload).hexdigest():
        return "fatal_error: clean sha256 mismatch"
    if name.endswith(".jsonl"):
        return _read_jsonl(payload), None
    if name.endswith(".parquet"):
        return _read_parquet(clean_path), None
    return "fatal_error: clean manifest is incomplete"


def _read_jsonl(payload: bytes) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    text = payload.decode("utf-8")
    for line in text.splitlines():
        if line.strip() == "":
            continue
        parsed = _as_dict(json.loads(line))
        if parsed is None:
            continue
        rows.append(parsed)
    return rows


def _read_parquet(path: Path) -> list[dict[str, object]]:
    parquet: Any = importlib.import_module("pyarrow.parquet")
    table: Any = parquet.read_table(path)
    raw: object = table.to_pylist()
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, object]] = []
    for item in raw:
        parsed = _as_dict(item)
        if parsed is not None:
            rows.append(parsed)
    return rows


def _collect_names(record: Mapping[str, object]) -> list[str]:
    found: list[str] = []
    for field, value in record.items():
        if _classify_key(field) != "name":
            continue
        found.extend(_name_values(value))
    return found


def _name_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    found: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            found.append(item)
            continue
        mapped = _as_dict(item)
        if mapped is None:
            continue
        for key in ("name", "display_name", "full_name", "author"):
            inner = mapped.get(key)
            if isinstance(inner, str) and inner.strip():
                found.append(inner)
                break
    return found


def _hmac_names(names: list[str], key: str) -> list[str]:
    digests: list[str] = []
    seen: set[str] = set()
    secret = key.encode("utf-8")
    for name in names:
        normalized = " ".join(name.casefold().split())
        if not normalized:
            continue
        digest = hmac.new(secret, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
        if digest not in seen:
            seen.add(digest)
            digests.append(digest)
    digests.sort()
    return digests


def _hmac_key() -> str | None:
    value = os.environ.get(HMAC_ENV)
    if value is None or value == "":
        return None
    return value


def _classify_key(key: str) -> str:
    normal = key.strip().lower().replace("-", "_")
    if normal in _NAME_KEYS:
        return "name"
    if normal in _DROP_KEYS:
        return "drop"
    return "keep"


def _stored_value(field: str, value: object) -> tuple[object, int]:
    if field in _ID_KEYS:
        return value, 0
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        spans = 0
        stored: list[object] = []
        for item in value:
            if isinstance(item, str):
                redacted, count = redact_text(item)
                stored.append(redacted)
                spans += count
            else:
                stored.append(item)
        return stored, spans
    return value, 0


def _keep_value(value: object) -> bool:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list):
        return all(item is None or isinstance(item, str | int | float | bool) for item in value)
    return False


def _identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(char in text for char in "\r\n\x00"):
        return None
    return text


def _derived(row: sqlite3.Row) -> DerivedArtifact:
    return DerivedArtifact(
        source_id=_cell_str(row, "source_id"),
        source_record_id=_cell_str(row, "source_record_id"),
        chunk_id=_cell_str(row, "chunk_id"),
        chunk_uri=_cell_str(row, "chunk_uri"),
        vector_id=_cell_str(row, "vector_id"),
        vector_uri=_cell_str(row, "vector_uri"),
        report_id=_cell_optional(row, "report_id"),
        report_uri=_cell_optional(row, "report_uri"),
    )


def _cell_str(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise sqlite3.Error(f"expected string {key}")
    return value


def _cell_optional(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise sqlite3.Error(f"expected string {key}")
    return value


def _replace(pattern: re.Pattern[str], text: str) -> tuple[str, int]:
    count = 0

    def repl(_match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return _MARKER

    return pattern.sub(repl, text), count


def _replace_secrets(text: str) -> tuple[str, int]:
    redacted = redact_secrets(text)
    added = redacted.count(_MARKER) - text.count(_MARKER)
    return redacted, max(added, 0)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        parsed[key] = item
    return parsed


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."} or any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _pretty(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
