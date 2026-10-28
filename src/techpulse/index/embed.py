"""I10 embedding and versioned indexes.

The default embedder hashes character n-grams into a fixed TF vector. It
downloads nothing and is deterministic. An OpenAI-compatible HTTP embedder
is configured from the process environment and stays off unless
``TECHPULSE_EMBED_REMOTE`` is set. This module does not open a ``.env`` file.

Vectors and the index-version manifest are stored in SQLite. PostgreSQL
remains the pgvector target in ``migrations/0002_search_indexes.sql``. This
process does not connect to Postgres. Chunk bytes go through ``ObjectStore``.
``LocalObjectStore`` writes under the artifacts root. There is no S3 client.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import math
import os
import re
import sqlite3
import struct
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from techpulse.contracts import LicenseStatus, TaskStatus
from techpulse.governance.pii import contains_pii
from techpulse.normalize.clean import normalize_language
from techpulse.workflows.activities import ObjectStore

VECTOR_DIMENSION = 256
NGRAM_SIZE = 3
REMOTE_FLAG = "TECHPULSE_EMBED_REMOTE"
REMOTE_BASE_URL = "TECHPULSE_EMBED_BASE_URL"
REMOTE_KEY_ENV = "TECHPULSE_EMBED_API_KEY_ENV"
REMOTE_MODEL = "TECHPULSE_EMBED_MODEL"
REMOTE_DIMENSION = "TECHPULSE_EMBED_DIMENSION"
_DEFAULT_KEY_ENV = "OPENAI_API_KEY"
_DEFAULT_REMOTE_MODEL = "text-embedding-3-small"
_DEFAULT_REMOTE_DIMENSION = 1536
_ON = frozenset({"1", "true", "yes", "on"})
_ALLOWED_LICENSE = frozenset(
    {
        LicenseStatus.METADATA_ONLY.value,
        LicenseStatus.REUSABLE_EXCERPT.value,
        LicenseStatus.FULLTEXT_ALLOWED.value,
    }
)
_TAG = re.compile(r"<[^>]+>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DDL_NAME = "0002_search_indexes.sqlite.sql"


class EmbedderDisabled(RuntimeError):
    """The remote embedder is off or its env configuration is incomplete."""


class EmbedderError(RuntimeError):
    """The embedder failed. The published index must stay as it was."""


class Embedder(Protocol):
    """Replaceable embedding model. Tests use the local baseline."""

    model_id: str
    model_version: str
    dimension: int
    calls_network: bool

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashedNgramEmbedder:
    """L2-normalised hashed character n-gram term frequencies."""

    def __init__(self) -> None:
        self.model_id = "hashed-char-ngram-tf"
        self.model_version = "1"
        self.dimension = VECTOR_DIMENSION
        self.calls_network = False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_embed_hashed(text) for text in texts]


class OpenAICompatibleEmbedder:
    """POST ``{base_url}/embeddings``. ``embed`` runs only when enabled.

    The API key is read from the environment variable named by
    ``api_key_env`` at call time. The key is not stored on this object.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key_env: str,
        model: str,
        enabled: bool,
        dimension: int,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model_id = model
        self.model_version = "openai-compatible"
        self.dimension = dimension
        self.calls_network = True
        self.api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")
        self._enabled = enabled
        self._timeout_s = timeout_s
        self._transport = transport

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self._enabled:
            raise EmbedderDisabled("remote embedder is off")
        if self._base_url == "":
            raise EmbedderDisabled("remote embedder base url is empty")
        key = os.environ.get(self.api_key_env, "")
        if key == "":
            raise EmbedderDisabled("remote embedder key is empty")
        url = _embeddings_url(self._base_url)
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        body = {"model": self.model_id, "input": list(texts)}
        try:
            with httpx.Client(
                timeout=self._timeout_s,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.post(url, headers=headers, json=body)
                response.raise_for_status()
                payload: object = response.json()
        except httpx.HTTPError as exc:
            raise EmbedderError("remote embedder request failed") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EmbedderError("remote embedder payload") from exc
        return _embeddings_from_payload(payload, len(texts), self.dimension)


def embedder_from_env() -> Embedder:
    """Local baseline, unless ``TECHPULSE_EMBED_REMOTE`` is explicitly on."""
    flag = os.environ.get(REMOTE_FLAG, "")
    if flag.strip().lower() not in _ON:
        return HashedNgramEmbedder()
    raw_dimension = os.environ.get(REMOTE_DIMENSION, str(_DEFAULT_REMOTE_DIMENSION))
    try:
        dimension = int(raw_dimension)
    except ValueError as exc:
        raise EmbedderDisabled("remote embedder dimension is invalid") from exc
    if dimension <= 0:
        raise EmbedderDisabled("remote embedder dimension is invalid")
    key_env = os.environ.get(REMOTE_KEY_ENV, _DEFAULT_KEY_ENV).strip()
    if key_env == "":
        raise EmbedderDisabled("remote embedder key env name is empty")
    model = os.environ.get(REMOTE_MODEL, _DEFAULT_REMOTE_MODEL).strip()
    if model == "":
        model = _DEFAULT_REMOTE_MODEL
    return OpenAICompatibleEmbedder(
        base_url=os.environ.get(REMOTE_BASE_URL, "").strip(),
        api_key_env=key_env,
        model=model,
        enabled=True,
        dimension=dimension,
    )


@dataclass(frozen=True)
class IndexBuildResult:
    status: TaskStatus
    run_id: str
    index_version: str | None
    entry_count: int
    degraded: bool
    origin: str
    errors: tuple[str, ...]
    metrics: dict[str, int | float | str | bool | None]


@dataclass(frozen=True)
class CorpusDocument:
    vector_id: str
    chunk_id: str
    source_id: str
    source_record_id: str
    language: str
    license_status: str
    body: str
    content_sha256: str
    embedding: tuple[float, ...] | None


@dataclass(frozen=True)
class ActiveCorpus:
    text_version: str | None
    published_version: str | None
    model_id: str | None
    model_version: str | None
    dimension: int
    vectors_available: bool
    documents: tuple[CorpusDocument, ...]


@dataclass(frozen=True)
class _Accepted:
    chunk_id: str
    vector_id: str
    source_id: str
    source_record_id: str
    language: str
    license_status: str
    body: str
    content_sha256: str


@dataclass(frozen=True)
class _Filter:
    accepted: tuple[_Accepted, ...]
    input_rows: int
    duplicates: int
    rejected_secret: int
    rejected_unclean: int
    rejected_license: int
    rejected_empty: int


class _InputError(Exception):
    def __init__(self, message: str, *, status: TaskStatus = TaskStatus.FATAL_ERROR) -> None:
        super().__init__(message)
        self.status = status


def build_index_from_run(
    *,
    run_id: str,
    artifacts_dir: Path,
    database: Path,
    store: ObjectStore,
    embedder: Embedder | None = None,
) -> IndexBuildResult:
    """Index D21 sanitized chunks. Absent or unsuccessful D21 output blocks the index."""
    checked = _run_id(run_id)
    if _is_isolated(artifacts_dir, checked, database):
        return _emit(
            directory=artifacts_dir / checked / "I10",
            run_id=checked,
            status=TaskStatus.FATAL_ERROR,
            index_version=None,
            entry_count=0,
            degraded=False,
            origin="isolated",
            errors=("fatal_error: index isolated",),
            extra=_filter_metrics(_Filter((), 0, 0, 0, 0, 0, 0), None, 0.0, 0),
        )
    try:
        records, origin = _load_run_records(artifacts_dir, checked)
    except _InputError as exc:
        return _emit(
            directory=artifacts_dir / checked / "I10",
            run_id=checked,
            status=exc.status,
            index_version=None,
            entry_count=0,
            degraded=False,
            origin="missing",
            errors=(str(exc),),
            extra=_filter_metrics(_Filter((), 0, 0, 0, 0, 0, 0), None, 0.0, 0),
        )
    return build_index(
        records,
        run_id=checked,
        artifacts_dir=artifacts_dir,
        database=database,
        store=store,
        embedder=embedder,
        origin=origin,
    )


def build_index(
    records: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    artifacts_dir: Path,
    database: Path,
    store: ObjectStore,
    embedder: Embedder | None = None,
    origin: str = "records",
) -> IndexBuildResult:
    """Embed accepted chunks, stage them, then publish the index pointer.

    A repeat of the same run, model, and corpus does not insert again.
    An embedder failure writes a text-only version and leaves the published
    pointer where it was. Secrets and uncleaned text are not inserted.
    """
    checked = _run_id(run_id)
    directory = artifacts_dir / checked / "I10"
    filtered = _filter_records(records)
    if not filtered.accepted:
        return _emit(
            directory=directory,
            run_id=checked,
            status=TaskStatus.SUCCESS,
            index_version=None,
            entry_count=0,
            degraded=False,
            origin=origin,
            errors=(),
            extra=_filter_metrics(filtered, None, 0.0, 0),
            note="no indexable rows",
        )
    try:
        chosen = embedder_from_env() if embedder is None else embedder
    except EmbedderDisabled as exc:
        return _emit(
            directory=directory,
            run_id=checked,
            status=TaskStatus.FATAL_ERROR,
            index_version=None,
            entry_count=0,
            degraded=False,
            origin=origin,
            errors=(str(exc),),
            extra=_filter_metrics(filtered, None, 0.0, 0),
        )
    corpus_sha = _corpus_sha256(filtered.accepted)
    version = _version_id(checked, chosen.model_id, chosen.model_version, corpus_sha)
    try:
        conn = _connect(database)
    except sqlite3.Error:
        return _db_error(directory, checked, origin, filtered, chosen)
    try:
        existing = _find_built(conn, checked, chosen.model_id, chosen.model_version, corpus_sha)
        conn.commit()
    except sqlite3.Error:
        conn.close()
        return _db_error(directory, checked, origin, filtered, chosen)
    if existing is not None:
        conn.close()
        return _emit(
            directory=directory,
            run_id=checked,
            status=TaskStatus.SUCCESS,
            index_version=existing[0],
            entry_count=existing[1],
            degraded=existing[2],
            origin=origin,
            errors=(),
            extra=_filter_metrics(filtered, chosen, 0.0, 0),
            corpus_sha256=corpus_sha,
            vector_ids=tuple(item.vector_id for item in filtered.accepted),
            index_manifest=_as_dict(json.loads(existing[3])),
        )
    started = time.perf_counter()
    try:
        vectors = chosen.embed(tuple(item.body for item in filtered.accepted))
    except EmbedderDisabled as exc:
        conn.close()
        return _emit(
            directory=directory,
            run_id=checked,
            status=TaskStatus.FATAL_ERROR,
            index_version=None,
            entry_count=0,
            degraded=False,
            origin=origin,
            errors=(str(exc),),
            extra=_filter_metrics(filtered, chosen, 0.0, 0),
        )
    except EmbedderError:
        embed_ms = (time.perf_counter() - started) * 1000.0
        calls = 1 if chosen.calls_network else 0
        try:
            index_manifest = _commit_degraded(
                conn,
                version,
                checked,
                chosen,
                filtered.accepted,
                corpus_sha,
                "embedding failed",
            )
        except sqlite3.Error:
            conn.close()
            return _db_error(directory, checked, origin, filtered, chosen, embed_ms, calls)
        conn.close()
        return _emit(
            directory=directory,
            run_id=checked,
            status=TaskStatus.SUCCESS,
            index_version=version,
            entry_count=len(filtered.accepted),
            degraded=True,
            origin=origin,
            errors=(),
            extra=_filter_metrics(filtered, chosen, embed_ms, calls),
            corpus_sha256=corpus_sha,
            vector_ids=tuple(item.vector_id for item in filtered.accepted),
            degraded_reason="embedding failed",
            index_manifest=index_manifest,
        )
    embed_ms = (time.perf_counter() - started) * 1000.0
    calls = 1 if chosen.calls_network else 0
    if len(vectors) != len(filtered.accepted) or any(
        len(vector) != chosen.dimension for vector in vectors
    ):
        try:
            index_manifest = _commit_degraded(
                conn,
                version,
                checked,
                chosen,
                filtered.accepted,
                corpus_sha,
                "embedding shape mismatch",
            )
        except sqlite3.Error:
            conn.close()
            return _db_error(directory, checked, origin, filtered, chosen, embed_ms, calls)
        conn.close()
        return _emit(
            directory=directory,
            run_id=checked,
            status=TaskStatus.SUCCESS,
            index_version=version,
            entry_count=len(filtered.accepted),
            degraded=True,
            origin=origin,
            errors=(),
            extra=_filter_metrics(filtered, chosen, embed_ms, calls),
            corpus_sha256=corpus_sha,
            vector_ids=tuple(item.vector_id for item in filtered.accepted),
            degraded_reason="embedding shape mismatch",
            index_manifest=index_manifest,
        )
    packed = [_pack(vector) for vector in vectors]
    uris: list[str] = []
    try:
        for item, blob in zip(filtered.accepted, packed, strict=True):
            uris.append(_put_vector(store, checked, chosen, item, blob))
    except OSError:
        conn.close()
        return _emit(
            directory=directory,
            run_id=checked,
            status=TaskStatus.RETRYABLE_ERROR,
            index_version=None,
            entry_count=0,
            degraded=False,
            origin=origin,
            errors=("retryable_error: object store",),
            extra=_filter_metrics(filtered, chosen, embed_ms, calls),
        )
    try:
        index_manifest = _commit_published(
            conn,
            version,
            checked,
            chosen,
            filtered.accepted,
            packed,
            uris,
            corpus_sha,
        )
    except sqlite3.Error:
        conn.close()
        return _db_error(directory, checked, origin, filtered, chosen, embed_ms, calls)
    conn.close()
    return _emit(
        directory=directory,
        run_id=checked,
        status=TaskStatus.SUCCESS,
        index_version=version,
        entry_count=len(filtered.accepted),
        degraded=False,
        origin=origin,
        errors=(),
        extra=_filter_metrics(filtered, chosen, embed_ms, calls),
        corpus_sha256=corpus_sha,
        vector_ids=tuple(item.vector_id for item in filtered.accepted),
        index_manifest=index_manifest,
    )


def load_active_corpus(database: Path) -> ActiveCorpus:
    """Return the text-pointer corpus. Dense search is allowed only when it is published."""
    empty = ActiveCorpus(None, None, None, None, 0, False, ())
    if not database.is_file():
        return empty
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "index_pointer"):
            return empty
        text_version = _pointer(conn, "text")
        published_version = _pointer(conn, "published")
        if text_version is None:
            return ActiveCorpus(None, published_version, None, None, 0, False, ())
        meta = conn.execute(
            """
            SELECT model_id, model_version, dimension
            FROM index_versions WHERE index_version = ?
            """,
            (text_version,),
        ).fetchone()
        if meta is None:
            return empty
        model_id = _cell_str(meta, "model_id")
        model_version = _cell_str(meta, "model_version")
        dimension = _cell_int(meta, "dimension")
        rows = conn.execute(
            """
            SELECT vector_id, chunk_id, source_id, source_record_id, language,
                   license_status, body, content_sha256, embedding
            FROM chunk_vectors
            WHERE index_version = ?
            ORDER BY source_record_id, chunk_id
            """,
            (text_version,),
        ).fetchall()
    except sqlite3.Error:
        return empty
    finally:
        conn.close()
    documents: list[CorpusDocument] = []
    for row in rows:
        documents.append(_document(row, dimension))
    complete = all(item.embedding is not None for item in documents)
    vectors_available = (
        published_version is not None
        and published_version == text_version
        and len(documents) > 0
        and complete
    )
    return ActiveCorpus(
        text_version=text_version,
        published_version=published_version,
        model_id=model_id,
        model_version=model_version,
        dimension=dimension,
        vectors_available=vectors_available,
        documents=tuple(documents),
    )


