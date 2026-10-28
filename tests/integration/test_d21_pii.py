"""D21 redaction and deletion-map tests. Fixtures are local. No live HTTP."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pytest import MonkeyPatch

from techpulse.contracts import TaskStatus
from techpulse.governance.pii import (
    HMAC_ENV,
    PARQUET_NOTE,
    attach_report,
    locate_derived,
    release_embedding_input,
    sanitize_from_clean,
    sanitize_records,
)
from techpulse.normalize.clean import clean_ndjson
from techpulse.workflows.activities import LocalObjectStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SECRET_FIXTURE = FIXTURES / "fake_secret_sample.txt"
GITHUB_FIXTURE = FIXTURES / "github" / "repository.json"
_AUTHOR = "Ada Fixture"
_PHONE = "+1 202 555 0147"
_NANP = "(202) 555-0147"
_MOBILE = "13800000000"
_SSN = "000-00-0000"
_CN_ID = "00000019000101000X"
_PASSPORT = "AB1234567"
_PASSWORD = "not-a-real-password"
_KEY = "fixture-hmac-key"
_OTHER_KEY = "fixture-hmac-key-2"


def test_security_fixtures_never_reach_embedding_input(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(HMAC_ENV, _KEY)
    secret = SECRET_FIXTURE.read_text(encoding="utf-8")
    token = _token(secret)
    email = _github_email()
    for path in (SECRET_FIXTURE, GITHUB_FIXTURE):
        assert path.stat().st_size < 100_000
    abstract = "\n".join(
        (
            secret,
            f"reach {email} or {_PHONE} and {_NANP}",
            f"mobile {_MOBILE}",
            f"ssn {_SSN}",
            f"id {_CN_ID}",
            f"passport {_PASSPORT}",
            f"password={_PASSWORD}",
        )
    )
    record = _row(
        "sec-010",
        abstract,
        email=email,
        phone=_PHONE,
        authors=[_AUTHOR],
        username="do-not-keep",
    )
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "d21.sqlite"
        result = sanitize_records(
            [record],
            run_id="d21-sec",
            artifacts_dir=artifacts,
            database=database,
            store=LocalObjectStore(artifacts),
        )
        assert result.status is TaskStatus.SUCCESS
        assert result.errors == ()
        assert result.leaks == 0
        assert result.metrics["third_party_writes"] == 0
        dropped = result.metrics["personal_fields_dropped"]
        spans = result.metrics["pii_spans_redacted"]
        assert isinstance(dropped, int) and dropped >= 3
        assert result.metrics["names_hmac"] == 1
        assert isinstance(spans, int) and spans > 0
        directory = artifacts / "d21-sec" / "D21"
        _assert_format(result.output_format, result.note, directory)
        rows = _rows(_sanitized_path(directory))
        assert len(rows) == 1
        saved = rows[0]
        assert "email" not in saved
        assert "phone" not in saved
        assert "username" not in saved
        assert "authors" not in saved
        embedding = saved["embedding_input"]
        assert isinstance(embedding, str)
        digest = saved["name_hmacs"]
        assert isinstance(digest, list)
        assert digest and all(
            isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) for item in digest
        )
        needles = _needles(token, email)
        _assert_clean(directory, database, needles)
        assert _KEY.encode() not in database.read_bytes()
        manifest = _object(directory / "manifest.json")
        assert manifest["ticket"] == "D21"
        assert manifest["isolated"] is False
        assert manifest["ops_notified"] is False
        assert (
            manifest["sanitized_sha256"]
            == hashlib.sha256(_sanitized_path(directory).read_bytes()).hexdigest()
        )


def test_names_are_hmac_and_a_missing_key_stores_nothing(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(HMAC_ENV, _KEY)
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "d21.sqlite"
        store = LocalObjectStore(artifacts)
        left = _row("name-a", "Signal notes", authors=[_AUTHOR])
        right = _row("name-b", "Signal notes", authors=["  ada   fixture "])
        other = _row("name-c", "Signal notes", authors=["Lin Query"])
        result = sanitize_records(
            [left, right, other],
            run_id="d21-names",
            artifacts_dir=artifacts,
            database=database,
            store=store,
        )
        assert result.status is TaskStatus.SUCCESS
        rows = {
            str(row["source_record_id"]): row
            for row in _rows(_sanitized_path(artifacts / "d21-names" / "D21"))
        }
        assert rows["name-a"]["name_hmacs"] == rows["name-b"]["name_hmacs"]
        assert rows["name-a"]["name_hmacs"] != rows["name-c"]["name_hmacs"]
        _assert_clean(artifacts / "d21-names" / "D21", database, [_AUTHOR, "Lin Query", _KEY])

    monkeypatch.setenv(HMAC_ENV, _OTHER_KEY)
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        result = sanitize_records(
            [_row("name-a", "Signal notes", authors=[_AUTHOR])],
            run_id="d21-names-2",
            artifacts_dir=artifacts,
            database=root / "d21.sqlite",
            store=LocalObjectStore(artifacts),
        )
        assert result.status is TaskStatus.SUCCESS
        changed = _rows(_sanitized_path(artifacts / "d21-names-2" / "D21"))[0]["name_hmacs"]
        assert changed != rows["name-a"]["name_hmacs"]
        _assert_clean(artifacts / "d21-names-2" / "D21", root / "d21.sqlite", [_AUTHOR, _OTHER_KEY])

    monkeypatch.delenv(HMAC_ENV, raising=False)
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "d21.sqlite"
        result = sanitize_records(
            [_row("name-missing", "Signal notes", authors=[_AUTHOR])],
            run_id="d21-no-key",
            artifacts_dir=artifacts,
            database=database,
            store=LocalObjectStore(artifacts),
        )
        assert result.status is TaskStatus.FATAL_ERROR
        assert result.errors == ("fatal_error: hmac key missing",)
        directory = artifacts / "d21-no-key" / "D21"
        assert not _sanitized_path(directory).exists()
        assert not (directory / "ops_notice.json").exists()
        _assert_clean(directory, database, [_AUTHOR, _KEY, _OTHER_KEY])
        assert _guard(database, "d21-no-key") == (0, 0)


def test_deletion_chain_locates_chunk_vector_and_report(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(HMAC_ENV, _KEY)
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "d21.sqlite"
        store = LocalObjectStore(artifacts)
        records = [
            _row("sec-010", "Retrieval notes", authors=[_AUTHOR], source_id="github"),
            _row("other-1", "Another note", source_id="arxiv"),
        ]
        result = sanitize_records(
            records,
            run_id="d21-chain",
            artifacts_dir=artifacts,
            database=database,
            store=store,
        )
        assert result.status is TaskStatus.SUCCESS
        report_body = b'{"report_id":"report-1","text":"weekly note"}\n'
        report_uri = store.put("d21-chain/D21/reports/report-1.json", report_body)
        attach_report(
            database,
            source_id="github",
            source_record_id="sec-010",
            report_id="report-1",
            report_uri=report_uri,
        )
        found = locate_derived(database, "sec-010")
        assert len(found) == 1
        item = found[0]
        assert item.source_id == "github"
        assert item.source_record_id == "sec-010"
        assert item.report_id == "report-1"
        assert store.get(item.chunk_uri)
        assert store.get(item.vector_uri)
        assert store.get(item.report_uri or "") == report_body
        chunk = store.get(item.chunk_uri).decode("utf-8")
        vector = store.get(item.vector_uri).decode("utf-8")
        assert item.chunk_id in chunk
        assert item.vector_id in vector
        assert "embedding_input" in chunk
        assert _AUTHOR not in chunk
        assert _AUTHOR not in vector
        by_source = locate_derived(database, "github")
        assert [row.source_record_id for row in by_source] == ["sec-010"]
        assert locate_derived(database, "missing") == ()
        assert _guard(database, "d21-chain") == (0, 0)


def test_embedding_leak_is_fatal_and_isolates(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv(HMAC_ENV, raising=False)
    secret = SECRET_FIXTURE.read_text(encoding="utf-8")
    token = _token(secret)
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "d21.sqlite"
        result = release_embedding_input(
            secret,
            run_id="d21-leak",
            source_id="github",
            source_record_id="sec-010",
            artifacts_dir=artifacts,
            database=database,
            store=LocalObjectStore(artifacts),
        )
        assert result.status is TaskStatus.FATAL_ERROR
        assert result.errors == ("fatal_error: pii leak in embedding input",)
        assert result.leaks == 1
        assert result.sanitized_rows == 0
        assert result.metrics["third_party_writes"] == 0
        directory = artifacts / "d21-leak" / "D21"
        assert not _sanitized_path(directory).exists()
        notice = _object(directory / "ops_notice.json")
        assert notice["event"] == "pii_leak"
        assert notice["action"] == "isolate_object_store_and_index"
        manifest = _object(directory / "manifest.json")
        assert manifest["isolated"] is True
        assert manifest["ops_notified"] is True
        _assert_clean(directory, database, [token, "ghp_"])
        assert locate_derived(database, "sec-010") == ()
        assert _guard(database, "d21-leak") == (1, 1)


def test_sanitizes_d20_clean_output(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv(HMAC_ENV, raising=False)
    email = _github_email()
    token = _token(SECRET_FIXTURE.read_text(encoding="utf-8"))
    body = f"See {email} and {token} or {_PHONE}."
    raw = {
        "schema_version": "1.0",
        "source_id": "github",
        "source_record_id": "d20-pii",
        "canonical_url": "https://example.test/d20-pii",
        "title": "Clean then redact",
        "abstract_or_excerpt": body,
        "published_at": "2026-10-27T00:00:00Z",
        "collected_at": "2026-10-27T00:00:00Z",
        "language": "en",
        "license_status": "fulltext_allowed",
        "content_sha256": hashlib.sha256(b"d20-pii").hexdigest(),
        "policy_snapshot_id": "fixture",
    }
    payload = (json.dumps(raw) + "\n").encode("utf-8")
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "d21.sqlite"
        store = LocalObjectStore(artifacts)
        cleaned = clean_ndjson(payload, run_id="d21-from-d20", artifacts_dir=artifacts)
        assert cleaned.status is TaskStatus.SUCCESS
        result = sanitize_from_clean(
            run_id="d21-from-d20",
            artifacts_dir=artifacts,
            database=database,
            store=store,
        )
        assert result.status is TaskStatus.SUCCESS
        assert result.errors == ()
        directory = artifacts / "d21-from-d20" / "D21"
        rows = _rows(_sanitized_path(directory))
        assert rows[0]["source_record_id"] == "d20-pii"
        embedding = rows[0]["embedding_input"]
        assert isinstance(embedding, str)
        assert email not in embedding
        assert token not in embedding
        assert _PHONE not in embedding
        found = locate_derived(database, "d20-pii")
        assert len(found) == 1
        assert found[0].vector_id
        assert found[0].report_id is None
        chunk = store.get(found[0].chunk_uri).decode("utf-8")
        assert token not in chunk
        assert email not in chunk
        _assert_clean(directory, database, [email, token, _PHONE, "ghp_"])


def _row(source_record_id: str, abstract: str, **extra: object) -> dict[str, object]:
    source_id = extra.pop("source_id", "github")
    row: dict[str, object] = {
        "schema_version": "1.0",
        "source_id": source_id,
        "source_record_id": source_record_id,
        "canonical_url": f"https://example.test/{source_record_id}",
        "title": "Retrieval notes",
        "abstract_or_excerpt": abstract,
        "published_at": "2026-10-27T00:00:00Z",
        "collected_at": "2026-10-27T00:00:00Z",
        "language": "en",
        "license_status": "fulltext_allowed",
        "content_sha256": hashlib.sha256(source_record_id.encode("utf-8")).hexdigest(),
        "policy_snapshot_id": "fixture",
        "retention": "full_text",
    }
    row.update(extra)
    return row


def _token(secret: str) -> str:
    match = re.search(r"ghp_[A-Za-z0-9]{36}", secret)
    assert match is not None
    return match.group(0)


def _github_email() -> str:
    loaded: object = json.loads(GITHUB_FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError("github fixture")
    owner = loaded.get("owner")
    if not isinstance(owner, dict):
        raise AssertionError("github owner")
    email = owner.get("email")
    if not isinstance(email, str):
        raise AssertionError("github email")
    return email


def _needles(token: str, email: str) -> list[str]:
    return [
        token,
        "ghp_",
        email,
        _PHONE,
        _NANP,
        _MOBILE,
        _SSN,
        _CN_ID,
        _PASSPORT,
        _PASSWORD,
        _AUTHOR,
        "do-not-keep",
    ]


def _assert_clean(directory: Path, database: Path, needles: list[str]) -> None:
    blob = _tree_text(directory).encode("utf-8")
    if database.is_file():
        blob += database.read_bytes()
    for needle in needles:
        assert needle.encode("utf-8") not in blob


def _guard(database: Path, run_id: str) -> tuple[int, int]:
    conn = sqlite3.connect(database)
    try:
        row = conn.execute(
            "SELECT index_isolated, ops_notified FROM run_guard WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    isolated = row[0]
    notified = row[1]
    assert isinstance(isolated, int)
    assert isinstance(notified, int)
    return isolated, notified


def _assert_format(output_format: str, note: str, directory: Path) -> None:
    if importlib.util.find_spec("pyarrow") is None:
        assert output_format == "jsonl"
        assert note == PARQUET_NOTE
        assert (directory / "sanitized.jsonl").is_file()
        assert not (directory / "sanitized.parquet").exists()
        return
    assert output_format == "parquet"
    assert note == ""
    assert (directory / "sanitized.parquet").is_file()


def _sanitized_path(directory: Path) -> Path:
    parquet = directory / "sanitized.parquet"
    if parquet.is_file():
        return parquet
    return directory / "sanitized.jsonl"


def _rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".parquet":
        parquet: Any = importlib.import_module("pyarrow.parquet")
        table: Any = parquet.read_table(path)
        raw: object = table.to_pylist()
        if not isinstance(raw, list):
            raise AssertionError("parquet rows")
        return [_object_value(item) for item in raw]
    text = path.read_text(encoding="utf-8")
    if text == "":
        return []
    return [_object_text(line) for line in text.splitlines()]


def _object(path: Path) -> dict[str, Any]:
    return _object_text(path.read_text(encoding="utf-8"))


def _object_text(text: str) -> dict[str, Any]:
    return _object_value(json.loads(text))


def _object_value(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object")
    parsed: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise AssertionError("expected string keys")
        parsed[key] = item
    return parsed


def _tree_text(directory: Path) -> str:
    parts: list[str] = []
    if not directory.exists():
        return ""
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            parts.append(path.read_bytes().decode("utf-8", errors="ignore"))
    return "\n".join(parts)
