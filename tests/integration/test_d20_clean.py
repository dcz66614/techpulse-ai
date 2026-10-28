"""D20 cleaning tests. Inputs are collector-fixture records. No live HTTP."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from techpulse.contracts import RawRecord, TaskStatus
from techpulse.normalize.clean import (
    PARQUET_NOTE,
    SHORT_EXCERPT_CHARS,
    clean_from_manifest,
    clean_ndjson,
)
from techpulse.workflows.activities import LocalObjectStore, archive_collector_ndjson
from tests.integration.collector_fixtures import load_fixture_records, ndjson_bytes

_BLOCKED = "BLOCKED-FULL-TEXT-SHOULD-NOT-PASS"
_UNKNOWN = "UNKNOWN-FULL-TEXT-SHOULD-NOT-PASS"
_HIDDEN = "HIDDEN-BODY"


def test_archived_fixtures_clean_without_passing_metadata_bodies() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = load_fixture_records(root / "collect")
        payload = ndjson_bytes(records)
        artifacts = root / "artifacts"
        store = LocalObjectStore(artifacts)
        archived = archive_collector_ndjson(
            payload,
            run_id="d20-chain",
            database=root / "archive.sqlite",
            store=store,
            artifacts_dir=artifacts,
            proposed_cursors={record.source_id.value: "fixture-end" for record in records},
        )
        assert archived.status is TaskStatus.SUCCESS
        manifest = artifacts / "d20-chain" / "D10" / "raw_manifest.json"
        cleaned = clean_from_manifest(manifest, store=store, artifacts_dir=artifacts)
        assert cleaned.status is TaskStatus.SUCCESS
        assert cleaned.errors == ()
        assert cleaned.quarantined == 0
        assert cleaned.metrics["third_party_writes"] == 0
        assert cleaned.metrics["extraction_success_rate"] == 1.0
        assert cleaned.metrics["required_field_completeness"] == 1.0
        assert cleaned.metrics["quarantine_ratio"] == 0.0
        directory = artifacts / "d20-chain" / "D20"
        _assert_format(cleaned.output_format, cleaned.note, directory)
        rows = _rows(directory / "clean.jsonl")
        by_source = {str(row["source_id"]): row for row in rows}
        assert by_source["openalex"]["retention"] == "metadata"
        assert by_source["openalex"]["abstract_or_excerpt"] == ""
        assert "mailto" not in json.dumps(by_source["openalex"])
        assert by_source["crossref"]["abstract_or_excerpt"] == ""
        assert "compare" not in json.dumps(by_source["crossref"])
        assert by_source["hn"]["retention"] == "metadata"
        assert by_source["hn"]["abstract_or_excerpt"] == ""
        github = by_source["github"]
        assert github["retention"] == "short_excerpt"
        assert isinstance(github["abstract_or_excerpt"], str)
        assert github["abstract_or_excerpt"]
        assert github["language"] == "und"
        assert str(github["collected_at"]).endswith("Z")
        assert (directory / "manifest.json").is_file()
        assert (directory / "metrics.json").is_file()
        assert (directory / "log.jsonl").is_file()
        assert (directory / "quarantine.jsonl").read_text(encoding="utf-8") == ""


def test_html_controls_language_timezone_and_withheld_text() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = load_fixture_records(root / "collect")
        base = records[0]
        html_row = _mutate(
            base,
            source_record_id=base.source_record_id + "-html",
            license_status="fulltext_allowed",
            language="EN_us",
            published_at="2024-03-02T10:00:00+02:00",
            abstract_or_excerpt=(
                "<jats:p>Identifiers\u0000 should <b>compare</b>.</jats:p>SURROGATE"
            ),
        )
        long_text = " ".join(["alpha"] * 80)
        long_row = _mutate(
            records[1],
            source_record_id=records[1].source_record_id + "-long",
            license_status="reusable_excerpt",
            abstract_or_excerpt=long_text,
        )
        blocked = _mutate(
            records[2],
            source_record_id=records[2].source_record_id + "-blocked",
            license_status="blocked",
            abstract_or_excerpt=_BLOCKED,
        )
        unknown = _mutate(
            records[3],
            source_record_id=records[3].source_record_id + "-unknown",
            license_status="unknown",
            abstract_or_excerpt=_UNKNOWN,
        )
        payload = _dumps(
            [_plain(record) for record in records] + [html_row, long_row, blocked, unknown]
        )
        # A lone surrogate is not valid UTF-8. The JSON escape is, and loads back to one.
        payload = payload.replace(b"SURROGATE", b"\\ud800")
        digest = hashlib.sha256(payload).hexdigest()
        artifacts = root / "artifacts"
        result = clean_ndjson(
            payload,
            run_id="d20-clean",
            artifacts_dir=artifacts,
            expected_sha256=digest,
        )
        assert result.status is TaskStatus.SUCCESS
        assert result.errors == ()
        assert result.quarantined == 0
        assert result.metrics["extraction_success_denominator"] == 3
        assert result.metrics["extraction_success_numerator"] == 3
        directory = artifacts / "d20-clean" / "D20"
        text = directory.read_text() if directory.is_file() else _tree_text(directory)
        assert _BLOCKED not in text
        assert _UNKNOWN not in text
        rows = {str(row["source_record_id"]): row for row in _rows(directory / "clean.jsonl")}
        html_out = rows[base.source_record_id + "-html"]
        assert html_out["retention"] == "full_text"
        assert html_out["abstract_or_excerpt"] == "Identifiers should compare."
        assert html_out["language"] == "en"
        assert html_out["published_at"] == "2024-03-02T08:00:00Z"
        assert "<" not in str(html_out["abstract_or_excerpt"])
        long_out = rows[records[1].source_record_id + "-long"]
        excerpt = str(long_out["abstract_or_excerpt"])
        assert long_out["retention"] == "short_excerpt"
        assert len(excerpt) <= SHORT_EXCERPT_CHARS
        assert long_text.startswith(excerpt)
        assert len(excerpt) < len(long_text)
        assert rows[records[2].source_record_id + "-blocked"]["abstract_or_excerpt"] == ""
        assert rows[records[2].source_record_id + "-blocked"]["retention"] == "withheld"
        assert rows[records[3].source_record_id + "-unknown"]["abstract_or_excerpt"] == ""
        assert rows[records[3].source_record_id + "-unknown"]["retention"] == "withheld"


def test_empty_extraction_is_quarantined_and_fatal_above_the_threshold() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = load_fixture_records(root / "collect")
        base = records[0]
        bad = _mutate(
            base,
            source_record_id=base.source_record_id + "-empty",
            license_status="fulltext_allowed",
            abstract_or_excerpt=f"<script>{_HIDDEN}</script>",
        )
        payload = _dumps([bad])
        artifacts = root / "artifacts"
        result = clean_ndjson(payload, run_id="d20-fatal", artifacts_dir=artifacts)
        assert result.status is TaskStatus.FATAL_ERROR
        assert result.errors == ("fatal_error: extraction success below 0.97",)
        assert result.metrics["quarantine_ratio"] == 1.0
        assert result.metrics["extraction_success_rate"] == 0.0
        directory = artifacts / "d20-fatal" / "D20"
        assert _HIDDEN not in _tree_text(directory)
        assert _rows(directory / "clean.jsonl") == []
        quarantined = _rows(directory / "quarantine.jsonl")
        assert quarantined[0]["reason"] == "empty_after_cleaning"
        assert quarantined[0]["extraction_failure_reason"] == "empty_after_cleaning"
        assert "abstract_or_excerpt" not in quarantined[0]


def test_empty_title_is_not_invented() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = load_fixture_records(root / "collect")
        base = records[1]
        bad = _mutate(
            base,
            source_record_id=base.source_record_id + "-title",
            license_status="metadata_only",
            title="<b></b>",
        )
        artifacts = root / "artifacts"
        result = clean_ndjson(_dumps([bad]), run_id="d20-title", artifacts_dir=artifacts)
        assert result.status is TaskStatus.FATAL_ERROR
        assert "fatal_error: required field completeness below 0.98" in result.errors
        directory = artifacts / "d20-title" / "D20"
        tree = _tree_text(directory)
        assert "Untitled" not in tree
        assert _rows(directory / "clean.jsonl") == []
        assert _rows(directory / "quarantine.jsonl")[0]["reason"] == "title_empty_after_cleaning"


def test_sha256_mismatch_is_fatal_and_writes_no_clean_rows() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = load_fixture_records(root / "collect")
        payload = ndjson_bytes(records)
        artifacts = root / "artifacts"
        result = clean_ndjson(
            payload,
            run_id="d20-hash",
            artifacts_dir=artifacts,
            expected_sha256="0" * 64,
        )
        assert result.status is TaskStatus.FATAL_ERROR
        assert result.errors == ("fatal_error: sha256 mismatch",)
        directory = artifacts / "d20-hash" / "D20"
        assert not (directory / "clean.jsonl").exists()
        assert not (directory / "clean.parquet").exists()
        manifest = _object(directory / "manifest.json")
        assert manifest["status"] == "fatal_error"
        assert manifest["note"] == PARQUET_NOTE or importlib.util.find_spec("pyarrow") is not None


def _assert_format(output_format: str, note: str, directory: Path) -> None:
    if importlib.util.find_spec("pyarrow") is None:
        assert output_format == "jsonl"
        assert note == PARQUET_NOTE
        assert (directory / "clean.jsonl").is_file()
        assert not (directory / "clean.parquet").exists()
        return
    assert output_format == "parquet"
    assert note == ""
    assert (directory / "clean.parquet").is_file()


def _mutate(record: RawRecord, **changes: object) -> dict[str, Any]:
    payload = _plain(record)
    payload.update(changes)
    return payload


def _plain(record: RawRecord) -> dict[str, Any]:
    loaded: object = json.loads(record.model_dump_json())
    if not isinstance(loaded, dict):
        raise AssertionError("record json")
    parsed: dict[str, Any] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise AssertionError("record json")
        parsed[key] = value
    return parsed


def _dumps(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")


def _rows(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    if text == "":
        return []
    found: list[dict[str, object]] = []
    for line in text.splitlines():
        found.append(_object_text(line))
    return found


def _object(path: Path) -> dict[str, object]:
    return _object_text(path.read_text(encoding="utf-8"))


def _object_text(text: str) -> dict[str, object]:
    loaded: object = json.loads(text)
    if not isinstance(loaded, dict):
        raise AssertionError("expected an object")
    parsed: dict[str, object] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise AssertionError("expected string keys")
        parsed[key] = value
    return parsed


def _tree_text(directory: Path) -> str:
    parts: list[str] = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)