def _embed_hashed(text: str) -> list[float]:
    folded = text.casefold()
    counts = [0.0] * VECTOR_DIMENSION
    if len(folded) < NGRAM_SIZE:
        grams: tuple[str, ...] = (folded,) if folded else (" ",)
    else:
        grams = tuple(
            folded[index : index + NGRAM_SIZE] for index in range(len(folded) - NGRAM_SIZE + 1)
        )
    for gram in grams:
        digest = hashlib.sha256(gram.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "little") % VECTOR_DIMENSION
        counts[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in counts))
    if norm == 0.0:
        return counts
    return [value / norm for value in counts]


def _embeddings_url(base_url: str) -> str:
    if base_url.endswith("/embeddings"):
        return base_url
    return base_url + "/embeddings"


def _embeddings_from_payload(payload: object, expected: int, dimension: int) -> list[list[float]]:
    if not isinstance(payload, dict):
        raise EmbedderError("remote embedder payload")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected:
        raise EmbedderError("remote embedder payload")
    ordered: list[list[float]] = []
    by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise EmbedderError("remote embedder payload")
        index = item.get("index")
        raw = item.get("embedding")
        if isinstance(index, bool) or not isinstance(index, int):
            raise EmbedderError("remote embedder payload")
        if index < 0 or index >= expected or not isinstance(raw, list):
            raise EmbedderError("remote embedder payload")
        vector: list[float] = []
        for value in raw:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise EmbedderError("remote embedder payload")
            vector.append(float(value))
        if len(vector) != dimension:
            raise EmbedderError("remote embedder dimension")
        by_index[index] = vector
    if len(by_index) != expected:
        raise EmbedderError("remote embedder payload")
    for index in range(expected):
        found = by_index.get(index)
        if found is None:
            raise EmbedderError("remote embedder payload")
        ordered.append(found)
    return ordered


