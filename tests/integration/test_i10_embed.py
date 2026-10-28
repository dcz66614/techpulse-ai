"""I10 embedding and index versioning. Local fixtures only. No live HTTP."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from alembic import command
from alembic.config import Config
from pytest import MonkeyPatch

from techpulse.contracts import TaskStatus
from techpulse.governance.pii import locate_derived, sanitize_records
from techpulse.index.embed import (
    REMOTE_BASE_URL,
    REMOTE_FLAG,
    REMOTE_KEY_ENV,
    VECTOR_DIMENSION,
    EmbedderDisabled,
    EmbedderError,
    HashedNgramEmbedder,
    OpenAICompatibleEmbedder,
    build_index,
    build_index_from_run,
    embedder_from_env,
    load_active_corpus,
)
from techpulse.workflows.activities import LocalObjectStore, ObjectStore

ROOT = Path(__file__).resolve().parents[2]
SECRET_FIXTURE = ROOT / "tests" / "fixtures" / "fake_secret_sample.txt"


class _CountingEmbedder:
    def __init__(self) -> None:
        self.calls = 0
        self.model_id = "hashed-char-ngram-tf"
        self.model_version = "1"
        self.dimension = VECTOR_DIMENSION
        self.calls_network = False
        self._inner = HashedNgramEmbedder()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return self._inner.embed(texts)


class _FailingEmbedder:
    def __init__(self) -> None:
        self.model_id = "failing-test"
        self.model_version = "0"
        self.dimension = VECTOR_DIMENSION
        self.calls_network = False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbedderError("vector model failed")


class _BoomStore:
    def put(self, key: str, body: bytes) -> str:
        raise OSError("full")

    def get(self, uri: str) -> bytes:
        raise OSError("full")


def test_hashed_ngram_embedder_is_deterministic() -> None:
    embedder = HashedNgramEmbedder()
    first = embedder.embed(["混合检索 hybrid", "other note"])
    second = embedder.embed(["混合检索 hybrid", "other note"])
    assert first == second
    assert len(first[0]) == VECTOR_DIMENSION
    assert first[0] != first[1]
    norm = math.sqrt(sum(value * value for value in first[0]))
    assert norm == pytest.approx(1.0)
    assert embedder.calls_network is False


def test_remote_embedder_stays_off_and_is_not_called(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv(REMOTE_FLAG, raising=False)
    assert isinstance(embedder_from_env(), HashedNgramEmbedder)

    env_dir_flag = "TECHPULSE_EMBED_REMOTE=1\n"
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text(env_dir_flag, encoding="utf-8")
        assert isinstance(embedder_from_env(), HashedNgramEmbedder)

    monkeypatch.setenv(REMOTE_FLAG, "1")
    monkeypatch.setenv(REMOTE_BASE_URL, "http://127.0.0.1:9/v1")
    monkeypatch.setenv(REMOTE_KEY_ENV, "TECHPULSE_TEST_EMBED_KEY")
    monkeypatch.delenv("TECHPULSE_TEST_EMBED_KEY", raising=False)
    configured = embedder_from_env()
    assert isinstance(configured, OpenAICompatibleEmbedder)
    assert configured.api_key_env == "TECHPULSE_TEST_EMBED_KEY"
    assert configured.calls_network is True

    disabled = OpenAICompatibleEmbedder(
        base_url="http://127.0.0.1:9/v1",
        api_key_env="TECHPULSE_TEST_EMBED_KEY",
        model="text-embedding-3-small",
        enabled=False,
        dimension=VECTOR_DIMENSION,
    )
    with pytest.raises(EmbedderDisabled, match="off"):
        disabled.embed(["ping"])


def test_hundred_rows_match_deduped_input_and_replay_is_idempotent() -> None:
    embedder = _CountingEmbedder()
    records: list[dict[str, object]] = []
    for index in range(100):
        language = "zh" if index % 2 else "en"
        records.append(_record(f"doc-{index:03d}", f"Agent retrieval note {index}", language))
    records.extend(records[:5])
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "i10.sqlite"
        store = LocalObjectStore(artifacts)
        first = build_index(
            records,
            run_id="i10-hundred",
            artifacts_dir=artifacts,
            database=database,
            store=store,
            embedder=embedder,
        )
        assert first.status == TaskStatus.SUCCESS
        assert first.degraded is False
        assert first.entry_count == 100
        assert first.metrics["input_rows"] == 105
        assert first.metrics["duplicates"] == 5
        assert first.metrics["deduped_rows"] == 100
        assert first.metrics["third_party_calls"] == 0
        assert first.metrics["model_id"] == "hashed-char-ngram-tf"
        assert _published_count(database) == 100
        assert _staging_count(database) == 0
        languages = _column(database, "language")
        assert "en" in languages
        assert "zh" in languages
        _assert_vector_columns(database)
        manifest = _read_json(artifacts / "i10-hundred" / "I10" / "index_manifest.json")
        assert manifest["model_id"] == "hashed-char-ngram-tf"
        assert manifest["model_version"] == "1"
        assert manifest["postgres_target"] == "pgvector"
        assert manifest["vector_store"] == "sqlite"
        assert manifest["explain_plan"] == "not_run_no_postgresql"
        assert manifest["entry_count"] == 100
        vector_ids = manifest["vector_ids"]
        assert isinstance(vector_ids, list)
        assert len(vector_ids) == 100
        corpus = load_active_corpus(database)
        assert corpus.vectors_available is True
        assert len(corpus.documents) == 100
        assert corpus.documents[0].embedding is not None
        assert len(corpus.documents[0].embedding) == VECTOR_DIMENSION
        second = build_index(
            records,
            run_id="i10-hundred",
            artifacts_dir=artifacts,
            database=database,
            store=store,
            embedder=embedder,
        )
        assert second.entry_count == 100
        assert second.index_version == first.index_version
        assert _published_count(database) == 100
        assert embedder.calls == 1
        vector_files = list((artifacts / "i10-hundred" / "I10" / "vectors").glob("*.json"))
        assert len(vector_files) == 100


def test_secrets_and_unclean_text_are_not_indexed() -> None:
    assert SECRET_FIXTURE.stat().st_size < 100_000
    token = _token(SECRET_FIXTURE.read_text(encoding="utf-8"))
    records: list[dict[str, object]] = [
        _record("good", "Indexed public note about retrieval", "en"),
        _record("secret", token, "en"),
        _record("mail", "reach leak@example.com please", "en"),
        _record("html", "<script>alert(1)</script> unclean", "en"),
        _record("control", "control\x01char", "en"),
        {
            "source_id": "fixture",
            "source_record_id": "blocked",
            "title": "blocked full text",
            "abstract_or_excerpt": "should stay out",
            "language": "en",
            "license_status": "blocked",
        },
    ]
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "i10.sqlite"
        result = build_index(
            records,
            run_id="i10-filter",
            artifacts_dir=artifacts,
            database=database,
            store=LocalObjectStore(artifacts),
            embedder=HashedNgramEmbedder(),
        )
        assert result.status == TaskStatus.SUCCESS
        assert result.entry_count == 1
        assert result.metrics["rejected_secret"] == 2
        assert result.metrics["rejected_unclean"] == 2
        assert result.metrics["rejected_license"] == 1
        bodies = "\n".join(_column(database, "body"))
        assert "Indexed public note about retrieval" in bodies
        assert token not in bodies
        assert "leak@example.com" not in bodies
        assert "<script>" not in bodies
        assert "blocked full text" not in bodies
        assert "\x01" not in bodies
        stored = json.dumps(_read_json(artifacts / "i10-filter" / "I10" / "index_manifest.json"))
        assert token not in stored


def test_rejected_batch_does_not_replace_the_published_index() -> None:
    token = _token(SECRET_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "i10.sqlite"
        store = LocalObjectStore(artifacts)
        first = build_index(
            [_record("good", "Keep this published row", "en")],
            run_id="i10-keep",
            artifacts_dir=artifacts,
            database=database,
            store=store,
            embedder=HashedNgramEmbedder(),
        )
        published = _pointer(database, "published")
        blob = _embedding(database, "good")
        second = build_index(
            [_record("secret", token, "en")],
            run_id="i10-secret-only",
            artifacts_dir=artifacts,
            database=database,
            store=store,
            embedder=HashedNgramEmbedder(),
        )
        assert first.entry_count == 1
        assert second.entry_count == 0
        assert second.index_version is None
        assert _pointer(database, "published") == published
        assert _embedding(database, "good") == blob
        assert token not in "\n".join(_column(database, "body"))


def test_embedder_failure_leaves_the_published_index() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "i10.sqlite"
        store = LocalObjectStore(artifacts)
        first = build_index(
            [_record("good", "Published vector row", "zh")],
            run_id="i10-published",
            artifacts_dir=artifacts,
            database=database,
            store=store,
            embedder=HashedNgramEmbedder(),
        )
        published = _pointer(database, "published")
        blob = _embedding(database, "good")
        failed = build_index(
            [_record("next", "Text fallback row", "zh")],
            run_id="i10-degraded",
            artifacts_dir=artifacts,
            database=database,
            store=store,
            embedder=_FailingEmbedder(),
        )
        assert failed.status == TaskStatus.SUCCESS
        assert failed.degraded is True
        assert failed.entry_count == 1
        assert _pointer(database, "published") == published
        assert _embedding(database, "good") == blob
        assert _pointer(database, "text") == failed.index_version
        corpus = load_active_corpus(database)
        assert corpus.vectors_available is False
        assert corpus.text_version == failed.index_version
        assert any(
            item.source_record_id == "next" and item.embedding is None for item in corpus.documents
        )
        assert first.index_version == published


def test_object_store_failure_is_retryable() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "i10.sqlite"
        store = LocalObjectStore(artifacts)
        first = build_index(
            [_record("good", "Stays published", "en")],
            run_id="i10-store-ok",
            artifacts_dir=artifacts,
            database=database,
            store=store,
            embedder=HashedNgramEmbedder(),
        )
        published = _pointer(database, "published")
        boom: ObjectStore = _BoomStore()
        failed = build_index(
            [_record("next", "Not stored", "en")],
            run_id="i10-store-fail",
            artifacts_dir=artifacts,
            database=database,
            store=boom,
            embedder=HashedNgramEmbedder(),
        )
        assert failed.status == TaskStatus.RETRYABLE_ERROR
        assert failed.errors == ("retryable_error: object store",)
        assert failed.degraded is False
        assert _pointer(database, "published") == published
        assert first.entry_count == 1
        assert _published_count(database) == 1


def test_d21_sanitized_rows_are_preferred(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("TECHPULSE_PII_HMAC_KEY", raising=False)
    record = _record("doc-1", "Sanitized retrieval note", "en")
    record["abstract_or_excerpt"] = "BM25 and dense vectors."
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "i10.sqlite"
        store = LocalObjectStore(artifacts)
        sanitized = sanitize_records(
            [record],
            run_id="i10-d21",
            artifacts_dir=artifacts,
            database=database,
            store=store,
        )
        assert sanitized.status == TaskStatus.SUCCESS
        _write_clean(
            artifacts / "i10-d21" / "D20",
            [_record("doc-1", "Leaky draft leak@example.com", "en")],
        )
        indexed = build_index_from_run(
            run_id="i10-d21",
            artifacts_dir=artifacts,
            database=database,
            store=store,
            embedder=HashedNgramEmbedder(),
        )
        assert indexed.status == TaskStatus.SUCCESS
        assert indexed.origin == "D21"
        assert indexed.entry_count == 1
        bodies = "\n".join(_column(database, "body"))
        assert "BM25 and dense vectors." in bodies
        assert "leak@example.com" not in bodies
        assert "Leaky draft" not in bodies
        found = locate_derived(database, "fixture")
        assert len(found) == 1
        assert "/I10/vectors/" in found[0].vector_uri
        payload = store.get(found[0].vector_uri).decode("utf-8")
        assert found[0].vector_id in payload
        assert "hashed-char-ngram-tf" in payload


def test_d20_is_used_when_sanitized_output_is_absent() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "i10.sqlite"
        _write_clean(
            artifacts / "i10-d20" / "D20",
            [_record("doc-9", "Clean excerpt about agents", "en")],
        )
        indexed = build_index_from_run(
            run_id="i10-d20",
            artifacts_dir=artifacts,
            database=database,
            store=LocalObjectStore(artifacts),
            embedder=HashedNgramEmbedder(),
        )
        assert indexed.origin == "D20"
        assert indexed.entry_count == 1
        assert "Clean excerpt about agents" in "\n".join(_column(database, "body"))


def test_isolated_run_is_not_indexed() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts"
        database = root / "i10.sqlite"
        directory = artifacts / "i10-iso" / "D21"
        rows = [_record("doc-1", "Must not be indexed", "en")]
        digest = _write_jsonl(directory / "sanitized.jsonl", rows)
        _write_json(
            directory / "manifest.json",
            {
                "isolated": True,
                "sanitized_name": "sanitized.jsonl",
                "sanitized_sha256": digest,
                "status": "success",
            },
        )
        _write_clean(artifacts / "i10-iso" / "D20", rows)
        indexed = build_index_from_run(
            run_id="i10-iso",
            artifacts_dir=artifacts,
            database=database,
            store=LocalObjectStore(artifacts),
            embedder=HashedNgramEmbedder(),
        )
        assert indexed.status == TaskStatus.FATAL_ERROR
        assert indexed.errors == ("fatal_error: index isolated",)
        assert indexed.entry_count == 0
        assert _pointer(database, "published") is None


def test_sqlite_migration_creates_search_tables_and_postgres_ddl_names_pgvector() -> None:
    postgres = (ROOT / "migrations" / "0002_search_indexes.sql").read_text(encoding="utf-8")
    sqlite_sql = (ROOT / "migrations" / "0002_search_indexes.sqlite.sql").read_text(
        encoding="utf-8"
    )
    assert "pgvector" in postgres.lower()
    assert f"vector({VECTOR_DIMENSION})" in postgres
    assert "hnsw" in postgres.lower()
    assert "USING hnsw" not in sqlite_sql
    with TemporaryDirectory() as tmp:
        database = Path(tmp) / "i10.sqlite"
        url = "sqlite:///" + database.resolve().as_posix()
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        names = _table_names(database)
        assert {
            "index_versions",
            "chunk_vectors",
            "chunk_vectors_staging",
            "index_pointer",
        } <= names
        command.downgrade(cfg, "base")
        gone = _table_names(database)
        assert "index_versions" not in gone
        assert "chunk_vectors" not in gone
        command.upgrade(cfg, "head")
        assert "index_versions" in _table_names(database)


def _record(source_record_id: str, title: str, language: str) -> dict[str, object]:
    return {
        "source_id": "fixture",
        "source_record_id": source_record_id,
        "title": title,
        "abstract_or_excerpt": "",
        "language": language,
        "license_status": "fulltext_allowed",
    }


def _token(text: str) -> str:
    marker = "ghp_"
    start = text.find(marker)
    assert start >= 0
    token = text[start:].strip().split()[0]
    assert token.startswith(marker)
    return token


def _published_count(database: Path) -> int:
    conn = sqlite3.connect(database)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM chunk_vectors
            WHERE index_version = (
                SELECT index_version FROM index_pointer WHERE name = 'published'
            )
            """
        ).fetchone()
    finally:
        conn.close()
    if row is None or not isinstance(row[0], int):
        return 0
    return row[0]


