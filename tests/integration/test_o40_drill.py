"""Integration test for O40 backup, restore, and deletion drill."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.backup import main as backup_cli
from scripts.restore import main as restore_cli

from techpulse.contracts import TaskStatus
from techpulse.ops.backup import run_o40_drill

ROOT = Path(__file__).resolve().parents[2]


def test_o40_drill_end_to_end() -> None:
    with TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        artifacts_dir = root_dir / "artifacts"
        run_id = "o40-test-run-001"

        report = run_o40_drill(
            artifacts_dir=artifacts_dir,
            run_id=run_id,
            repo_root=ROOT,
        )

        assert report.status == "pass"
        assert report.errors == ()
        assert report.backup.total_database_rows > 0
        assert report.restore.ok
        assert report.restore.database_rows_match
        assert report.restore.database_sha256_match
        assert report.restore.restore_check.match
        assert report.deletion.status == "pass"
        assert report.deletion.raw_text_purged
        assert report.deletion.chunks_purged >= 1
        assert report.deletion.vectors_purged >= 1
        assert report.deletion.report_caches_purged >= 1
        assert not report.deletion.retrievable_after_deletion
        assert not report.deletion.real_customer_data_touched

        # Verify artifacts written on disk
        o40_dir = artifacts_dir / run_id / "O40"
        restore_test_file = o40_dir / "restore_test.json"
        manifest_file = o40_dir / "manifest.json"
        metrics_file = o40_dir / "metrics.json"

        assert restore_test_file.is_file()
        assert manifest_file.is_file()
        assert metrics_file.is_file()

        restore_data = json.loads(restore_test_file.read_text(encoding="utf-8"))
        assert restore_data["ticket"] == "O40"
        assert restore_data["status"] == "pass"
        assert restore_data["restore"]["ok"] is True
        assert restore_data["deletion_drill"]["retrievable_after_deletion"] is False

        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        assert manifest_data["ticket"] == "O40"
        assert manifest_data["status"] == TaskStatus.SUCCESS.value
        assert len(manifest_data["files"]) == 2

        metrics_data = json.loads(metrics_file.read_text(encoding="utf-8"))
        assert metrics_data["ticket"] == "O40"
        assert metrics_data["status"] == "pass"
        assert metrics_data["restore_match"] is True
        assert metrics_data["deletion_drill_pass"] is True
        assert metrics_data["real_customer_data_touched"] is False
        assert metrics_data["sensitive_record_retrievable"] is False


def test_backup_and_restore_cli_integration() -> None:
    with TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        artifacts_dir = root_dir / "artifacts"
        backup_dir = root_dir / "my_backup"

        # 1. First run a small drill to create a database and artifacts
        run_o40_drill(
            artifacts_dir=artifacts_dir,
            run_id="o40-cli-run",
            repo_root=ROOT,
        )
        db_file = artifacts_dir / "o40-cli-run" / "drill_work" / "drill.sqlite"
        assert db_file.is_file()

        # 2. Run backup CLI
        ret = backup_cli(
            [
                "--backup-dir",
                str(backup_dir),
                "--database",
                str(db_file),
                "--artifacts-dir",
                str(artifacts_dir),
            ]
        )
        assert ret == 0
        assert (backup_dir / "backup_manifest.json").is_file()

        # 3. Run restore CLI with verification into isolated dir
        restore_dir = root_dir / "cli_restored"
        ret_restore = restore_cli(
            [
                "--backup-dir",
                str(backup_dir),
                "--target-dir",
                str(restore_dir),
            ]
        )
        assert ret_restore == 0
        assert (restore_dir / "databases" / "drill.sqlite").is_file()

        # 4. Run restore CLI into temporary isolated dir
        ret_temp_restore = restore_cli(
            [
                "--backup-dir",
                str(backup_dir),
            ]
        )
        assert ret_temp_restore == 0