def _filter_records(records: Sequence[Mapping[str, object]]) -> _Filter:
    seen: dict[str, _Accepted] = {}
    duplicates = 0
    rejected_secret = 0
    rejected_unclean = 0
    rejected_license = 0
    rejected_empty = 0
    for record in records:
        item, reason = _accept_one(record)
        if reason == "secret":
            rejected_secret += 1
            continue
        if reason == "unclean":
            rejected_unclean += 1
            continue
        if reason == "license":
            rejected_license += 1
            continue
        if reason == "empty":
            rejected_empty += 1
            continue
        if item is None:
            rejected_unclean += 1
            continue
        if item.chunk_id in seen:
            duplicates += 1
            continue
        seen[item.chunk_id] = item
    ordered = tuple(seen[key] for key in sorted(seen))
    return _Filter(
        accepted=ordered,
        input_rows=len(records),
        duplicates=duplicates,
        rejected_secret=rejected_secret,
        rejected_unclean=rejected_unclean,
        rejected_license=rejected_license,
        rejected_empty=rejected_empty,
    )


def _accept_one(record: Mapping[str, object]) -> tuple[_Accepted | None, str | None]:
    license_status = record.get("license_status")
    if not isinstance(license_status, str) or license_status not in _ALLOWED_LICENSE:
        return None, "license"
    source_id = _identifier(record.get("source_id"))
    source_record_id = _identifier(record.get("source_record_id"))
    if source_id is None or source_record_id is None:
        return None, "unclean"
    body = _body(record)
    if body.strip() == "":
        return None, "empty"
    if _TAG.search(body) is not None or _CONTROL.search(body) is not None:
        return None, "unclean"
    if contains_pii(body) or contains_pii(source_id) or contains_pii(source_record_id):
        return None, "secret"
    chunk_id, vector_id = _identities(source_id, source_record_id, body, record)
    content_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    item = _Accepted(
        chunk_id=chunk_id,
        vector_id=vector_id,
        source_id=source_id,
        source_record_id=source_record_id,
        language=normalize_language(record.get("language")),
        license_status=license_status,
        body=body,
        content_sha256=content_sha256,
    )
    return item, None