def _staging_count(database: Path) -> int:
    conn = sqlite3.connect(database)
    try:
        row = conn.execute("SELECT COUNT(*) FROM chunk_vectors_staging").fetchone()
    finally:
        conn.close()
    if row is None or not isinstance(row[0], int):
        return 0
    return row[0]


def _column(database: Path, name: str) -> list[str]:
    if name == "language":
        sql = "SELECT language FROM chunk_vectors"
    elif name == "body":
        sql = "SELECT body FROM chunk_vectors"
    else:
        raise AssertionError(name)
    conn = sqlite3.connect(database)
    try:
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    values: list[str] = []
    for row in rows:
        if isinstance(row[0], str):
            values.append(row[0])
    return values


def _assert_vector_columns(database: Path) -> None:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT model_id, model_version, content_sha256, vector_sha256, vector_id, embedding
            FROM chunk_vectors
            """
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 100
    for row in rows:
        assert row["model_id"] == "hashed-char-ngram-tf"
        assert row["model_version"] == "1"
        assert isinstance(row["content_sha256"], str) and len(row["content_sha256"]) == 64
        assert isinstance(row["vector_sha256"], str) and len(row["vector_sha256"]) == 64
        assert isinstance(row["vector_id"], str) and len(row["vector_id"]) == 64
        assert isinstance(row["embedding"], bytes) and len(row["embedding"]) == VECTOR_DIMENSION * 4


def _pointer(database: Path, name: str) -> str | None:
    if not database.is_file():
        return None
    conn = sqlite3.connect(database)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'index_pointer'"
        ).fetchone()
        if found is None:
            return None
        row = conn.execute(
            "SELECT index_version FROM index_pointer WHERE name = ?",
            (name,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not isinstance(row[0], str):
        return None
    return row[0]


def _embedding(database: Path, source_record_id: str) -> bytes | None:
    conn = sqlite3.connect(database)
    try:
        row = conn.execute(
            """
            SELECT embedding FROM chunk_vectors
            WHERE source_record_id = ?
              AND index_version = (
                  SELECT index_version FROM index_pointer WHERE name = 'published'
              )
            """,
            (source_record_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not isinstance(row[0], bytes):
        return None
    return row[0]


def _table_names(database: Path) -> set[str]:
    conn = sqlite3.connect(database)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    names: set[str] = set()
    for row in rows:
        if isinstance(row[0], str):
            names.add(row[0])
    return names


def _write_clean(directory: Path, rows: Sequence[Mapping[str, object]]) -> None:
    digest = _write_jsonl(directory / "clean.jsonl", rows)
    _write_json(
        directory / "manifest.json",
        {
            "clean_name": "clean.jsonl",
            "clean_sha256": digest,
            "status": "success",
            "ticket": "D20",
        },
    )


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, object]]) -> str:
    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    data = body.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    parsed: dict[str, object] = {}
    for key, value in loaded.items():
        assert isinstance(key, str)
        parsed[key] = value
    return parsed
