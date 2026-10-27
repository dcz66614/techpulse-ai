"""Positive and negative checks for schema_version 1.0, plus migration round-trip."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from techpulse.contracts import (
    EvidenceItem,
    RawRecord,
    Report,
    TaskEnvelope,
    TaskResult,
    Trend,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
DRAFT = "https://json-schema.org/draft/2020-12/schema"

MODELS: dict[str, type[BaseModel]] = {
    "task_envelope": TaskEnvelope,
    "task_result": TaskResult,
    "raw_record": RawRecord,
    "evidence_item": EvidenceItem,
    "trend": Trend,
    "report": Report,
}

TABLES: dict[str, set[str]] = {
    "source_registry": {
        "source_id",
        "source_family",
        "base_url",
        "policy_snapshot_id",
        "enabled",
        "requests_per_minute",
        "checked_at",
    },
    "raw_records": {
        "id",
        "source_id",
        "source_record_id",
        "canonical_url",
        "title",
        "abstract_or_excerpt",
        "published_at",
        "collected_at",
        "content_sha256",
        "license_status",
        "policy_snapshot_id",
        "raw_uri",
    },
    "canonical_entities": {
        "canonical_id",
        "entity_type",
        "display_name",
        "normalized_key",
        "created_at",
    },
    "evidence_items": {
        "evidence_id",
        "canonical_id",
        "source_record_id",
        "source_family",
        "excerpt",
        "quality_tier",
        "license_status",
        "embedding_status",
        "sha256",
    },
    "trend_scores": {
        "trend_id",
        "topic",
        "window_days",
        "score",
        "confidence",
        "scored_at",
        "features_json",
        "source_families",
    },
    "trend_evidence": {"trend_id", "evidence_id", "relation"},
    "published_reports": {
        "report_id",
        "report_kind",
        "content_uri",
        "qa_manifest_uri",
        "evidence_snapshot_uri",
        "published_at",
        "generated_by_ai",
    },
    "task_runs": {
        "run_id",
        "task_id",
        "attempt",
        "status",
        "input_sha256",
        "started_at",
        "finished_at",
        "budget_spent_usd",
        "result_uri",
    },
}


def _load_schema(name: str) -> dict[str, Any]:
    raw = json.loads((CONTRACTS / f"{name}.schema.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        message = f"{name} schema is not an object"
        raise AssertionError(message)
    return raw


def _example(name: str) -> dict[str, Any]:
    schema = _load_schema(name)
    raw_examples = schema.get("examples")
    if not isinstance(raw_examples, list) or not raw_examples:
        message = f"{name} has no examples"
        raise AssertionError(message)
    first = raw_examples[0]
    if not isinstance(first, dict):
        message = f"{name} example is not an object"
        raise AssertionError(message)
    copied = copy.deepcopy(first)
    if not isinstance(copied, dict):
        message = f"{name} example copy is not an object"
        raise AssertionError(message)
    return copied


def _as_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        message = "expected an object"
        raise AssertionError(message)
    return value


def _apply_negative(name: str, kind: str, payload: dict[str, Any]) -> None:
    if name == "task_envelope" and kind == "missing":
        del payload["task_id"]
    elif name == "task_envelope" and kind == "wrong_type":
        payload["attempt"] = "1"
    elif name == "task_envelope" and kind == "bad_enum":
        _as_dict(payload["trigger"])["type"] = "manual"
    elif name == "task_result" and kind == "missing":
        del payload["status"]
    elif name == "task_result" and kind == "wrong_type":
        payload["next_tasks"] = "D30"
    elif name == "task_result" and kind == "bad_enum":
        payload["status"] = "unknown"
    elif name == "raw_record" and kind == "missing":
        del payload["source_record_id"]
    elif name == "raw_record" and kind == "wrong_type":
        payload["collected_at"] = 1
    elif name == "raw_record" and kind == "bad_enum":
        payload["license_status"] = "public_domain"
    elif name == "evidence_item" and kind == "missing":
        del payload["evidence_id"]
    elif name == "evidence_item" and kind == "wrong_type":
        payload["quote_length"] = "42"
    elif name == "evidence_item" and kind == "bad_enum":
        payload["quality_tier"] = "Z"
    elif name == "trend" and kind == "missing":
        del payload["claim"]
    elif name == "trend" and kind == "wrong_type":
        payload["score"] = "0.72"
    elif name == "trend" and kind == "bad_enum":
        payload["confidence"] = "certain"
    elif name == "report" and kind == "missing":
        del payload["report_id"]
    elif name == "report" and kind == "wrong_type":
        payload["generated_by_ai"] = "true"
    elif name == "report" and kind == "bad_enum":
        payload["report_kind"] = "monthly"
    else:
        message = f"unhandled case {name} {kind}"
        raise AssertionError(message)


def _payload(name: str, kind: str) -> dict[str, Any]:
    payload = _example(name)
    if kind != "positive":
        _apply_negative(name, kind, payload)
    return payload


def _assert_agreement(name: str, payload: dict[str, Any], *, accept: bool) -> None:
    schema = _load_schema(name)
    validator = Draft202012Validator(schema)
    schema_errors = list(validator.iter_errors(payload))
    if accept:
        assert schema_errors == []
        MODELS[name].model_validate(payload)
        return
    assert schema_errors
    with pytest.raises(ValidationError):
        MODELS[name].model_validate(payload)


@pytest.mark.parametrize("name", list(MODELS))
def test_schema_is_draft_2020_12(name: str) -> None:
    """Each contract file is a draft 2020-12 schema whose examples match it."""
    schema = _load_schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == DRAFT
    assert schema["$id"].endswith(":1.0")
    examples = schema["examples"]
    assert isinstance(examples, list) and examples
    validator = Draft202012Validator(schema)
    for example in examples:
        assert isinstance(example, dict)
        assert list(validator.iter_errors(example)) == []
        MODELS[name].model_validate(example)


@pytest.mark.parametrize("name", list(MODELS))
@pytest.mark.parametrize("kind", ["positive", "missing", "wrong_type", "bad_enum"])
def test_pydantic_and_jsonschema_agree(name: str, kind: str) -> None:
    """A missing field, a wrong type, and a bad enum are rejected by both validators."""
    _assert_agreement(name, _payload(name, kind), accept=kind == "positive")


def test_null_published_at_requires_missing_reason() -> None:
    """Section 4 allows a null published_at only when a missing reason is recorded."""
    accepted = _example("raw_record")
    accepted["published_at"] = None
    accepted["published_at_missing_reason"] = "source omitted publication date"
    _assert_agreement("raw_record", accepted, accept=True)

    rejected = _example("raw_record")
    rejected["published_at"] = None
    _assert_agreement("raw_record", rejected, accept=False)


def test_high_confidence_requires_three_source_families() -> None:
    """Section 4: confidence=high only when at least three source families are present."""
    accepted = _example("trend")
    accepted["confidence"] = "high"
    _assert_agreement("trend", accepted, accept=True)

    rejected = _example("trend")
    rejected["confidence"] = "high"
    rejected["source_families"] = ["academic"]
    _assert_agreement("trend", rejected, accept=False)


def test_postgres_ddl_matches_section_5() -> None:
    """The PostgreSQL script declares every section 5 table and check."""
    sql = (ROOT / "migrations" / "0001_init.sql").read_text(encoding="utf-8")
    for table in TABLES:
        assert f"CREATE TABLE {table}" in sql
    assert "BIGSERIAL" in sql
    assert "TIMESTAMPTZ" in sql
    assert "JSONB" in sql
    assert "UUID" in sql
    assert "CHECK (window_days IN (7, 28))" in sql
    assert "CHECK (relation IN ('support', 'contradict'))" in sql
    assert "NUMERIC(12,5)" in sql
    assert "pgvector" not in sql.lower()


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _engine(url: str) -> Engine:
    engine = create_engine(url)

    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    event.listen(engine, "connect", _enable_foreign_keys)
    return engine


def _assert_tables(url: str, *, present: bool) -> None:
    engine = _engine(url)
    try:
        found = set(inspect(engine).get_table_names())
        if not present:
            assert TABLES.keys().isdisjoint(found)
            return
        missing = set(TABLES).difference(found)
        assert not missing
        inspector = inspect(engine)
        for table, columns in TABLES.items():
            reflected = {column["name"] for column in inspector.get_columns(table)}
            assert columns <= reflected
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO source_registry "
                    "(source_id, source_family, base_url, policy_snapshot_id, checked_at) "
                    "VALUES (:source_id, :source_family, :base_url, :policy_snapshot_id, "
                    ":checked_at)"
                ),
                {
                    "source_id": "openalex",
                    "source_family": "academic",
                    "base_url": "https://api.openalex.org",
                    "policy_snapshot_id": "openalex-2026-10-27",
                    "checked_at": "2026-10-27T00:00:00Z",
                },
            )
            row = conn.execute(
                text("SELECT enabled, requests_per_minute FROM source_registry")
            ).one()
            assert row[0] in (0, False)
            assert row[1] == 10
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(
                    text(
                        "INSERT INTO trend_scores "
                        "(trend_id, topic, window_days, score, confidence, scored_at, "
                        "features_json, source_families) "
                        "VALUES ('t', 'RAG', 14, 0.1, 'low', '2026-10-27T00:00:00Z', "
                        "'{}', '[]')"
                    )
                )
            except IntegrityError:
                trans.rollback()
            else:
                trans.rollback()
                message = "window_days 14 was accepted"
                raise AssertionError(message)
            bad_source = conn.begin()
            try:
                conn.execute(
                    text(
                        "INSERT INTO raw_records "
                        "(source_id, source_record_id, canonical_url, title, collected_at, "
                        "content_sha256, license_status, policy_snapshot_id, raw_uri) "
                        "VALUES ('missing', 'id', 'https://example.org/x', 't', "
                        "'2026-10-27T00:00:00Z', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                        "aaaaaaaaaaaaaaaaaaaaaaaa', 'metadata_only', 'snap', 's3://raw/x')"
                    )
                )
            except IntegrityError:
                bad_source.rollback()
            else:
                bad_source.rollback()
                message = "foreign key to source_registry was not enforced"
                raise AssertionError(message)
    finally:
        engine.dispose()


def test_migration_up_down_up_on_sqlite() -> None:
    """Revision 0001 applies, rolls back, and applies again on a temporary SQLite file."""
    with TemporaryDirectory() as tmp:
        database = Path(tmp) / "p10.sqlite"
        url = "sqlite:///" + database.resolve().as_posix()
        cfg = _alembic_config(url)
        command.upgrade(cfg, "head")
        _assert_tables(url, present=True)
        command.downgrade(cfg, "base")
        _assert_tables(url, present=False)
        command.upgrade(cfg, "head")
        _assert_tables(url, present=True)
