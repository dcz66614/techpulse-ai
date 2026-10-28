"""O10 release manifest, gate, and local smoke checks.

Tests use tempfile. They do not build an image, open a port, or read an env
file. The sealed retrieval test set is not used.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.migration_dry_run import main as migration_main
from scripts.release_manifest import main as release_main
from scripts.smoke_test import main as smoke_main

from techpulse.ops.release import (
    IMAGE_DIGEST,
    MODEL_VERSION,
    POLICY_VERSION,
    auth02_granted,
    evaluate_gate,
    migration_dry_run,
    read_qa_pass,
    repository_root,
    run_smoke,
    write_release,
)

ROOT = repository_root()
_DIGEST = re.compile(r"sha256:[0-9a-fA-F]{64}")


def test_default_config_is_not_granted() -> None:
    """The committed deploy config does not grant AUTH-02."""
    assert auth02_granted() is False
    assert auth02_granted(ROOT / "config" / "missing-deploy.yaml") is False


def test_only_boolean_true_grants_auth02() -> None:
    """A string or integer true does not grant AUTH-02."""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        granted = base / "yes.yaml"
        granted.write_text("auth02:\n  granted: true\n", encoding="utf-8")
        text = base / "text.yaml"
        text.write_text("auth02:\n  granted: 'true'\n", encoding="utf-8")
        number = base / "number.yaml"
        number.write_text("auth02:\n  granted: 1\n", encoding="utf-8")
        assert auth02_granted(granted) is True
        assert auth02_granted(text) is False
        assert auth02_granted(number) is False


def test_gate_refuses_unless_qa_pass_and_auth02() -> None:
    """Production and staging deploys both need qa_pass and AUTH-02."""
    for target in ("staging", "production"):
        no_auth = evaluate_gate(target, qa_pass=True, auth02_granted=False)
        no_qa = evaluate_gate(target, qa_pass=False, auth02_granted=True)
        both = evaluate_gate(target, qa_pass=True, auth02_granted=True)
        assert no_auth.allowed is False
        assert no_auth.staging_only is True
        assert "AUTH-02 is not granted" in no_auth.reason
        assert no_qa.allowed is False
        assert no_qa.reason == "Q10 qa_pass is false"
        assert both.allowed is True
        assert both.staging_only is False
    production = evaluate_gate("production", qa_pass=True, auth02_granted=False)
    assert "production refused" in production.reason
    closed = evaluate_gate("production", qa_pass=False, auth02_granted=False)
    assert closed.allowed is False
    assert "production refused" in closed.reason
    assert closed.staging_only is True
    unknown = evaluate_gate("prod", qa_pass=True, auth02_granted=True)
    assert unknown.allowed is False


def test_qa_pass_must_be_boolean_true() -> None:
    """A string true in a QA manifest does not pass the gate."""
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "qa.json"
        path.write_text('{"qa_pass": "true"}\n', encoding="utf-8")
        assert read_qa_pass(path) is False
        path.write_text('{"qa_pass": true}\n', encoding="utf-8")
        assert read_qa_pass(path) is True
    assert read_qa_pass(None) is False


def test_migration_dry_run_on_temporary_sqlite() -> None:
    """Alembic head applies to a temporary SQLite file and leaves alembic.ini alone."""
    ini = ROOT / "alembic.ini"
    before = ini.read_bytes()
    with TemporaryDirectory() as tmp:
        database = Path(tmp) / "dry-run.sqlite"
        result = migration_dry_run(database)
        assert database.is_file()
        connection = sqlite3.connect(database)
        try:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            connection.close()
    assert ini.read_bytes() == before
    assert result.ok is True
    assert result.revision == "0002"
    assert revision == ("0002",)
    assert "published_reports" in result.tables
    assert "index_versions" in result.tables
    assert result.missing_tables == ()


def test_smoke_checks_health_endpoints() -> None:
    """TestClient gets a live and ready response from the migrated app."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        database = root / "app.sqlite"
        migration = migration_dry_run(database)
        smoke = run_smoke(database, root / "objects")
    assert migration.ok is True
    assert smoke.ok is True
    assert smoke.live_status == 200
    assert smoke.ready_status == 200
    assert smoke.errors == ()


