"""Static mount checks. The read API stays read-only. No browser and no network."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from techpulse.api import main as api_main
from techpulse.api.auth import ApiSettings
from techpulse.api.main import create_app

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "src"
API_PATHS = {
    "/health/live",
    "/health/ready",
    "/v1/trends",
    "/v1/trends/{id}",
    "/v1/evidence/{id}",
    "/v1/reports",
    "/v1/reports/{id}",
    "/metrics",
}
WRITE = ("post", "put", "patch", "delete", "trace")


def _settings(root: Path) -> ApiSettings:
    database = root / "catalog.sqlite"
    database.write_bytes(b"")
    return ApiSettings(
        read_tokens=(),
        cursor_key="",
        anon_limit=30,
        rate_limit=120,
        rate_window_seconds=60.0,
        cursor_ttl_seconds=3600,
        cors_origins=(),
        database=database,
        artifacts_dir=root,
    )


def test_ui_mount_serves_files_and_api_stays_read_only(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite"
    app = create_app(_settings(tmp_path))
    before = database.read_bytes()
    assert any(getattr(route, "path", None) == "/ui" for route in app.routes)
    with TestClient(app) as client:
        page = client.get("/ui/")
        assert page.status_code == 200
        # StaticFiles returns the file bytes. read_text() rewrites CRLF to LF.
        assert page.content == (WEB / "index.html").read_bytes()
        assert "AI 生成" in page.text
        script = client.get("/ui/js/dashboard.js")
        style = client.get("/ui/css/app.css")
        assert script.status_code == 200
        assert style.status_code == 200
        assert script.content == (WEB / "js" / "dashboard.js").read_bytes()
        assert style.content == (WEB / "css" / "app.css").read_bytes()
        assert client.head("/ui/index.html").status_code == 200
        for method in WRITE:
            assert client.request(method, "/ui/").status_code == 405
            assert client.request(method, "/ui/index.html").status_code == 405
            denied = client.request(method, "/v1/trends")
            assert denied.status_code == 405
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json()["status"] == "ok"
        trends = client.get("/v1/trends")
        assert trends.status_code == 503
        assert trends.headers["content-type"].startswith("application/json")
        assert trends.json()["code"] == "unavailable"
        assert "主题榜" not in trends.text
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        assert isinstance(paths, dict)
        assert set(paths) == API_PATHS
        for item in paths.values():
            assert isinstance(item, dict)
            assert set(item).isdisjoint(WRITE)
        escaped = client.get("/ui/%2e%2e/%2e%2e/pyproject.toml")
        assert "hatchling" not in escaped.text
    assert database.read_bytes() == before


def test_missing_ui_directory_leaves_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> Path:
        return tmp_path / "missing-ui"

    monkeypatch.setattr(api_main, "ui_directory", missing)
    app = create_app(_settings(tmp_path))
    assert all(getattr(route, "path", None) != "/ui" for route in app.routes)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/ui/").status_code == 404
        assert client.get("/ui/index.html").status_code == 404
        paths = client.get("/openapi.json").json()["paths"]
        assert set(paths) == API_PATHS
