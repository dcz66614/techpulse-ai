"""D20 offline cleaning and licence marking.

HTML, control characters, and invalid Unicode are stripped locally. There is
no network call. Licence status decides whether the body is dropped, cut to
a short excerpt, or kept. ``blocked`` and ``unknown`` bodies are not written
to the clean output or the quarantine file. PyArrow is used for Parquet only
when it is already installed; otherwise the clean rows are JSONL.
"""

from __future__ import annotations

import hashlib
import html
import importlib
import importlib.util
import json
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from techpulse.contracts import TaskStatus
from techpulse.workflows.activities import ObjectStore, default_raw_schema_path

SHORT_EXCERPT_CHARS = 280
PARQUET_NOTE = "pyarrow is not installed; wrote clean.jsonl instead of clean.parquet"
_EXTRACTION_PERCENT = 97
_COMPLETENESS_PERCENT = 98
_TAG_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_PRIMARY = re.compile(r"^[a-z]{2,3}$")
_LANGUAGE_NAMES = {
    "chinese": "zh",
    "english": "en",
    "french": "fr",
    "german": "de",
    "japanese": "ja",
    "korean": "ko",
    "mandarin": "zh",
    "spanish": "es",
    "und": "und",
}
_EXTRACTABLE = {"reusable_excerpt", "fulltext_allowed"}
_WITHHELD = {"blocked", "unknown"}


@dataclass(frozen=True)
class CleanResult:
    status: TaskStatus
    run_id: str
    clean_rows: int
    quarantined: int
    output_format: str
    note: str
    errors: tuple[str, ...]
    metrics: dict[str, int | float | str | bool | None]