def test_smoke_fails_when_schema_is_missing() -> None:
    """An empty SQLite file fails the ready check."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        database = root / "empty.sqlite"
        sqlite3.connect(database).close()
        smoke = run_smoke(database, root / "objects")
    assert smoke.ok is False
    assert smoke.live_status == 200
    assert smoke.ready_status == 503


def test_manifest_records_versions_and_pending_digest() -> None:
    """The release manifest pins versions and does not invent an image digest."""
    with TemporaryDirectory() as tmp:
        artifacts = Path(tmp)
        prep = write_release(run_id="o10-unit", artifacts_dir=artifacts)
        directory = prep.directory
        release = json.loads((directory / "release_manifest.json").read_text(encoding="utf-8"))
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        metrics = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
    assert prep.image_digest == IMAGE_DIGEST
    assert prep.production_allowed is False
    assert prep.status == "staging_only"
    assert release["image_digest"] == "pending-AUTH-02"
    assert release["base_image"] == "python:3.12-slim@sha256:<PINNED_DIGEST>"
    assert release["commit"]
    assert release["model_version"] == MODEL_VERSION
    assert release["policy_version"] == POLICY_VERSION
    assert release["schema_version"] == "1.0"
    assert release["source_registry_version"] == "1.0.0"
    assert release["migration_revision"] == "0002"
    assert release["auth02_granted"] is False
    assert release["scope"] == "staging-only"
    assert release["deployed"] is False
    assert release["docker_invoked"] is False
    assert release["gates"]["production"]["allowed"] is False
    assert release["gates"]["staging"]["allowed"] is False
    assert _DIGEST.search(json.dumps(release)) is None
    assert metrics["smoke_pass"] is True
    assert metrics["migration_dry_run_pass"] is True
    assert metrics["network_calls"] == 0
    assert manifest["ticket"] == "O10"
    assert manifest["status"] == "staging_only"
    names = {Path(str(item["path"])).name for item in manifest["artifacts"]}
    assert names == {"release_manifest.json", "metrics.json", "log.jsonl"}


def test_open_gate_still_does_not_deploy() -> None:
    """A granted config and a passing QA manifest still leave the digest pending."""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        config = base / "deploy.yaml"
        config.write_text("auth02:\n  granted: true\n", encoding="utf-8")
        qa = base / "qa.json"
        qa.write_text('{"qa_pass": true}\n', encoding="utf-8")
        prep = write_release(
            run_id="o10-open",
            artifacts_dir=base / "artifacts",
            qa_manifest=qa,
            config_path=config,
        )
        release = json.loads((prep.directory / "release_manifest.json").read_text(encoding="utf-8"))
    assert prep.status == "not_deployed"
    assert prep.production_allowed is True
    assert release["image_digest"] == "pending-AUTH-02"
    assert release["deployed"] is False
    assert release["docker_invoked"] is False
    assert release["gates"]["production"]["allowed"] is True


def test_scripts_run_without_deploying() -> None:
    """The three CLIs pass against temporary files."""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        database = base / "cli.sqlite"
        assert migration_main(["--database", str(database)]) == 0
        assert smoke_main(["--database", str(database)]) == 0
        assert release_main(["--run-id", "o10-cli", "--artifacts-dir", str(base)]) == 0
        written = json.loads(
            (base / "o10-cli" / "O10" / "release_manifest.json").read_text(encoding="utf-8")
        )
    assert written["image_digest"] == "pending-AUTH-02"


def test_dockerfile_and_staging_templates_have_no_digest() -> None:
    """The image reference is a placeholder, and the process user is not root."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "<PINNED_DIGEST>" in dockerfile
    assert "uv.lock" in dockerfile
    assert "\nUSER techpulse\n" in dockerfile
    assert _DIGEST.search(dockerfile) is None
    staging = ROOT / "infra" / "staging"
    combined = "\n".join(path.read_text(encoding="utf-8") for path in staging.glob("*.yaml"))
    assert "pending-AUTH-02" in combined
    assert "pending-auth-02.invalid/techpulse@pending-AUTH-02" in combined
    assert "type: ClusterIP" in combined
    assert _DIGEST.search(combined) is None
    readme = (ROOT / "infra" / "README.md").read_text(encoding="utf-8")
    assert "AUTH-02" in readme
    blocked = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
    assert "Still blocked on AUTH-02" in blocked
    assert "<PINNED_DIGEST>" in blocked
