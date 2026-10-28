"""Contract checks for the read-only API. No live server and no network."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from techpulse.api.auth import ApiSettings, sign_cursor
from techpulse.api.main import create_app
from techpulse.api.routes import open_readonly
from techpulse.workflows.activities import LocalObjectStore

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "migrations" / "0001_init.sqlite.sql"
UTC_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
FULL_TEXT = "SECRET-FULLTEXT-NOT-FOR-API"
TAIL = "TAIL-SECRET"
FULL_CUT = "FULL-CUT"
FEATURE_TAIL = "FEATURE-TAIL"
FAILED_BODY = "FAILED-REPORT-BODY"
UNSIGNED_BODY = "UNSIGNED-REPORT-BODY"
BLOCKED_EXCERPT = "blocked excerpt should stay hidden"
PATHS = {
    "/health/live",
    "/health/ready",
    "/v1/trends",
    "/v1/trends/{id}",
    "/v1/evidence/{id}",
    "/v1/reports",
    "/v1/reports/{id}",
    "/metrics",
}
WRITE_METHODS = {"post", "put", "patch", "delete", "trace"}


@dataclass(frozen=True)
class Seed:
    root: Path
    database: Path
    app: FastAPI


@dataclass(frozen=True)
class _Evidence:
    evidence_id: str
    license_status: str
    excerpt: str
    published_at: str | None


@dataclass(frozen=True)
class _Trend:
    trend_id: str
    topic: str
    window_days: int
    score: float
    scored_at: str
    claim: str
    note: str | None = None


@dataclass(frozen=True)
class _Report:
    report_id: str
    report_kind: str
    published_at: str
    generated_by_ai: bool
    content: str
    qa_pass: bool
    signed: bool
    trend_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]


def test_openapi_matches_the_read_contract(client: TestClient) -> None:
    response = client.get("/openapi.json")
    schema = _obj(response)
    paths = schema["paths"]
    assert isinstance(paths, dict)
    assert set(paths) == PATHS
    for path, item in paths.items():
        assert isinstance(item, dict)
        assert WRITE_METHODS.isdisjoint(item)
        operation = item["get"]
        assert isinstance(operation, dict)
        responses = operation["responses"]
        assert isinstance(responses, dict)
        assert "422" not in responses
        if path.startswith("/v1/"):
            assert operation["security"] == [{"ReadToken": []}]
    assert _query_names(paths["/v1/trends"]) == {"topic", "window", "limit", "cursor"}
    assert _query_names(paths["/v1/reports"]) == {"kind", "limit", "cursor"}
    assert _status_codes(paths["/v1/trends"]) >= {"200", "400"}
    assert _status_codes(paths["/v1/trends/{id}"]) >= {"200", "404"}
    assert _status_codes(paths["/v1/evidence/{id}"]) >= {"200", "404"}
    assert _status_codes(paths["/v1/reports"]) >= {"200", "400"}
    assert _status_codes(paths["/v1/reports/{id}"]) >= {"200", "404"}
    assert _status_codes(paths["/metrics"]) >= {"200", "403"}
    assert _status_codes(paths["/health/live"]) >= {"200"}
    assert _status_codes(paths["/health/ready"]) >= {"200", "503"}
    components = schema["components"]
    assert isinstance(components, dict)
    schemes = components["securitySchemes"]
    assert isinstance(schemes, dict)
    token = schemes["ReadToken"]
    assert isinstance(token, dict)
    assert token["type"] == "http"
    assert token["scheme"] == "bearer"
    live = paths["/health/live"]
    assert isinstance(live, dict)
    live_get = live["get"]
    assert isinstance(live_get, dict)
    assert "security" not in live_get


def test_only_signed_qa_pass_content_is_visible(client: TestClient) -> None:
    trends = _obj(_get(client, "/v1/trends"))
    assert _ids(trends) == [
        "trend-a",
        "trend-b",
        "trend-c",
        "trend-28",
        "trend-llm",
        "trend-daily",
        "trend-topic",
    ]
    reports = _obj(_get(client, "/v1/reports"))
    assert _ids(reports, "report_id") == [
        "report-w1",
        "report-w2",
        "report-daily",
        "report-topic",
    ]
    listed = json.dumps({"trends": trends, "reports": reports})
    for secret in (FULL_TEXT, FAILED_BODY, UNSIGNED_BODY, BLOCKED_EXCERPT, "secret trend claim"):
        assert secret not in listed
    for hidden in (
        "/v1/trends/trend-hidden",
        "/v1/trends/trend-secret",
        "/v1/trends/trend-unsigned",
        "/v1/trends/missing-trend",
        "/v1/reports/report-fail",
        "/v1/reports/report-unsigned",
        "/v1/reports/missing-report",
        "/v1/evidence/ev-blocked",
        "/v1/evidence/ev-secret",
        "/v1/evidence/ev-unsigned",
        "/v1/evidence/missing-evidence",
    ):
        missing = _obj(_get(client, hidden), 404)
        assert missing["code"] == "not_found"
        assert FAILED_BODY not in json.dumps(missing)
        assert UNSIGNED_BODY not in json.dumps(missing)
        _assert_error(missing)


def test_evidence_hides_unlicensed_full_text(client: TestClient) -> None:
    ok = _obj(_get(client, "/v1/evidence/ev-ok"))
    assert set(ok) == {
        "source_url",
        "excerpt",
        "license",
        "dates",
        "hash",
        "as_of",
        "schema_version",
    }
    assert ok["source_url"] == "https://example.org/records/ev-ok"
    assert ok["excerpt"] == "public excerpt for rag"
    assert ok["license"] == "metadata_only"
    assert ok["hash"] == _digest("ev-ok")
    assert ok["dates"] == {
        "published_at": "2026-10-26T00:00:00Z",
        "collected_at": "2026-10-27T00:00:00Z",
    }
    long = _obj(_get(client, "/v1/evidence/ev-long"))
    assert long["excerpt"] == "L" * 280
    assert long["dates"] == {"published_at": None, "collected_at": "2026-10-27T00:00:00Z"}
    assert TAIL not in json.dumps(long)
    full = _obj(_get(client, "/v1/evidence/ev-full"))
    assert full["license"] == "fulltext_allowed"
    assert full["excerpt"] == "F" * 2000
    assert len(str(full["excerpt"])) > 280
    assert FULL_CUT not in json.dumps(full)
    detail = _obj(_get(client, "/v1/trends/trend-a"))
    assert set(detail) == {
        "trend",
        "features",
        "evidence_ids",
        "contrary_ids",
        "as_of",
        "schema_version",
    }
    assert detail["evidence_ids"] == ["ev-ok"]
    assert detail["contrary_ids"] == ["ev-contrary"]
    trend = detail["trend"]
    assert isinstance(trend, dict)
    assert trend["supporting_evidence_ids"] == ["ev-ok"]
    assert trend["contrary_evidence_ids"] == ["ev-contrary"]
    assert trend["claim"] == "RAG records increased"
    assert trend["numeric_checks_passed"] is True
    assert trend["last_verified_at"] == "2026-10-27T00:00:01Z"
    features = detail["features"]
    assert isinstance(features, dict)
    assert features["claim"] == "RAG records increased"
    rendered = json.dumps(detail)
    assert FEATURE_TAIL not in rendered
    assert FULL_TEXT not in rendered
    assert BLOCKED_EXCERPT not in rendered


def test_signed_cursor_pagination(client: TestClient) -> None:
    first = _obj(_get(client, "/v1/trends", {"topic": "RAG", "window": "7", "limit": "2"}))
    assert _ids(first) == ["trend-a", "trend-b"]
    assert first["next_cursor"]
    assert "test-cursor-key" not in json.dumps(first)
    cursor = first["next_cursor"]
    assert isinstance(cursor, str)
    second = _obj(
        _get(
            client,
            "/v1/trends",
            {"topic": "RAG", "window": "7", "limit": "2", "cursor": cursor},
        )
    )
    assert _ids(second) == ["trend-c"]
    assert second["next_cursor"] is None
    replay = _obj(
        _get(
            client,
            "/v1/trends",
            {"topic": "RAG", "window": "7", "limit": "2", "cursor": cursor},
        )
    )
    assert replay["items"] == second["items"]
    # The last base64 character of a SHA-256 digest can change only unused bits.
    body, mac = cursor.split(".", 1)
    flipped = ("A" if mac[0] != "A" else "B") + mac[1:]
    tampered = body + "." + flipped
    invalid = _obj(
        _get(
            client,
            "/v1/trends",
            {"topic": "RAG", "window": "7", "cursor": tampered},
        ),
        400,
    )
    assert invalid["code"] == "invalid_cursor"
    mismatched = _obj(
        _get(client, "/v1/trends", {"topic": "LLM", "window": "7", "cursor": cursor}),
        400,
    )
    assert mismatched["code"] == "invalid_cursor"
    forged = sign_cursor(
        key="other-test-key",
        kind="trends",
        query={"topic": "RAG", "window": "7"},
        after_sort="2026-10-27T00:00:01Z",
        after_id="trend-a",
        now=time.time(),
        ttl=3600,
    )
    assert (
        _get(client, "/v1/trends", {"topic": "RAG", "window": "7", "cursor": forged}).status_code
        == 400
    )
    expired = sign_cursor(
        key="test-cursor-key",
        kind="trends",
        query={"topic": "", "window": ""},
        after_sort="2026-10-27T00:00:01Z",
        after_id="trend-a",
        now=0,
        ttl=1,
    )
    assert _obj(_get(client, "/v1/trends", {"cursor": expired}), 400)["code"] == "invalid_cursor"
    reports = _obj(_get(client, "/v1/reports", {"kind": "weekly", "limit": "1"}))
    assert _ids(reports, "report_id") == ["report-w1"]
    report_cursor = reports["next_cursor"]
    assert isinstance(report_cursor, str)
    rest = _obj(
        _get(client, "/v1/reports", {"kind": "weekly", "limit": "1", "cursor": report_cursor})
    )
    assert _ids(rest, "report_id") == ["report-w2"]
    assert rest["next_cursor"] is None
    assert _get(client, "/v1/trends", {"cursor": report_cursor}).status_code == 400


def test_invalid_cursor_limit_and_filters_are_400(client: TestClient) -> None:
    for raw in ("0", "51", "-1", "abc", "1.5", "01", "1e1", "+1", " 1"):
        body = _obj(_get(client, "/v1/trends", {"limit": raw}), 400)
        assert body["code"] == "invalid_limit"
        _assert_error(body)
    for raw in ("8", "0", "7.0", "seven", ""):
        assert _obj(_get(client, "/v1/trends", {"window": raw}), 400)["code"] == "invalid_window"
    assert _obj(_get(client, "/v1/reports", {"kind": "monthly"}), 400)["code"] == "invalid_kind"
    assert _obj(_get(client, "/v1/trends", {"topic": ""}), 400)["code"] == "invalid_topic"
    assert _obj(_get(client, "/v1/trends", {"topic": "R" * 201}), 400)["code"] == "invalid_topic"
    assert _obj(_get(client, "/v1/trends", {"cursor": ""}), 400)["code"] == "invalid_cursor"
    assert _obj(_get(client, "/v1/trends", {"cursor": "x" * 3000}), 400)["code"] == "invalid_cursor"
    assert (
        _obj(_get(client, "/v1/trends", {"cursor": "not-a-cursor"}), 400)["code"]
        == "invalid_cursor"
    )
    assert _obj(_get(client, "/v1/trends/" + ("n" * 257)), 400)["code"] == "invalid_id"
    injected = _obj(_get(client, "/v1/trends", {"topic": "' OR '1'='1"}))
    assert injected["items"] == []
    rag = _obj(_get(client, "/v1/trends", {"topic": "RAG"}))
    assert _ids(rag) == ["trend-a", "trend-b", "trend-c", "trend-28"]
    assert _ids(_obj(_get(client, "/v1/trends", {"window": "28"}))) == ["trend-28"]
    assert _ids(_obj(_get(client, "/v1/trends", {"topic": "LLM", "window": "7"}))) == ["trend-llm"]
    assert _ids(_obj(_get(client, "/v1/reports", {"kind": "daily"})), "report_id") == [
        "report-daily"
    ]
    assert _ids(_obj(_get(client, "/v1/reports", {"kind": "topic"})), "report_id") == [
        "report-topic"
    ]
    page = _obj(_get(client, "/v1/trends", {"limit": "50"}))
    assert len(_ids(page)) == 7
    assert page["next_cursor"] is None


def test_report_detail_and_filters(client: TestClient) -> None:
    body = _obj(_get(client, "/v1/reports/report-w1"))
    assert set(body) == {"content", "evidence", "generated_by_ai", "as_of", "schema_version"}
    assert body["content"] == "weekly report one"
    assert body["generated_by_ai"] is True
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    assert [card["evidence_id"] for card in evidence] == [
        "ev-contrary",
        "ev-full",
        "ev-long",
        "ev-ok",
    ]
    rendered = json.dumps(body)
    for secret in (FULL_TEXT, TAIL, FULL_CUT, FAILED_BODY, BLOCKED_EXCERPT):
        assert secret not in rendered
    other = _obj(_get(client, "/v1/reports/report-w2"))
    assert other["content"] == "weekly report two"
    assert other["generated_by_ai"] is False
    _assert_meta(body)
    _assert_meta(other)


def test_metrics_are_internal_only(seed: Seed) -> None:
    external = TestClient(seed.app)
    denied = _obj(external.get("/metrics"), 403)
    assert denied["code"] == "forbidden"
    still = external.get("/metrics", headers={"Authorization": "Bearer reader-token"})
    assert still.status_code == 403
    internal = TestClient(seed.app, client=("127.0.0.1", 50000))
    ok = internal.get("/metrics")
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain")
    assert "techpulse_api_up 1" in ok.text
    assert "# schema_version: 1.0" in ok.text
    stamped = re.search(r"# as_of: (\S+)", ok.text)
    assert stamped is not None
    assert UTC_TEXT.fullmatch(stamped.group(1))
    private = TestClient(seed.app, client=("10.1.2.3", 50000))
    assert private.get("/metrics").status_code == 200
    public = TestClient(seed.app, client=("8.8.8.8", 50000))
    assert public.get("/metrics").status_code == 403


def test_read_token_anonymous_threshold_and_rotation(seed: Seed) -> None:
    app = create_app(
        _settings(
            seed.root, seed.database, anon_limit=1, tokens=("reader-token", "reader-token-rotated")
        )
    )
    with TestClient(app) as client:
        assert _get(client, "/v1/trends", token=None).status_code == 200
        blocked = _obj(_get(client, "/v1/trends", token=None), 401)
        assert blocked["code"] == "unauthorized"
        assert _get(client, "/v1/trends", token="not-the-token").status_code == 401
        assert _get(client, "/v1/trends", token="reader-token").status_code == 200
        assert _get(client, "/v1/trends", token="reader-token-rotated").status_code == 200
        basic = client.get("/v1/trends", headers={"Authorization": "Basic cmVhZGVy"})
        assert basic.status_code == 401


def test_rate_limit(seed: Seed) -> None:
    app = create_app(_settings(seed.root, seed.database, rate_limit=2))
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/live").status_code == 200
        limited = _obj(client.get("/health/live"), 429)
        assert limited["code"] == "rate_limited"
        assert REQUEST_ID.fullmatch(str(limited["request_id"]))


def test_request_ids(client: TestClient) -> None:
    sent = "req-12345678"
    missing = _get(client, "/v1/trends/missing-trend", headers={"X-Request-ID": sent})
    body = _obj(missing, 404)
    assert missing.headers["X-Request-ID"] == sent
    assert body["request_id"] == sent
    assert missing.headers["X-Schema-Version"] == "1.0"
    replaced = client.get("/health/live", headers={"X-Request-ID": "bad"})
    assert replaced.status_code == 200
    generated = replaced.headers["X-Request-ID"]
    assert generated != "bad"
    assert REQUEST_ID.fullmatch(generated)


def test_health_and_ready(seed: Seed, client: TestClient) -> None:
    live = _obj(client.get("/health/live"))
    assert live["status"] == "ok"
    _assert_meta(live)
    ready = _obj(client.get("/health/ready"))
    assert ready["status"] == "ok"
    assert ready["checks"] == {"database": "ok", "object_store": "ok"}
    offline = create_app(_settings(seed.root, seed.root / "missing.sqlite"))
    with TestClient(offline) as broken:
        assert _obj(broken.get("/health/live"))["status"] == "ok"
        failed = _obj(broken.get("/health/ready"), 503)
        assert failed["status"] == "unavailable"
        assert failed["checks"] == {"database": "fail", "object_store": "ok"}
        unavailable = _obj(_get(broken, "/v1/trends"), 503)
        assert unavailable["code"] == "unavailable"


def test_cursor_key_is_required_to_sign(seed: Seed) -> None:
    app = create_app(_settings(seed.root, seed.database, cursor_key=""))
    with TestClient(app) as client:
        page = _obj(_get(client, "/v1/trends", {"limit": "50"}))
        assert page["next_cursor"] is None
        failed = _obj(_get(client, "/v1/trends", {"cursor": "abc.def"}), 503)
        assert failed["code"] == "unavailable"


def test_cors_allowlist(client: TestClient) -> None:
    allowed = client.get("/health/live", headers={"Origin": "https://reader.example"})
    assert allowed.headers["access-control-allow-origin"] == "https://reader.example"
    blocked = client.get("/health/live", headers={"Origin": "https://evil.example"})
    assert blocked.headers.get("access-control-allow-origin") in {None, ""}
    preflight = client.options(
        "/v1/trends",
        headers={
            "Origin": "https://reader.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert preflight.status_code == 200
    allow_methods = preflight.headers["access-control-allow-methods"]
    assert "GET" in allow_methods
    assert "POST" not in allow_methods


def test_no_write_endpoints_and_readonly_sql(seed: Seed, client: TestClient) -> None:
    assert client.post("/v1/trends").status_code == 405
    assert client.put("/v1/reports/report-w1").status_code == 405
    assert client.patch("/v1/evidence/ev-ok").status_code == 405
    assert client.delete("/v1/reports/report-w1").status_code == 405
    before = seed.database.read_bytes()
    assert _get(client, "/v1/trends").status_code == 200
    assert _get(client, "/v1/reports/report-w1").status_code == 200
    assert _get(client, "/v1/evidence/ev-ok").status_code == 200
    assert seed.database.read_bytes() == before
    connection = open_readonly(seed.database)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO published_reports ("
                "report_id, report_kind, content_uri, qa_manifest_uri, "
                "evidence_snapshot_uri, published_at, generated_by_ai"
                ") VALUES ('x', 'daily', 'u', 'q', 'e', '2026-10-27T00:00:00Z', 1)"
            )
    finally:
        connection.close()


def test_settings_read_the_process_environment(
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "TECHPULSE_API_READ_TOKENS",
        "TECHPULSE_CURSOR_HMAC_KEY",
        "TECHPULSE_ANON_LIMIT",
        "TECHPULSE_RATE_LIMIT",
        "TECHPULSE_RATE_WINDOW_SECONDS",
        "TECHPULSE_CURSOR_TTL_SECONDS",
        "TECHPULSE_CORS_ORIGINS",
        "TECHPULSE_API_DATABASE",
        "TECHPULSE_ARTIFACTS_DIR",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert ApiSettings.from_env().cursor_key == ""
    monkeypatch.setenv("TECHPULSE_API_READ_TOKENS", "env-token-a, env-token-b")
    monkeypatch.setenv("TECHPULSE_CURSOR_HMAC_KEY", "env-cursor-key")
    monkeypatch.setenv("TECHPULSE_ANON_LIMIT", "4")
    monkeypatch.setenv("TECHPULSE_RATE_LIMIT", "9")
    monkeypatch.setenv("TECHPULSE_RATE_WINDOW_SECONDS", "30")
    monkeypatch.setenv("TECHPULSE_CURSOR_TTL_SECONDS", "120")
    monkeypatch.setenv("TECHPULSE_CORS_ORIGINS", "https://reader.example, *")
    monkeypatch.setenv("TECHPULSE_API_DATABASE", str(seed.database))
    monkeypatch.setenv("TECHPULSE_ARTIFACTS_DIR", str(seed.root))
    settings = ApiSettings.from_env()
    assert settings.read_tokens == ("env-token-a", "env-token-b")
    assert settings.cursor_key == "env-cursor-key"
    assert settings.anon_limit == 4
    assert settings.rate_limit == 9
    assert settings.rate_window_seconds == 30
    assert settings.cursor_ttl_seconds == 120
    assert settings.cors_origins == ("https://reader.example",)
    assert settings.database == seed.database
    assert settings.artifacts_dir == seed.root


def test_sources_have_no_baked_in_secret() -> None:
    root = ROOT / "src" / "techpulse" / "api"
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "load_dotenv" not in text
        assert "dotenv" not in text
        assert "httpx" not in text
        assert "boto3" not in text
        assert "psycopg" not in text
        assert "test-cursor-key" not in text
        assert "reader-token" not in text


def test_default_app_imports() -> None:
    from techpulse.api.main import app

    schema = app.openapi()
    paths = schema["paths"]
    assert isinstance(paths, dict)
    assert "/v1/evidence/{id}" in paths


@pytest.fixture(scope="module")
def seed() -> Iterator[Seed]:
    with TemporaryDirectory(prefix="techpulse-a10-") as raw:
        root = Path(raw)
        database = root / "catalog.sqlite"
        _build_catalog(root, database)
        yield Seed(root=root, database=database, app=create_app(_settings(root, database)))


@pytest.fixture
def client(seed: Seed) -> Iterator[TestClient]:
    with TestClient(seed.app) as test_client:
        yield test_client


def _settings(
    root: Path,
    database: Path,
    *,
    tokens: tuple[str, ...] = ("reader-token", "reader-token-rotated"),
    cursor_key: str = "test-cursor-key",
    anon_limit: int = 10_000,
    rate_limit: int = 10_000,
    cors: tuple[str, ...] = ("https://reader.example",),
) -> ApiSettings:
    return ApiSettings(
        read_tokens=tokens,
        cursor_key=cursor_key,
        anon_limit=anon_limit,
        rate_limit=rate_limit,
        rate_window_seconds=60.0,
        cursor_ttl_seconds=3600,
        cors_origins=cors,
        database=database,
        artifacts_dir=root,
    )


def _build_catalog(root: Path, database: Path) -> None:
    store = LocalObjectStore(root)
    uris: dict[str, dict[str, str]] = {}
    for spec in _reports():
        uris[spec.report_id] = {
            "content": store.put(f"fixture/{spec.report_id}.md", spec.content.encode("utf-8")),
            "qa": store.put(
                f"fixture/{spec.report_id}.qa.json",
                _bytes(
                    {
                        "qa_pass": spec.qa_pass,
                        "report_id": spec.report_id,
                        "signed": spec.signed,
                    }
                ),
            ),
            "snapshot": store.put(
                f"fixture/{spec.report_id}.evidence.json",
                _bytes(
                    {"evidence_ids": list(spec.evidence_ids), "trend_ids": list(spec.trend_ids)}
                ),
            ),
        }
    connection = sqlite3.connect(database)
    try:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        connection.execute("PRAGMA foreign_keys = ON")
        _seed(connection, uris)
        connection.commit()
    finally:
        connection.close()


def _seed(connection: sqlite3.Connection, uris: Mapping[str, Mapping[str, str]]) -> None:
    connection.execute(
        """
        INSERT INTO source_registry (
            source_id, source_family, base_url, policy_snapshot_id, enabled,
            requests_per_minute, checked_at
        ) VALUES ('openalex', 'academic', 'https://example.org', 'policy-test', 1, 10,
                  '2026-10-27T00:00:00Z')
        """
    )
    raw_ids: dict[str, int] = {}
    for item in _evidence():
        connection.execute(
            """
            INSERT INTO raw_records (
                source_id, source_record_id, canonical_url, title, abstract_or_excerpt,
                published_at, collected_at, content_sha256, license_status,
                policy_snapshot_id, raw_uri
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "openalex",
                item.evidence_id,
                f"https://example.org/records/{item.evidence_id}",
                item.evidence_id,
                FULL_TEXT,
                item.published_at,
                "2026-10-27T00:00:00Z",
                _digest(item.evidence_id),
                item.license_status,
                "policy-test",
                f"local:fixture/raw/{item.evidence_id}",
            ),
        )
        row_id = connection.execute("SELECT last_insert_rowid()").fetchone()
        assert row_id is not None
        raw_ids[item.evidence_id] = int(row_id[0])
        connection.execute(
            """
            INSERT INTO canonical_entities (canonical_id, entity_type, display_name, normalized_key)
            VALUES (?, 'record', ?, ?)
            """,
            (f"doc:{item.evidence_id}", item.evidence_id, f"doc:{item.evidence_id}"),
        )
        connection.execute(
            """
            INSERT INTO evidence_items (
                evidence_id, canonical_id, source_record_id, source_family, excerpt,
                quality_tier, license_status, embedding_status, sha256
            ) VALUES (?, ?, ?, 'academic', ?, 'A', ?, 'pending', ?)
            """,
            (
                item.evidence_id,
                f"doc:{item.evidence_id}",
                raw_ids[item.evidence_id],
                item.excerpt,
                item.license_status,
                _digest(item.evidence_id),
            ),
        )
    for trend in _trends():
        features: dict[str, object] = {
            "claim": trend.claim,
            "numeric_checks_passed": True,
        }
        if trend.note is not None:
            features["note"] = trend.note
        connection.execute(
            """
            INSERT INTO trend_scores (
                trend_id, topic, window_days, score, confidence, scored_at,
                features_json, source_families
            ) VALUES (?, ?, ?, ?, 'medium', ?, ?, ?)
            """,
            (
                trend.trend_id,
                trend.topic,
                trend.window_days,
                trend.score,
                trend.scored_at,
                json.dumps(features, ensure_ascii=False, sort_keys=True),
                json.dumps(["academic"], sort_keys=True),
            ),
        )
    connection.executemany(
        """
        INSERT INTO trend_evidence (trend_id, evidence_id, relation)
        VALUES (?, ?, ?)
        """,
        (
            ("trend-a", "ev-ok", "support"),
            ("trend-a", "ev-contrary", "contradict"),
            ("trend-a", "ev-blocked", "contradict"),
        ),
    )
    for spec in _reports():
        stored = uris[spec.report_id]
        connection.execute(
            """
            INSERT INTO published_reports (
                report_id, report_kind, content_uri, qa_manifest_uri,
                evidence_snapshot_uri, published_at, generated_by_ai
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                spec.report_id,
                spec.report_kind,
                stored["content"],
                stored["qa"],
                stored["snapshot"],
                spec.published_at,
                1 if spec.generated_by_ai else 0,
            ),
        )


def _evidence() -> tuple[_Evidence, ...]:
    return (
        _Evidence("ev-ok", "metadata_only", "public excerpt for rag", "2026-10-26T00:00:00Z"),
        _Evidence(
            "ev-contrary", "reusable_excerpt", "contrary public note", "2026-10-26T00:00:00Z"
        ),
        _Evidence("ev-blocked", "unknown", BLOCKED_EXCERPT, "2026-10-26T00:00:00Z"),
        _Evidence("ev-long", "metadata_only", ("L" * 280) + TAIL, None),
        _Evidence("ev-full", "fulltext_allowed", ("F" * 2000) + FULL_CUT, "2026-10-26T00:00:00Z"),
        _Evidence("ev-secret", "metadata_only", "secret evidence excerpt", "2026-10-26T00:00:00Z"),
        _Evidence(
            "ev-unsigned", "metadata_only", "unsigned evidence excerpt", "2026-10-26T00:00:00Z"
        ),
    )


def _trends() -> tuple[_Trend, ...]:
    note = ("n" * 2000) + FEATURE_TAIL
    return (
        _Trend("trend-a", "RAG", 7, 0.5, "2026-10-27T00:00:01Z", "RAG records increased", note),
        _Trend("trend-b", "RAG", 7, 0.25, "2026-10-27T00:00:02Z", "RAG records held"),
        _Trend("trend-c", "RAG", 7, 0.75, "2026-10-27T00:00:03Z", "RAG records split"),
        _Trend("trend-28", "RAG", 28, 0.5, "2026-10-27T00:00:04Z", "RAG month held"),
        _Trend("trend-llm", "LLM", 7, 0.5, "2026-10-27T00:00:05Z", "LLM records increased"),
        _Trend("trend-daily", "OPS", 7, 0.5, "2026-10-27T00:00:06Z", "OPS records increased"),
        _Trend("trend-topic", "TOPIC", 7, 0.5, "2026-10-27T00:00:07Z", "TOPIC records increased"),
        _Trend("trend-hidden", "RAG", 7, 0.5, "2026-10-27T00:00:08Z", "hidden trend claim"),
        _Trend("trend-secret", "RAG", 7, 0.5, "2026-10-27T00:00:09Z", "secret trend claim"),
        _Trend("trend-unsigned", "RAG", 7, 0.5, "2026-10-27T00:00:10Z", "unsigned trend claim"),
    )


def _reports() -> tuple[_Report, ...]:
    shared = ("ev-ok",)
    return (
        _Report(
            "report-w1",
            "weekly",
            "2026-10-27T01:00:00Z",
            True,
            "weekly report one",
            True,
            True,
            ("trend-a", "trend-b", "trend-c", "trend-28", "trend-llm"),
            ("ev-ok", "ev-contrary", "ev-blocked", "ev-long", "ev-full"),
        ),
        _Report(
            "report-w2",
            "weekly",
            "2026-10-27T02:00:00Z",
            False,
            "weekly report two",
            True,
            True,
            (),
            shared,
        ),
        _Report(
            "report-daily",
            "daily",
            "2026-10-27T03:00:00Z",
            True,
            "daily report body",
            True,
            True,
            ("trend-daily",),
            shared,
        ),
        _Report(
            "report-topic",
            "topic",
            "2026-10-27T04:00:00Z",
            True,
            "topic report body",
            True,
            True,
            ("trend-topic",),
            shared,
        ),
        _Report(
            "report-fail",
            "weekly",
            "2026-10-27T05:00:00Z",
            True,
            FAILED_BODY,
            False,
            False,
            ("trend-secret",),
            ("ev-secret",),
        ),
        _Report(
            "report-unsigned",
            "weekly",
            "2026-10-27T06:00:00Z",
            True,
            UNSIGNED_BODY,
            True,
            False,
            ("trend-unsigned",),
            ("ev-unsigned",),
        ),
    )


def _get(
    client: TestClient,
    path: str,
    params: dict[str, str] | None = None,
    *,
    token: str | None = "reader-token",
    headers: dict[str, str] | None = None,
) -> Response:
    merged: dict[str, str] = {}
    if token is not None:
        merged["Authorization"] = f"Bearer {token}"
    if headers:
        merged.update(headers)
    return cast(Response, client.get(path, params=params, headers=merged))


def _obj(response: Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    payload: object = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _ids(body: dict[str, Any], key: str = "trend_id") -> list[str]:
    items = body["items"]
    assert isinstance(items, list)
    found: list[str] = []
    for item in items:
        assert isinstance(item, dict)
        value = item[key]
        assert isinstance(value, str)
        found.append(value)
    return found


def _assert_meta(body: dict[str, Any]) -> None:
    assert body["schema_version"] == "1.0"
    assert isinstance(body["as_of"], str)
    assert UTC_TEXT.fullmatch(body["as_of"])


def _assert_error(body: dict[str, Any]) -> None:
    assert set(body) == {"code", "message", "request_id", "as_of", "schema_version"}
    _assert_meta(body)
    assert isinstance(body["request_id"], str)
    assert REQUEST_ID.fullmatch(body["request_id"])


def _query_names(path_item: object) -> set[str]:
    assert isinstance(path_item, dict)
    operation = path_item["get"]
    assert isinstance(operation, dict)
    params = operation.get("parameters", [])
    assert isinstance(params, list)
    names: set[str] = set()
    for item in params:
        assert isinstance(item, dict)
        if item.get("in") == "query":
            name = item["name"]
            assert isinstance(name, str)
            names.add(name)
    return names


def _status_codes(path_item: object) -> set[str]:
    assert isinstance(path_item, dict)
    operation = path_item["get"]
    assert isinstance(operation, dict)
    responses = operation["responses"]
    assert isinstance(responses, dict)
    return {str(code) for code in responses}


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