def clean_ndjson(
    payload: bytes,
    *,
    run_id: str,
    artifacts_dir: Path,
    expected_sha256: str | None = None,
    schema_path: Path | None = None,
    raw_uris: Mapping[int, str] | None = None,
    record_sha256: Mapping[int, str] | None = None,
    only_lines: set[int] | None = None,
) -> CleanResult:
    """Clean one NDJSON payload. Bad rows are quarantined. Over-threshold runs fail."""
    checked = _run_id(run_id)
    schema = default_raw_schema_path() if schema_path is None else schema_path
    directory = artifacts_dir / checked / "D20"
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and expected_sha256.lower() != digest:
        return _emit(
            directory=directory,
            run_id=checked,
            status=TaskStatus.FATAL_ERROR,
            clean_rows=[],
            quarantine=[],
            total=0,
            complete=0,
            extracted=0,
            extractable=0,
            errors=("fatal_error: sha256 mismatch",),
            write_clean=False,
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return _emit(
            directory=directory,
            run_id=checked,
            status=TaskStatus.FATAL_ERROR,
            clean_rows=[],
            quarantine=[],
            total=0,
            complete=0,
            extracted=0,
            extractable=0,
            errors=("fatal_error: invalid utf-8",),
            write_clean=False,
        )
    validator = _validator(schema)
    lines = text.splitlines()
    clean_rows: list[dict[str, object]] = []
    quarantine: list[dict[str, object]] = []
    total = 0
    complete = 0
    extracted = 0
    extractable = 0
    for line_no, line in enumerate(lines, start=1):
        if only_lines is not None and line_no not in only_lines:
            continue
        total += 1
        raw_uri = f"local:{checked}/D20/input#line={line_no}"
        if raw_uris is not None and line_no in raw_uris:
            raw_uri = raw_uris[line_no]
        declared = None if record_sha256 is None else record_sha256.get(line_no)
        outcome = _clean_line(line, line_no, raw_uri, declared, validator)
        if outcome.extractable:
            extractable += 1
            if outcome.extracted:
                extracted += 1
        if outcome.complete:
            complete += 1
        if outcome.row is None:
            quarantine.append(outcome.quarantine)
        else:
            clean_rows.append(outcome.row)
    errors: list[str] = []
    if not _meets(extracted, extractable, _EXTRACTION_PERCENT):
        errors.append("fatal_error: extraction success below 0.97")
    if not _meets(complete, total, _COMPLETENESS_PERCENT):
        errors.append("fatal_error: required field completeness below 0.98")
    status = TaskStatus.FATAL_ERROR if errors else TaskStatus.SUCCESS
    return _emit(
        directory=directory,
        run_id=checked,
        status=status,
        clean_rows=clean_rows,
        quarantine=quarantine,
        total=total,
        complete=complete,
        extracted=extracted,
        extractable=extractable,
        errors=tuple(errors),
        write_clean=True,
    )


def clean_from_manifest(
    manifest_path: Path,
    *,
    store: ObjectStore,
    artifacts_dir: Path,
    schema_path: Path | None = None,
) -> CleanResult:
    """Clean the records named by a D10 raw manifest, re-checking the object hash."""
    loaded = _read_manifest(manifest_path)
    if loaded is None:
        return _emit(
            directory=artifacts_dir / "unknown" / "D20",
            run_id="unknown",
            status=TaskStatus.FATAL_ERROR,
            clean_rows=[],
            quarantine=[],
            total=0,
            complete=0,
            extracted=0,
            extractable=0,
            errors=("fatal_error: raw manifest is incomplete",),
            write_clean=False,
        )
    run_id, object_uri, object_sha256, raw_uris, hashes, lines = loaded
    try:
        payload = store.get(object_uri)
    except OSError:
        return _emit(
            directory=artifacts_dir / run_id / "D20",
            run_id=run_id,
            status=TaskStatus.RETRYABLE_ERROR,
            clean_rows=[],
            quarantine=[],
            total=0,
            complete=0,
            extracted=0,
            extractable=0,
            errors=("retryable_error: object store",),
            write_clean=False,
        )
    return clean_ndjson(
        payload,
        run_id=run_id,
        artifacts_dir=artifacts_dir,
        expected_sha256=object_sha256,
        schema_path=schema_path,
        raw_uris=raw_uris,
        record_sha256=hashes,
        only_lines=lines,
    )


@dataclass(frozen=True)
class _Outcome:
    row: dict[str, object] | None
    quarantine: dict[str, object]
    complete: bool
    extractable: bool
    extracted: bool


def _clean_line(
    line: str,
    line_no: int,
    raw_uri: str,
    declared_sha256: str | None,
    validator: Draft202012Validator,
) -> _Outcome:
    empty = _quarantine(line_no, raw_uri, None, "empty_line")
    if line.strip() == "":
        return _Outcome(None, empty, False, False, False)
    try:
        loaded: object = json.loads(line)
    except json.JSONDecodeError:
        return _Outcome(
            None, _quarantine(line_no, raw_uri, None, "invalid_json"), False, False, False
        )
    record = _as_dict(loaded)
    if record is None:
        item = _quarantine(line_no, raw_uri, None, "schema: record must be an object")
        return _Outcome(None, item, False, False, False)
    found = list(validator.iter_errors(record))
    if found:
        item = _quarantine(line_no, raw_uri, record, "schema: " + found[0].message)
        return _Outcome(None, item, False, False, False)
    digest = record.get("content_sha256")
    if declared_sha256 is not None and digest != declared_sha256:
        item = _quarantine(line_no, raw_uri, record, "hash_mismatch")
        return _Outcome(None, item, False, False, False)
    return _licence_row(record, line_no, raw_uri)


def _licence_row(record: Mapping[str, Any], line_no: int, raw_uri: str) -> _Outcome:
    license_status = _text(record, "license_status")
    title = clean_text(_text(record, "title") or "")
    body = clean_text(_text(record, "abstract_or_excerpt", allow_empty=True) or "")
    language = normalize_language(record.get("language"))
    published, published_error = _optional_time(record.get("published_at"))
    collected, collected_error = _required_time(record.get("collected_at"))
    missing_reason = _text(record, "published_at_missing_reason", allow_empty=False)
    reasons: list[str] = []
    if published_error is not None:
        reasons.append(published_error)
    if collected_error is not None:
        reasons.append(collected_error)
    if not title:
        reasons.append("title_empty_after_cleaning")
    retention, text, extraction_reason = _retain(license_status or "", body)
    if extraction_reason is not None:
        reasons.append(extraction_reason)
    required = {
        "source_id": _text(record, "source_id"),
        "source_record_id": _text(record, "source_record_id"),
        "canonical_url": _text(record, "canonical_url"),
        "collected_at": collected,
        "language": language,
        "license_status": license_status,
        "content_sha256": _text(record, "content_sha256"),
        "policy_snapshot_id": _text(record, "policy_snapshot_id"),
        "raw_uri": raw_uri,
    }
    fields_ok = title != "" and all(value for value in required.values())
    if published is None and not missing_reason:
        fields_ok = False
        if "published_at_missing" not in reasons and published_error is None:
            reasons.append("published_at_missing")
    extractable = license_status in _EXTRACTABLE
    extracted = extractable and text != "" and extraction_reason is None
    if reasons or not fields_ok:
        reason = reasons[0] if reasons else "required_field_missing"
        return _Outcome(
            None,
            _quarantine(line_no, raw_uri, record, reason),
            fields_ok and extraction_reason is not None,
            extractable,
            extracted,
        )
    row: dict[str, object] = {
        "schema_version": "1.0",
        "source_id": required["source_id"],
        "source_record_id": required["source_record_id"],
        "canonical_url": required["canonical_url"],
        "title": title,
        "abstract_or_excerpt": text,
        "published_at": published,
        "published_at_missing_reason": missing_reason,
        "collected_at": collected,
        "language": language,
        "license_status": license_status,
        "content_sha256": required["content_sha256"],
        "policy_snapshot_id": required["policy_snapshot_id"],
        "raw_uri": raw_uri,
        "retention": retention,
        "extraction_failure_reason": None,
    }
    return _Outcome(row, {}, True, extractable, extracted)


def clean_text(value: str) -> str:
    """Drop tags, control characters, and invalid Unicode, then collapse space."""
    without_blocks = _TAG_BLOCK.sub(" ", value)
    without_tags = _TAG.sub("", without_blocks)
    return _strip_chars(html.unescape(without_tags))


def normalize_language(value: object) -> str:
    if not isinstance(value, str):
        return "und"
    text = value.strip().lower().replace("_", "-")
    if not text:
        return "und"
    named = _LANGUAGE_NAMES.get(text)
    if named is not None:
        return named
    primary = text.split("-", 1)[0]
    named_primary = _LANGUAGE_NAMES.get(primary)
    if named_primary is not None:
        return named_primary
    if _PRIMARY.fullmatch(primary):
        return primary
    return "und"


def normalize_timestamp(value: str) -> str:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    rendered = parsed.astimezone(UTC).isoformat()
    if rendered.endswith("+00:00"):
        return rendered[:-6] + "Z"
    return rendered


def _retain(license_status: str, body: str) -> tuple[str, str, str | None]:
    if license_status in _WITHHELD:
        return "withheld", "", None
    if license_status == "metadata_only":
        return "metadata", "", None
    if license_status == "reusable_excerpt":
        short = _short_excerpt(body)
        if not short:
            return "short_excerpt", "", "empty_after_cleaning"
        return "short_excerpt", short, None
    if license_status == "fulltext_allowed":
        if not body:
            return "full_text", "", "empty_after_cleaning"
        return "full_text", body, None
    return "withheld", "", "empty_after_cleaning"


def _short_excerpt(text: str) -> str:
    if len(text) <= SHORT_EXCERPT_CHARS:
        return text
    cut = text[:SHORT_EXCERPT_CHARS]
    space = cut.rfind(" ")
    if space >= SHORT_EXCERPT_CHARS // 2:
        return cut[:space]
    return cut


def _optional_time(value: object) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, "published_at_unparseable"
    try:
        return normalize_timestamp(value), None
    except ValueError:
        return None, "published_at_unparseable"


def _required_time(value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, "collected_at_unparseable"
    try:
        return normalize_timestamp(value), None
    except ValueError:
        return None, "collected_at_unparseable"


def _quarantine(
    line_no: int,
    raw_uri: str,
    record: Mapping[str, Any] | None,
    reason: str,
) -> dict[str, object]:
    item: dict[str, object] = {
        "line": line_no,
        "raw_uri": raw_uri,
        "reason": reason,
        "extraction_failure_reason": reason if reason == "empty_after_cleaning" else None,
    }
    if record is None:
        return item
    for key in ("source_id", "source_record_id", "license_status", "policy_snapshot_id"):
        value = record.get(key)
        if isinstance(value, str):
            item[key] = value
    return item


def _emit(
    *,
    directory: Path,
    run_id: str,
    status: TaskStatus,
    clean_rows: list[dict[str, object]],
    quarantine: list[dict[str, object]],
    total: int,
    complete: int,
    extracted: int,
    extractable: int,
    errors: tuple[str, ...],
    write_clean: bool,
) -> CleanResult:
    directory.mkdir(parents=True, exist_ok=True)
    note = "" if importlib.util.find_spec("pyarrow") is not None else PARQUET_NOTE
    output_format = "parquet" if not note else "jsonl"
    clean_name: str | None = None
    clean_sha: str | None = None
    if write_clean:
        clean_name, output_format, note, clean_sha = _write_clean(directory, clean_rows)
    quarantine_body = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in quarantine
    )
    _write_bytes(directory / "quarantine.jsonl", quarantine_body.encode("utf-8"))
    ratio = 0.0 if total == 0 else len(quarantine) / total
    extraction_rate = 1.0 if extractable == 0 else extracted / extractable
    completeness = 1.0 if total == 0 else complete / total
    metrics: dict[str, int | float | str | bool | None] = {
        "rows": total,
        "clean_rows": len(clean_rows),
        "quarantine_count": len(quarantine),
        "quarantine_ratio": ratio,
        "extraction_success_numerator": extracted,
        "extraction_success_denominator": extractable,
        "extraction_success_rate": extraction_rate,
        "required_field_complete": complete,
        "required_field_total": total,
        "required_field_completeness": completeness,
        "output_format": output_format,
        "parquet_note": note,
        "status": status.value,
        "third_party_writes": 0,
    }
    manifest = {
        "ticket": "D20",
        "run_id": run_id,
        "status": status.value,
        "rows": len(clean_rows),
        "clean_name": clean_name,
        "clean_sha256": clean_sha,
        "quarantine_name": "quarantine.jsonl",
        "quarantine_count": len(quarantine),
        "output_format": output_format,
        "note": note,
        "errors": list(errors),
    }
    _write_bytes(directory / "manifest.json", _pretty(manifest))
    _write_bytes(directory / "metrics.json", _pretty(metrics))
    log = {
        "event": "d20_clean",
        "run_id": run_id,
        "status": status.value,
        "clean_rows": len(clean_rows),
        "quarantine_count": len(quarantine),
        "output_format": output_format,
    }
    _write_bytes(directory / "log.jsonl", (json.dumps(log, sort_keys=True) + "\n").encode("utf-8"))
    return CleanResult(
        status=status,
        run_id=run_id,
        clean_rows=len(clean_rows),
        quarantined=len(quarantine),
        output_format=output_format,
        note=note,
        errors=errors,
        metrics=metrics,
    )


def _write_clean(directory: Path, rows: list[dict[str, object]]) -> tuple[str, str, str, str]:
    if importlib.util.find_spec("pyarrow") is None:
        body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        data = body.encode("utf-8")
        _write_bytes(directory / "clean.jsonl", data)
        return "clean.jsonl", "jsonl", PARQUET_NOTE, hashlib.sha256(data).hexdigest()
    _write_parquet(directory / "clean.parquet", rows)
    data = (directory / "clean.parquet").read_bytes()
    return "clean.parquet", "parquet", "", hashlib.sha256(data).hexdigest()


def _write_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    pyarrow: Any = importlib.import_module("pyarrow")
    parquet: Any = importlib.import_module("pyarrow.parquet")
    table = pyarrow.Table.from_pylist(rows)
    parquet.write_table(table, path)


def _read_manifest(
    path: Path,
) -> tuple[str, str, str, dict[int, str], dict[int, str], set[int]] | None:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    document = _as_dict(loaded)
    if document is None:
        return None
    run_id = document.get("run_id")
    object_uri = document.get("object_uri")
    object_sha256 = document.get("object_sha256")
    records = document.get("records")
    if not isinstance(run_id, str) or not isinstance(object_uri, str):
        return None
    if not isinstance(object_sha256, str) or not isinstance(records, list):
        return None
    raw_uris: dict[int, str] = {}
    hashes: dict[int, str] = {}
    lines: set[int] = set()
    for item in records:
        mapped = _as_dict(item)
        if mapped is None:
            return None
        line_no = mapped.get("line")
        raw_uri = mapped.get("raw_uri")
        digest = mapped.get("content_sha256")
        if isinstance(line_no, bool) or not isinstance(line_no, int):
            return None
        if not isinstance(raw_uri, str) or not isinstance(digest, str):
            return None
        raw_uris[line_no] = raw_uri
        hashes[line_no] = digest
        lines.add(line_no)
    return run_id, object_uri, object_sha256, raw_uris, hashes, lines


def _meets(numerator: int, denominator: int, percent: int) -> bool:
    if denominator == 0:
        return True
    return numerator * 100 >= percent * denominator


def _strip_chars(value: str) -> str:
    kept: list[str] = []
    for char in value:
        if _drop(ord(char)):
            continue
        kept.append(char)
    return " ".join("".join(kept).split())


def _drop(code: int) -> bool:
    if 0xD800 <= code <= 0xDFFF or code == 0xFFFD:
        return True
    if 0xFDD0 <= code <= 0xFDEF or (code & 0xFFFF) in {0xFFFE, 0xFFFF}:
        return True
    return unicodedata.category(chr(code)) in {"Cc", "Cf"}


def _text(record: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str | None:
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


def _validator(path: Path) -> Draft202012Validator:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    schema = _as_dict(loaded)
    if schema is None:
        raise ValueError("raw record schema is not an object")
    return Draft202012Validator(schema)


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."} or any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _pretty(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