def _body(record: Mapping[str, object]) -> str:
    embedding = record.get("embedding_input")
    if isinstance(embedding, str) and embedding.strip() != "":
        return embedding
    title = record.get("title")
    excerpt = record.get("abstract_or_excerpt")
    parts: list[str] = []
    if isinstance(title, str) and title.strip() != "":
        parts.append(title.strip())
    if isinstance(excerpt, str) and excerpt.strip() != "":
        parts.append(excerpt.strip())
    return "\n".join(parts)


def _identities(
    source_id: str,
    source_record_id: str,
    body: str,
    record: Mapping[str, object],
) -> tuple[str, str]:
    chunk = record.get("chunk_id")
    vector = record.get("vector_id")
    if (
        isinstance(chunk, str)
        and _HEX64.fullmatch(chunk)
        and isinstance(vector, str)
        and _HEX64.fullmatch(vector)
    ):
        return chunk, vector
    chunk_id = hashlib.sha256(
        "\n".join(("chunk", source_id, source_record_id, body)).encode("utf-8")
    ).hexdigest()
    vector_id = hashlib.sha256("\n".join(("vector", chunk_id)).encode("utf-8")).hexdigest()
    return chunk_id, vector_id


def _identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text == "" or any(char in text for char in "\r\n\x00"):
        return None
    return text


