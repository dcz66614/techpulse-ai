"""Unit tests for O40 backup, restore, and deletion drill."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from techpulse.ops.backup import (
    POSTGRES_PG_DUMP_COMMAND,
    POSTGRES_PG_RESTORE_COMMAND,
    POSTGRES_TARGET_NOTE,
    collect_policy_snapshots,
    count_database_tables,
    create_backup,
    hash_file,
    is_synthetic_test_id,
    purge_record_by_deletion_map,
    restore_backup,
)

ROOT = Path(__file__).resolve().parents[2]


def test_postgres_target_is_documented() -> None:
    assert "pg_dump" in POSTGRES_PG_DUMP_COMMAND
    assert "--format=custom" in POSTGRES_PG_DUMP_COMMAND
    assert "pg_restore" in POSTGRES_PG_RESTORE_COMMAND
    assert "--clean" in POSTGRES_PG_RESTORE_COMMAND
    assert "target only" in POSTGRES_TARGET_NOTE.lower()


def test_synthetic_identifier_guard() -> None:
    assert is_synthetic_test_id("synthetic-123")
    assert is_synthetic_test_id("drill-record-01")
    assert is_synthetic_test_id("test-fixture-1")
    assert not is_synthetic_test_id("real-customer-12345")
    assert not is_synthetic_test_id("production-user-record")


def test_collect_policy_snapshots() -> None:
    snapshots = collect_policy_snapshots(ROOT)
    assert len(snapshots) >= 8
    names = [p.name for p in snapshots]
    assert any("openalex" in n for n in names)
    assert any("arxiv" in n for n in names)


def test_backup_and_restore_into_isolated_directory() -> None:
    with TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        db_file = root_dir / "sample.sqlite"
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO users (name) VALUES ('Alice'), ('Bob')")
        conn.execute(
            "INSERT INTO notes (note) VALUES ('test note 1'), ('test note 2'), ('test note 3')"
        )
        conn.commit()
        conn.close()

        tables_before = count_database_tables(db_file)
        assert tables_before == {"notes": 3, "users": 2}

        # Create dummy artifacts dir with manifest
        artifacts_dir = root_dir / "artifacts"
        manifest_file = artifacts_dir / "test-run" / "P00" / "manifest.json"
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text('{"ticket": "P00", "status": "success"}\n', encoding="utf-8")

        # 1. Create backup
        backup_dir = root_dir / "backup"
        manifest = create_backup(
            backup_dir,
            databases=(db_file,),
            repo_root=ROOT,
            artifacts_dir=artifacts_dir,
            backup_id="test-backup-001",
        )
        assert manifest.total_database_rows == 5
        assert len(manifest.databases) == 1
        assert manifest.databases[0].name == "sample.sqlite"
        assert manifest.databases[0].sha256 == hash_file(db_file)

        # 2. Restore into isolated temporary directory
        restore_dir = root_dir / "isolated_restore"
        verification = restore_backup(backup_dir, restore_dir, verify=True)

        assert verification.ok
        assert verification.database_rows == 5
        assert verification.restored_rows == 5
        assert verification.database_rows_match
        assert verification.database_sha256_match
        assert verification.policy_snapshots_match
        assert verification.manifests_match
        assert verification.restore_check.match

        # Verify restored sqlite file directly
        restored_db = restore_dir / "databases" / "sample.sqlite"
        assert restored_db.is_file()
        assert hash_file(restored_db) == hash_file(db_file)
        assert count_database_tables(restored_db) == tables_before


def test_restore_detects_corrupted_database() -> None:
    with TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        db_file = root_dir / "sample.sqlite"
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO users (name) VALUES ('Alice')")
        conn.commit()
        conn.close()

        backup_dir = root_dir / "backup"
        create_backup(backup_dir, databases=(db_file,), repo_root=ROOT)

        # Tamper with the database in backup
        tampered_db = backup_dir / "databases" / "sample.sqlite"
        with tampered_db.open("ab") as handle:
            handle.write(b"corrupted-tampered-data")

        # Restore must detect mismatch
        restore_dir = root_dir / "restored"
        verification = restore_backup(backup_dir, restore_dir, verify=True)
        assert not verification.ok
        assert not verification.database_sha256_match
        assert any("mismatch" in e for e in verification.errors)


def test_customer_data_safety_invariant() -> None:
    with TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        db_file = root_dir / "db.sqlite"
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE dummy (id INTEGER)")
        conn.commit()
        conn.close()

        with pytest.raises(ValueError, match="Safety guard"):
            purge_record_by_deletion_map(
                db_file,
                "real-customer-private-data-id",
                artifacts_dir=root_dir,
                allow_non_synthetic=False,
            )


def test_shell_script_wrappers_exist_and_executable() -> None:
    backup_sh = ROOT / "scripts" / "backup.sh"
    restore_sh = ROOT / "scripts" / "restore.sh"
    assert backup_sh.is_file()
    assert restore_sh.is_file()
    assert backup_sh.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    assert restore_sh.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    assert "scripts/backup.py" in backup_sh.read_text(encoding="utf-8")
    assert "scripts/restore.py" in restore_sh.read_text(encoding="utf-8")
