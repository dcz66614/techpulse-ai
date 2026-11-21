"""O10 release preparation. No image build, registry, or deploy.

AUTH-02 is read from ``config/deploy.yaml``. Missing or not boolean true
means not granted. That scope is staging-only, and the gate refuses
production. A deploy is allowed only when Q10 ``qa_pass`` is true and
AUTH-02 is granted. This module still does not build or push: the image
digest stays ``pending-AUTH-02``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess  # nosec B404  # only fixed argv, no shell
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import yaml
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from techpulse.api.auth import SCHEMA_VERSION, ApiSettings
from techpulse.api.main import create_app
from techpulse.reports.draft import PROMPT_MODEL_VERSION as DRAFT_PROMPT_MODEL
from techpulse.reports.render import PROMPT_MODEL_VERSION as RENDER_PROMPT_MODEL
from techpulse.workflows.activities import LocalObjectStore

IMAGE_DIGEST = "pending-AUTH-02"
LOCAL_IMAGE_DIGEST = "N/A (local deployment, no container)"
BASE_IMAGE = "python:3.12-slim@sha256:<PINNED_DIGEST>"
POLICY_VERSION = "policy-2026-10-27"
MODEL_VERSION = "none"
TICKET = "O10"
_EXPECTED_TABLES = frozenset(
    {
        "alembic_version",
        "canonical_entities",
        "chunk_vectors",
        "chunk_vectors_staging",
        "evidence_items",
        "index_pointer",
        "index_versions",
        "published_reports",
        "raw_records",
        "source_registry",
        "task_runs",
        "trend_evidence",
        "trend_scores",
    }
)


@dataclass(frozen=True)
class GateDecision:
    """Deploy authorization for one target."""

    allowed: bool
    target: str
    reason: str
    staging_only: bool
    qa_pass: bool
    auth02_granted: bool

    def to_dict(self) -> dict[str, bool | str]:
        return {
            "allowed": self.allowed,
            "auth02_granted": self.auth02_granted,
            "qa_pass": self.qa_pass,
            "reason": self.reason,
            "staging_only": self.staging_only,
            "target": self.target,
        }


@dataclass(frozen=True)
class MigrationDryRun:
    """Result of applying Alembic head to one temporary SQLite file."""

    ok: bool
    revision: str | None
    tables: tuple[str, ...]
    missing_tables: tuple[str, ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "dialect": "sqlite",
            "errors": list(self.errors),
            "missing_tables": list(self.missing_tables),
            "ok": self.ok,
            "revision": self.revision,
            "tables": list(self.tables),
        }


@dataclass(frozen=True)
class SmokeResult:
    """Local health check of the A10 ASGI app. No socket is opened."""

    ok: bool
    live_status: int | None
    ready_status: int | None
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "errors": list(self.errors),
            "live_status": self.live_status,
            "ok": self.ok,
            "ready_status": self.ready_status,
            "transport": "fastapi.TestClient",
        }


@dataclass(frozen=True)
class ReleasePrep:
    """Files written under ``artifacts/{run_id}/O10``."""

    directory: Path
    status: str
    smoke_ok: bool
    migration_ok: bool
    production_allowed: bool
    image_digest: str


def repository_root() -> Path:
    """Return the repository root that contains this package."""
    return Path(__file__).resolve().parents[3]


def auth02_granted(config_path: Path | None = None) -> bool:
    """Return True only when config sets ``auth02.granted`` to boolean true.
    A missing file, a missing key, or any non-boolean value is not granted.
    """
    path = _config_path(config_path)
    if not path.is_file():
        return False
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return False
    raw = cast(dict[object, object], loaded)
    auth = raw.get("auth02")
    if not isinstance(auth, dict):
        return False
    granted = cast(dict[object, object], auth).get("granted")
    return granted is True


def read_qa_pass(path: Path | None) -> bool:
    """Return True only when a Q10 manifest has boolean ``qa_pass`` true.
    ``None`` means the caller did not supply a manifest, which fails closed.
    A path that is not a file, or JSON that is not an object, is an error.
    """
    if path is None:
        return False
    if not path.is_file():
        raise ValueError("qa manifest is not a file")
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("qa manifest must be an object")
    return cast(dict[object, object], loaded).get("qa_pass") is True


def evaluate_gate(target: str, *, qa_pass: bool, auth02_granted: bool) -> GateDecision:
    """Refuse a deploy unless Q10 passed and AUTH-02 is granted.
    AUTH-02 grants local single-machine deployment on a separate always-on Linux box
    (x86_64, Python via uv, no Docker, no Postgres) only.
    Any public, cloud, or external target is strictly refused.
    When target is 'production', production means 'local v1.0' only.
    """
    normalized = target.strip().lower()
    staging_only = not auth02_granted
    if normalized in {"public", "cloud", "external", "remote"}:
        return GateDecision(
            allowed=False,
            target=normalized,
            reason="public deployment refused; local single-machine v1.0 only",
            staging_only=staging_only,
            qa_pass=qa_pass,
            auth02_granted=auth02_granted,
        )
    if normalized not in {"staging", "production", "local", "local_v1.0", "local-v1.0"}:
        return GateDecision(
            allowed=False,
            target=normalized,
            reason="unknown release target",
            staging_only=staging_only,
            qa_pass=qa_pass,
            auth02_granted=auth02_granted,
        )
    if not qa_pass and not auth02_granted:
        if normalized == "production":
            reason = (
                "Q10 qa_pass is false; AUTH-02 is not granted; staging-only; production refused"
            )
        elif normalized in {"local", "local_v1.0", "local-v1.0"}:
            reason = "Q10 qa_pass is false; AUTH-02 is not granted; local release refused"
        else:
            reason = (
                "Q10 qa_pass is false; AUTH-02 is not granted; "
                "staging templates only; deploy refused"
            )
        return GateDecision(
            allowed=False,
            target=normalized,
            reason=reason,
            staging_only=True,
            qa_pass=qa_pass,
            auth02_granted=auth02_granted,
        )
    if not qa_pass:
        return GateDecision(
            allowed=False,
            target=normalized,
            reason="Q10 qa_pass is false",
            staging_only=staging_only,
            qa_pass=qa_pass,
            auth02_granted=auth02_granted,
        )
    if not auth02_granted:
        if normalized == "production":
            reason = "AUTH-02 is not granted; staging-only; production refused"
        elif normalized in {"local", "local_v1.0", "local-v1.0"}:
            reason = "AUTH-02 is not granted; local release refused"
        else:
            reason = "AUTH-02 is not granted; staging templates only; deploy refused"
        return GateDecision(
            allowed=False,
            target=normalized,
            reason=reason,
            staging_only=True,
            qa_pass=qa_pass,
            auth02_granted=auth02_granted,
        )
    if normalized in {"local", "local_v1.0", "local-v1.0"}:
        return GateDecision(
            allowed=True,
            target=normalized,
            reason="local release gate passed (dedicated Linux box v1.0 authorized)",
            staging_only=False,
            qa_pass=qa_pass,
            auth02_granted=auth02_granted,
        )
    if normalized == "production":
        return GateDecision(
            allowed=True,
            target=normalized,
            reason=(
                "release gate passed (production authorized for local v1.0 only; public refused)"
            ),
            staging_only=False,
            qa_pass=qa_pass,
            auth02_granted=auth02_granted,
        )
    return GateDecision(
        allowed=True,
        target=normalized,
        reason="release gate passed",
        staging_only=False,
        qa_pass=qa_pass,
        auth02_granted=auth02_granted,
    )


def migration_dry_run(database: Path) -> MigrationDryRun:
    """Upgrade one SQLite file to Alembic head and leave it for the caller.
    The file is the only database this function opens. ``alembic.ini`` is
    not modified, and its PostgreSQL URL is not used.
    """
    if database.exists() and database.is_dir():
        return MigrationDryRun(False, None, (), (), ("database path is a directory",))
    database.parent.mkdir(parents=True, exist_ok=True)
    url = "sqlite:///" + database.resolve().as_posix()
    try:
        command.upgrade(_alembic_config(url), "head")
    except Exception as exc:
        return MigrationDryRun(False, None, (), (), (f"{type(exc).__name__}: {exc}",))
    revision, tables = _inspect_sqlite(database)
    missing = tuple(sorted(_EXPECTED_TABLES.difference(tables)))
    errors: list[str] = []
    if revision is None:
        errors.append("alembic_version is empty")
    if missing:
        errors.append("missing tables: " + ", ".join(missing))
    return MigrationDryRun(
        ok=not errors,
        revision=revision,
        tables=tuple(sorted(tables)),
        missing_tables=missing,
        errors=tuple(errors),
    )


def run_smoke(database: Path, artifacts_dir: Path) -> SmokeResult:
    """Check ``/health/live`` and ``/health/ready`` on the A10 app.
    The app is the object uvicorn serves (``techpulse.api.main:app`` via
    ``create_app``). ``TestClient`` runs it in process and does not bind a
    port. Settings are passed in. This function does not read an env file.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(
        ApiSettings(
            read_tokens=(),
            cursor_key="o10-local-smoke",
            anon_limit=100,
            rate_limit=100,
            rate_window_seconds=60.0,
            cursor_ttl_seconds=3600,
            cors_origins=(),
            database=database,
            artifacts_dir=artifacts_dir,
        ),
        LocalObjectStore(artifacts_dir),
    )
    with TestClient(app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
    errors: list[str] = []
    live_body = _object(live.json())
    ready_body = _object(ready.json())
    if live.status_code != 200 or _text(live_body, "status") != "ok":
        errors.append("live check failed")
    if _text(live_body, "schema_version") != SCHEMA_VERSION:
        errors.append("live schema_version is not 1.0")
    if ready.status_code != 200 or _text(ready_body, "status") != "ok":
        errors.append("ready check failed")
    checks = _checks(ready_body)
    if checks.get("database") != "ok" or checks.get("object_store") != "ok":
        errors.append("ready dependencies are not ok")
    return SmokeResult(
        ok=not errors,
        live_status=live.status_code,
        ready_status=ready.status_code,
        errors=tuple(errors),
    )


def write_release(
    *,
    run_id: str,
    artifacts_dir: Path,
    qa_manifest: Path | None = None,
    config_path: Path | None = None,
    image_digest: str | None = None,
) -> ReleasePrep:
    """Write the O10 manifest set. Does not build, push, or deploy."""
    checked = _run_id(run_id)
    root = repository_root()
    granted = auth02_granted(config_path)
    qa_pass = read_qa_pass(qa_manifest)
    staging = evaluate_gate("staging", qa_pass=qa_pass, auth02_granted=granted)
    production = evaluate_gate("production", qa_pass=qa_pass, auth02_granted=granted)
    local_gate = evaluate_gate("local", qa_pass=qa_pass, auth02_granted=granted)
    effective_digest = image_digest or (LOCAL_IMAGE_DIGEST if granted else IMAGE_DIGEST)
    with TemporaryDirectory(prefix="o10-") as tmp:
        base = Path(tmp)
        migration = migration_dry_run(base / "dry-run.sqlite")
        if migration.ok:
            smoke = run_smoke(base / "dry-run.sqlite", base / "objects")
        else:
            smoke = SmokeResult(False, None, None, ("migration dry-run failed",))
    status = _status(smoke.ok, migration.ok, granted)
    release: dict[str, object] = {
        "ticket": TICKET,
        "run_id": checked,
        "commit": git_commit(root),
        "model_version": MODEL_VERSION,
        "prompt_model_versions": {
            "draft": DRAFT_PROMPT_MODEL,
            "render": RENDER_PROMPT_MODEL,
        },
        "policy_version": POLICY_VERSION,
        "policy_sha256": _sha256(root / "config" / "policy.yaml"),
        "source_registry_version": _registry_version(root),
        "source_registry_sha256": _sha256(root / "config" / "source_registry.yaml"),
        "schema_version": SCHEMA_VERSION,
        "migration_revision": migration.revision,
        "base_image": BASE_IMAGE,
        "image_digest": effective_digest,
        "auth02_granted": granted,
        "scope": "local_v1_0_authorized" if granted else "staging-only",
        "qa_pass": qa_pass,
        "gates": {
            "local": local_gate.to_dict(),
            "production": production.to_dict(),
            "staging": staging.to_dict(),
        },
        "production_allowed": production.allowed,
        "staging_deploy_allowed": staging.allowed,
        "local_release_allowed": local_gate.allowed,
        "deployed": False,
        "docker_invoked": False,
        "registry_contacted": False,
        "smoke": smoke.to_dict(),
        "migration_dry_run": migration.to_dict(),
    }
    metrics: dict[str, object] = {
        "ticket": TICKET,
        "run_id": checked,
        "status": status,
        "auth02_granted": granted,
        "qa_pass": qa_pass,
        "production_allowed": production.allowed,
        "staging_deploy_allowed": staging.allowed,
        "local_release_allowed": local_gate.allowed,
        "smoke_pass": smoke.ok,
        "migration_dry_run_pass": migration.ok,
        "migration_revision": migration.revision,
        "image_built": False,
        "image_pushed": False,
        "deployed": False,
        "docker_invoked": False,
        "network_calls": 0,
        "third_party_writes": 0,
    }
    log = {
        "event": "o10_release_prep",
        "run_id": checked,
        "status": status,
        "image_digest": effective_digest,
        "smoke_pass": smoke.ok,
        "migration_dry_run_pass": migration.ok,
        "production_allowed": production.allowed,
        "local_release_allowed": local_gate.allowed,
        "deployed": False,
    }
    directory = artifacts_dir / checked / TICKET
    store = LocalObjectStore(artifacts_dir)
    prefix = f"{checked}/{TICKET}"
    store.put(f"{prefix}/release_manifest.json", _json_bytes(release))
    store.put(f"{prefix}/metrics.json", _json_bytes(metrics))
    store.put(f"{prefix}/log.jsonl", (json.dumps(log, sort_keys=True) + "\n").encode("utf-8"))
    manifest = {
        "ticket": TICKET,
        "run_id": checked,
        "status": status,
        "image_digest": effective_digest,
        "production_allowed": production.allowed,
        "local_release_allowed": local_gate.allowed,
        "deployed": False,
        "errors": list(smoke.errors + migration.errors),
        "artifacts": [
            _artifact(directory / "release_manifest.json", root),
            _artifact(directory / "metrics.json", root),
            _artifact(directory / "log.jsonl", root),
        ],
    }
    store.put(f"{prefix}/manifest.json", _json_bytes(manifest))
    return ReleasePrep(
        directory=directory,
        status=status,
        smoke_ok=smoke.ok,
        migration_ok=migration.ok,
        production_allowed=production.allowed,
        image_digest=effective_digest,
    )


def git_commit(root: Path) -> str:
    """Return HEAD, or ``unknown`` when git cannot be read."""
    completed = subprocess.run(  # nosec B603, B607  # fixed git argv, no shell, no user input
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        return "unknown"
    commit = completed.stdout.strip()
    if len(commit) == 40 and all(char in "0123456789abcdef" for char in commit):
        return commit
    return "unknown"


def _config_path(config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path
    return repository_root() / "config" / "deploy.yaml"


def _alembic_config(url: str) -> Config:
    root = repository_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _inspect_sqlite(database: Path) -> tuple[str | None, set[str]]:
    connection = sqlite3.connect(database)
    try:
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.Error:
            row = None
        names = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        connection.close()
    revision = row[0] if row is not None and isinstance(row[0], str) and row[0] else None
    tables = {name for (name,) in names if isinstance(name, str)}
    return revision, tables


def _registry_version(root: Path) -> str:
    loaded: object = yaml.safe_load(
        (root / "config" / "source_registry.yaml").read_text(encoding="utf-8")
    )
    if not isinstance(loaded, dict):
        return "unknown"
    version = cast(dict[object, object], loaded).get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()
    return "unknown"


def _status(smoke_ok: bool, migration_ok: bool, granted: bool) -> str:
    if not smoke_ok or not migration_ok:
        return "checks_failed"
    if not granted:
        return "staging_only"
    return "not_deployed"


def _run_id(run_id: str) -> str:
    if not run_id or run_id in {".", ".."} or any(char in run_id for char in "/\\:"):
        raise ValueError("invalid run_id")
    return run_id


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path, root: Path) -> dict[str, object]:
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = path.as_posix()
    return {"path": relative, "sha256": _sha256(path), "bytes": path.stat().st_size}


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _object(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _text(payload: Mapping[str, object] | None, key: str) -> str | None:
    if payload is None:
        return None
    value = payload.get(key)
    if isinstance(value, str):
        return value
    return None


def _checks(payload: Mapping[str, object] | None) -> dict[str, str]:
    if payload is None:
        return {}
    raw = payload.get("checks")
    if not isinstance(raw, dict):
        return {}
    found: dict[str, str] = {}
    for key, value in cast(dict[object, object], raw).items():
        if isinstance(key, str) and isinstance(value, str):
            found[key] = value
    return found