def _corpus_sha256(rows: Sequence[_Accepted]) -> str:
    lines = [f"{item.chunk_id}\t{item.content_sha256}" for item in rows]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _version_id(run_id: str, model_id: str, model_version: str, corpus_sha256: str) -> str:
    raw = "\n".join((run_id, model_id, model_version, corpus_sha256))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack("<" + f"{len(vector)}f", *[float(value) for value in vector])


def _unpack(blob: bytes, dimension: int) -> tuple[float, ...] | None:
    if len(blob) != dimension * 4:
        return None
    values = struct.unpack("<" + f"{dimension}f", blob)
    return values


def _put_vector(
    store: ObjectStore,
    run_id: str,
    embedder: Embedder,
    item: _Accepted,
    blob: bytes,
) -> str:
    payload = {
        "chunk_id": item.chunk_id,
        "content_sha256": item.content_sha256,
        "dimension": embedder.dimension,
        "embedding_f32_b64": base64.b64encode(blob).decode("ascii"),
        "model_id": embedder.model_id,
        "model_version": embedder.model_version,
        "source_id": item.source_id,
        "source_record_id": item.source_record_id,
        "vector_id": item.vector_id,
        "vector_sha256": hashlib.sha256(blob).hexdigest(),
    }
    key = f"{run_id}/I10/vectors/{item.vector_id}.json"
    return store.put(key, _pretty(payload))


def _commit_published(
    conn: sqlite3.Connection,
    version: str,
    run_id: str,
    embedder: Embedder,
    rows: Sequence[_Accepted],
    blobs: Sequence[bytes],
    uris: Sequence[str],
    corpus_sha256: str,
) -> dict[str, object]:
    created = _utc_now()
    manifest = _manifest(
        run_id=run_id,
        version=version,
        embedder=embedder,
        rows=rows,
        corpus_sha256=corpus_sha256,
        degraded=False,
        status="staging",
        degraded_reason=None,
    )
    params = [
        _vector_params(version, embedder, item, blob)
        for item, blob in zip(rows, blobs, strict=True)
    ]
    with conn:
        _clear_staging(conn, version)
        conn.executemany(_STAGING_INSERT, params)
        conn.execute(
            _VERSION_INSERT,
            (
                version,
                run_id,
                embedder.model_id,
                embedder.model_version,
                embedder.dimension,
                corpus_sha256,
                "staging",
                len(rows),
                0,
                _pretty(manifest).decode("utf-8"),
                created,
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO chunk_vectors (
                index_version, vector_id, chunk_id, source_id, source_record_id,
                language, license_status, model_id, model_version, content_sha256,
                vector_sha256, embedding, body
            )
            SELECT index_version, vector_id, chunk_id, source_id, source_record_id,
                   language, license_status, model_id, model_version, content_sha256,
                   vector_sha256, embedding, body
            FROM chunk_vectors_staging
            WHERE index_version = ?
            """,
            (version,),
        )
        count = _count_version(conn, version)
        if count != len(rows):
            raise sqlite3.Error("entry count mismatch")
        published_at = _utc_now()
        manifest["status"] = "published"
        manifest["published_at"] = published_at
        conn.execute(
            """
            UPDATE index_versions
            SET status = 'published', published_at = ?, manifest_json = ?
            WHERE index_version = ?
            """,
            (published_at, _pretty(manifest).decode("utf-8"), version),
        )
        for name in ("published", "text"):
            conn.execute(
                """
                INSERT INTO index_pointer (name, index_version) VALUES (?, ?)
                ON CONFLICT (name) DO UPDATE SET index_version = excluded.index_version
                """,
                (name, version),
            )
        conn.execute(
            """
            UPDATE index_versions SET status = 'retired'
            WHERE status = 'published' AND index_version != ?
            """,
            (version,),
        )
        conn.execute("DELETE FROM chunk_vectors_staging WHERE index_version = ?", (version,))
        if _table_exists(conn, "deletion_map"):
            for item, uri in zip(rows, uris, strict=True):
                conn.execute(
                    "UPDATE deletion_map SET vector_uri = ? WHERE chunk_id = ?",
                    (uri, item.chunk_id),
                )
    return manifest


def _commit_degraded(
    conn: sqlite3.Connection,
    version: str,
    run_id: str,
    embedder: Embedder,
    rows: Sequence[_Accepted],
    corpus_sha256: str,
    reason: str,
) -> dict[str, object]:
    created = _utc_now()
    manifest = _manifest(
        run_id=run_id,
        version=version,
        embedder=embedder,
        rows=rows,
        corpus_sha256=corpus_sha256,
        degraded=True,
        status="degraded",
        degraded_reason=reason,
    )
    empty = hashlib.sha256(b"").hexdigest()
    params = [
        (
            version,
            item.vector_id,
            item.chunk_id,
            item.source_id,
            item.source_record_id,
            item.language,
            item.license_status,
            embedder.model_id,
            embedder.model_version,
            item.content_sha256,
            empty,
            None,
            item.body,
        )
        for item in rows
    ]
    with conn:
        _clear_staging(conn, version)
        conn.execute(
            _VERSION_INSERT,
            (
                version,
                run_id,
                embedder.model_id,
                embedder.model_version,
                embedder.dimension,
                corpus_sha256,
                "degraded",
                len(rows),
                1,
                _pretty(manifest).decode("utf-8"),
                created,
                None,
            ),
        )
        conn.executemany(_LIVE_INSERT, params)
        count = _count_version(conn, version)
        if count != len(rows):
            raise sqlite3.Error("entry count mismatch")
        conn.execute(
            """
            INSERT INTO index_pointer (name, index_version) VALUES ('text', ?)
            ON CONFLICT (name) DO UPDATE SET index_version = excluded.index_version
            """,
            (version,),
        )
    return manifest


def _clear_staging(conn: sqlite3.Connection, version: str) -> None:
    conn.execute("DELETE FROM chunk_vectors_staging WHERE index_version = ?", (version,))
    conn.execute(
        "DELETE FROM chunk_vectors WHERE index_version = ? AND index_version IN "
        "(SELECT index_version FROM index_versions WHERE index_version = ? AND status = 'staging')",
        (version, version),
    )
    conn.execute(
        "DELETE FROM index_versions WHERE index_version = ? AND status = 'staging'",
        (version,),
    )


def _find_built(
    conn: sqlite3.Connection,
    run_id: str,
    model_id: str,
    model_version: str,
    corpus_sha256: str,
) -> tuple[str, int, bool, str] | None:
    row = conn.execute(
        """
        SELECT index_version, entry_count, degraded, manifest_json
        FROM index_versions
        WHERE run_id = ? AND model_id = ? AND model_version = ?
          AND corpus_sha256 = ? AND status IN ('published', 'degraded')
        """,
        (run_id, model_id, model_version, corpus_sha256),
    ).fetchone()
    if row is None:
        return None
    return (
        _cell_str(row, "index_version"),
        _cell_int(row, "entry_count"),
        _cell_int(row, "degraded") == 1,
        _cell_str(row, "manifest_json"),
    )


def _count_version(conn: sqlite3.Connection, version: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM chunk_vectors WHERE index_version = ?",
        (version,),
    ).fetchone()
    if row is None:
        return 0
    return _cell_int(row, "n")


def _pointer(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT index_version FROM index_pointer WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return _cell_str(row, "index_version")


def _document(row: sqlite3.Row, dimension: int) -> CorpusDocument:
    blob = row["embedding"]
    embedding: tuple[float, ...] | None = None
    if isinstance(blob, bytes):
        embedding = _unpack(blob, dimension)
    return CorpusDocument(
        vector_id=_cell_str(row, "vector_id"),
        chunk_id=_cell_str(row, "chunk_id"),
        source_id=_cell_str(row, "source_id"),
        source_record_id=_cell_str(row, "source_record_id"),
        language=_cell_str(row, "language"),
        license_status=_cell_str(row, "license_status"),
        body=_cell_str(row, "body"),
        content_sha256=_cell_str(row, "content_sha256"),
        embedding=embedding,
    )


def _manifest(
    *,
    run_id: str,
    version: str,
    embedder: Embedder,
    rows: Sequence[_Accepted],
    corpus_sha256: str,
    degraded: bool,
    status: str,
    degraded_reason: str | None,
) -> dict[str, object]:
    return {
        "candidates": [
            {
                "languages": ["zh", "en"],
                "model_id": "hashed-char-ngram-tf",
                "reason": "deterministic character n-gram hashing; no downloaded weights",
                "selected": isinstance(embedder, HashedNgramEmbedder),
            },
            {
                "model_id": "openai-compatible",
                "reason": "remote endpoint; off unless TECHPULSE_EMBED_REMOTE is set",
                "selected": isinstance(embedder, OpenAICompatibleEmbedder),
            },
        ],
        "corpus_sha256": corpus_sha256,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "dimension": embedder.dimension,
        "entry_count": len(rows),
        "errors": [],
        "explain_plan": "not_run_no_postgresql",
        "index_version": version,
        "model_id": embedder.model_id,
        "model_version": embedder.model_version,
        "postgres_target": "pgvector",
        "postgres_vector_column": "vector(256)",
        "run_id": run_id,
        "status": status,
        "ticket": "I10",
        "vector_ids": [item.vector_id for item in rows],
        "vector_store": "sqlite",
    }


def _filter_metrics(
    filtered: _Filter,
    embedder: Embedder | None,
    embed_ms: float,
    third_party_calls: int,
) -> dict[str, int | float | str | bool | None]:
    return {
        "deduped_rows": len(filtered.accepted),
        "dimension": 0 if embedder is None else embedder.dimension,
        "duplicates": filtered.duplicates,
        "embed_ms": embed_ms,
        "input_rows": filtered.input_rows,
        "model_id": "" if embedder is None else embedder.model_id,
        "model_version": "" if embedder is None else embedder.model_version,
        "postgres_target": "pgvector",
        "rejected_empty": filtered.rejected_empty,
        "rejected_license": filtered.rejected_license,
        "rejected_secret": filtered.rejected_secret,
        "rejected_unclean": filtered.rejected_unclean,
        "third_party_calls": third_party_calls,
        "vector_store": "sqlite",
    }


def _emit(
    *,
    directory: Path,
    run_id: str,
    status: TaskStatus,
    index_version: str | None,
    entry_count: int,
    degraded: bool,
    origin: str,
    errors: tuple[str, ...],
    extra: dict[str, int | float | str | bool | None],
    note: str = "",
    corpus_sha256: str | None = None,
    vector_ids: tuple[str, ...] = (),
    degraded_reason: str | None = None,
    index_manifest: dict[str, object] | None = None,
) -> IndexBuildResult:
    metrics: dict[str, int | float | str | bool | None] = {
        "degraded": degraded,
        "entry_count": entry_count,
        "indexed_rows": entry_count,
        "origin": origin,
        "status": status.value,
        "third_party_writes": 0,
    }
    metrics.update(extra)
    manifest: dict[str, object] = {
        "corpus_sha256": corpus_sha256,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "entry_count": entry_count,
        "errors": list(errors),
        "explain_plan": "not_run_no_postgresql",
        "index_version": index_version,
        "model_id": extra.get("model_id"),
        "note": note,
        "origin": origin,
        "postgres_target": "pgvector",
        "postgres_vector_column": "vector(256)",
        "run_id": run_id,
        "status": status.value,
        "ticket": "I10",
        "vector_ids": list(vector_ids),
        "vector_store": "sqlite",
    }
    _write_bytes(directory / "manifest.json", _pretty(manifest))
    stored = manifest if index_manifest is None else index_manifest
    _write_bytes(directory / "index_manifest.json", _pretty(stored))
    _write_bytes(directory / "metrics.json", _pretty(metrics))
    log = {
        "degraded": degraded,
        "entry_count": entry_count,
        "event": "i10_index",
        "run_id": run_id,
        "status": status.value,
    }
    _write_bytes(directory / "log.jsonl", (json.dumps(log, sort_keys=True) + "\n").encode("utf-8"))
    return IndexBuildResult(
        status=status,
        run_id=run_id,
        index_version=index_version,
        entry_count=entry_count,
        degraded=degraded,
        origin=origin,
        errors=errors,
        metrics=metrics,
    )


def _db_error(
    directory: Path,
    run_id: str,
    origin: str,
    filtered: _Filter,
    embedder: Embedder,
    embed_ms: float = 0.0,
    calls: int = 0,
) -> IndexBuildResult:
    return _emit(
        directory=directory,
        run_id=run_id,
        status=TaskStatus.RETRYABLE_ERROR,
        index_version=None,
        entry_count=0,
        degraded=False,
        origin=origin,
        errors=("retryable_error: index database",),
        extra=_filter_metrics(filtered, embedder, embed_ms, calls),
    )


def _load_run_records(artifacts_dir: Path, run_id: str) -> tuple[list[dict[str, object]], str]:
    manifest_path = artifacts_dir / run_id / "D21" / "manifest.json"
    sanitized = _load_named(manifest_path, "sanitized_name", "sanitized_sha256")
    if isinstance(sanitized, list):
        return sanitized, "D21"
    if isinstance(sanitized, str):
        raise _InputError(sanitized)
    if manifest_path.is_file():
        raise _InputError(
            "policy_blocked: D21 sanitized output is not successful",
            status=TaskStatus.POLICY_BLOCKED,
        )
    raise _InputError(
        "policy_blocked: D21 sanitized output is absent",
        status=TaskStatus.POLICY_BLOCKED,
    )


def _load_named(
    manifest_path: Path,
    name_key: str,
    sha_key: str,
) -> list[dict[str, object]] | str | None:
    if not manifest_path.is_file():
        return None
    document = _read_json(manifest_path)
    if document is None:
        return "fatal_error: input manifest is incomplete"
    if document.get("status") != TaskStatus.SUCCESS.value:
        return None
    if document.get("isolated") is True:
        return "fatal_error: index isolated"
    name = document.get(name_key)
    if not isinstance(name, str) or name == "":
        return "fatal_error: input manifest is incomplete"
    path = manifest_path.parent / name
    try:
        payload = path.read_bytes()
    except OSError:
        return "fatal_error: input manifest is incomplete"
    declared = document.get(sha_key)
    if not isinstance(declared, str) or declared.lower() != hashlib.sha256(payload).hexdigest():
        return "fatal_error: input sha256 mismatch"
    if name.endswith(".jsonl"):
        return _read_jsonl(payload)
    if name.endswith(".parquet"):
        return _read_parquet(path)
    return "fatal_error: input manifest is incomplete"


def _is_isolated(artifacts_dir: Path, run_id: str, database: Path) -> bool:
    manifest_path = artifacts_dir / run_id / "D21" / "manifest.json"
    if manifest_path.is_file():
        document = _read_json(manifest_path)
        if document is not None and document.get("isolated") is True:
            return True
    if not database.is_file():
        return False
    conn = sqlite3.connect(database)
    try:
        if not _table_exists(conn, "run_guard"):
            return False
        row = conn.execute(
            "SELECT index_isolated FROM run_guard WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    if row is None:
        return False
    flag = row[0]
    if isinstance(flag, bool) or not isinstance(flag, int):
        return False
    return flag == 1


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return _as_dict(loaded)


def _read_jsonl(payload: bytes) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    text = payload.decode("utf-8")
    for line in text.splitlines():
        if line.strip() == "":
            continue
        parsed = _as_dict(json.loads(line))
        if parsed is not None:
            rows.append(parsed)
    return rows


def _read_parquet(path: Path) -> list[dict[str, object]]:
    try:
        parquet = importlib.import_module("pyarrow.parquet")
    except ImportError as exc:
        raise _InputError("fatal_error: pyarrow is not installed") from exc
    read_table = getattr(parquet, "read_table", None)
    if read_table is None:
        raise _InputError("fatal_error: pyarrow is not installed")
    table: object = read_table(path)
    raw: object = getattr(table, "to_pylist", lambda: [])()
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, object]] = []
    for item in raw:
        parsed = _as_dict(item)
        if parsed is not None:
            rows.append(parsed)
    return rows


def _connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_sqlite_ddl())
    return conn


def _sqlite_ddl() -> str:
    path = Path(__file__).resolve().parents[3] / "migrations" / _DDL_NAME
    return path.read_text(encoding="utf-8")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _vector_params(
    version: str,
    embedder: Embedder,
    item: _Accepted,
    blob: bytes,
) -> tuple[object, ...]:
    return (
        version,
        item.vector_id,
        item.chunk_id,
        item.source_id,
        item.source_record_id,
        item.language,
        item.license_status,
        embedder.model_id,
        embedder.model_version,
        item.content_sha256,
        hashlib.sha256(blob).hexdigest(),
        blob,
        item.body,
    )


_STAGING_INSERT = """
INSERT INTO chunk_vectors_staging (
    index_version, vector_id, chunk_id, source_id, source_record_id,
    language, license_status, model_id, model_version, content_sha256,
    vector_sha256, embedding, body
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_LIVE_INSERT = """
INSERT INTO chunk_vectors (
    index_version, vector_id, chunk_id, source_id, source_record_id,
    language, license_status, model_id, model_version, content_sha256,
    vector_sha256, embedding, body
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_VERSION_INSERT = """
INSERT INTO index_versions (
    index_version, run_id, model_id, model_version, dimension, corpus_sha256,
    status, entry_count, degraded, manifest_json, created_at, published_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _cell_str(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise sqlite3.Error(f"expected string {key}")
    return value


def _cell_int(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise sqlite3.Error(f"expected integer {key}")
    return value


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


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
