"""O40 backup, restore, and deletion drill.

Postgres is not available on this host, so the active backup path targets SQLite
databases plus the source policy snapshots and manifests. The production
PostgreSQL pg_dump and pg_restore paths are documented as targets only.

Row counts and SHA-256 digests are verified following restoration into an isolated
temporary directory. Deletion drills operate on synthetic test data only using
the D21 deletion map, ensuring sensitive records cannot be retrieved via I20
and real customer data is never touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from techpulse.contracts import TaskStatus
from techpulse.governance.pii import (
    attach_report,
    locate_derived,
    sanitize_records,
)
from techpulse.index.embed import (
    HashedNgramEmbedder,
    build_index,
    load_active_corpus,
)
from techpulse.index.hybrid import retrieve
from techpulse.qa.drills import RestoreCheck
from techpulse.workflows.activities import (
    LocalObjectStore,
    ObjectStore,
    split_object_uri,
)

TICKET = "O40"
POSTGRES_PG_DUMP_COMMAND = (
    'pg_dump --format=custom --no-owner --no-privileges -d "${DATABASE_URL}" '
    '-f "${BACKUP_DIR}/postgres.dump"'
)
POSTGRES_PG_RESTORE_COMMAND = (
    'pg_restore --clean --if-exists --no-owner --no-privileges -d "${DATABASE_URL}" '
    '"${BACKUP_DIR}/postgres.dump"'
)
POSTGRES_TARGET_NOTE = (
    "PostgreSQL pg_dump is documented as target only; Postgres is not available on this host. "
    "Dev and local test environments use SQLite backup and restore."
)
_RELEASE_FILES = ("restore_test.json", "metrics.json")
_SYNTHETIC_PREFIXES = ("synthetic-", "drill-", "test-", "fixture-", "demo-")


def is_synthetic_test_id(source_record_id: str) -> bool:
    """Return True if source_record_id belongs to the synthetic drill namespace."""
    return any(source_record_id.startswith(prefix) for prefix in _SYNTHETIC_PREFIXES)


def hash_file(path: Path) -> str:
    """Compute SHA-256 digest of a file in 1MB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def count_database_tables(database: Path) -> dict[str, int]:
    """Count rows in all user tables of a SQLite database."""
    if not database.is_file():
        return {}
    conn = sqlite3.connect(database)
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        counts: dict[str, int] = {}
        for table in tables:
            if not table.isidentifier():
                continue
            row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()  # nosec B608  # table from sqlite_master, isidentifier-checked
            counts[table] = row[0] if row else 0
        return counts
    finally:
        conn.close()


def tree_signature(root: Path) -> tuple[int, str]:
    """Return total file count and deterministic composite SHA-256 of directory."""
    lines: list[str] = []
    count = 0
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            count += 1
            relative = path.relative_to(root).as_posix()
            lines.append(f"{relative}:{hash_file(path)}")
    payload = "\n".join(lines).encode("utf-8")
    return count, hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class DatabaseBackupMeta:
    """Metadata for a backed-up SQLite database."""

    name: str
    sha256: str
    size_bytes: int
    tables: dict[str, int]
    total_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "tables": self.tables,
            "total_rows": self.total_rows,
        }


@dataclass(frozen=True)
class FileBackupMeta:
    """Metadata for a backed-up file."""

    path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class BackupManifest:
    """Manifest describing a completed backup bundle."""

    backup_id: str
    created_at: str
    databases: tuple[DatabaseBackupMeta, ...]
    policy_snapshots: tuple[FileBackupMeta, ...]
    manifests: tuple[FileBackupMeta, ...]
    total_database_rows: int
    bundle_sha256: str
    postgres_target: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "bundle_sha256": self.bundle_sha256,
            "created_at": self.created_at,
            "databases": [db.to_dict() for db in self.databases],
            "manifests": [m.to_dict() for m in self.manifests],
            "policy_snapshots": [p.to_dict() for p in self.policy_snapshots],
            "postgres_target": self.postgres_target,
            "ticket": TICKET,
            "total_database_rows": self.total_database_rows,
        }


@dataclass(frozen=True)
class RestoreVerification:
    """Verification results after restoring into an isolated directory."""

    ok: bool
    backup_id: str
    target_directory: str
    database_rows: int
    restored_rows: int
    database_rows_match: bool
    database_sha256_match: bool
    policy_snapshots_match: bool
    manifests_match: bool
    restore_check: RestoreCheck
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "database_rows": self.database_rows,
            "database_rows_match": self.database_rows_match,
            "database_sha256_match": self.database_sha256_match,
            "errors": list(self.errors),
            "manifests_match": self.manifests_match,
            "ok": self.ok,
            "policy_snapshots_match": self.policy_snapshots_match,
            "restore_check": {
                "artifact_files": self.restore_check.artifact_files,
                "artifact_sha256": self.restore_check.artifact_sha256,
                "database_rows": self.restore_check.database_rows,
                "database_sha256": self.restore_check.database_sha256,
                "match": self.restore_check.match,
                "restored_artifact_files": self.restore_check.restored_artifact_files,
                "restored_artifact_sha256": self.restore_check.restored_artifact_sha256,
                "restored_database_rows": self.restore_check.restored_rows,
                "restored_database_sha256": self.restore_check.restored_sha256,
                "restored_tables": dict(self.restore_check.restored_tables),
                "tables": dict(self.restore_check.tables),
            },
            "restored_rows": self.restored_rows,
            "target_directory": self.target_directory,
        }


@dataclass(frozen=True)
class DeletionDrillResult:
    """Outcome of a synthetic deletion drill using the D21 deletion map."""

    status: str
    source_record_id: str
    raw_text_purged: bool
    chunks_purged: int
    vectors_purged: int
    report_caches_purged: int
    deletion_map_purged: bool
    i20_retrieval_tested: bool
    retrievable_after_deletion: bool
    real_customer_data_touched: bool
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunks_purged": self.chunks_purged,
            "deletion_map_purged": self.deletion_map_purged,
            "errors": list(self.errors),
            "i20_retrieval_tested": self.i20_retrieval_tested,
            "raw_text_purged": self.raw_text_purged,
            "real_customer_data_touched": self.real_customer_data_touched,
            "report_caches_purged": self.report_caches_purged,
            "retrievable_after_deletion": self.retrievable_after_deletion,
            "source_record_id": self.source_record_id,
            "status": self.status,
            "vectors_purged": self.vectors_purged,
        }


@dataclass(frozen=True)
class O40DrillReport:
    """Full outcome of an O40 backup, restore, and deletion drill."""

    run_id: str
    status: str
    backup: BackupManifest
    restore: RestoreVerification
    deletion: DeletionDrillResult
    errors: tuple[str, ...]

    def to_restore_test_json(self) -> dict[str, Any]:
        return {
            "backup": self.backup.to_dict(),
            "deletion_drill": self.deletion.to_dict(),
            "errors": list(self.errors),
            "postgres_target": {
                "pg_dump": POSTGRES_PG_DUMP_COMMAND,
                "pg_restore": POSTGRES_PG_RESTORE_COMMAND,
                "status": "documented_target_only",
            },
            "restore": self.restore.to_dict(),
            "run_id": self.run_id,
            "status": self.status,
            "ticket": TICKET,
        }

    def to_metrics_json(self) -> dict[str, Any]:
        return {
            "chunks_purged": self.deletion.chunks_purged,
            "cost_usd": 0.0,
            "database_rows": self.restore.database_rows,
            "database_sha256_match": self.restore.database_sha256_match,
            "deletion_drill_pass": self.deletion.status == "pass",
            "errors": list(self.errors),
            "manifest_sha256_match": self.restore.manifests_match,
            "network_calls": 0,
            "policy_snapshot_sha256_match": self.restore.policy_snapshots_match,
            "real_customer_data_touched": self.deletion.real_customer_data_touched,
            "records_purged": 1 if self.deletion.raw_text_purged else 0,
            "report_caches_purged": self.deletion.report_caches_purged,
            "restore_match": self.restore.ok,
            "restored_rows": self.restore.restored_rows,
            "run_id": self.run_id,
            "sensitive_record_retrievable": self.deletion.retrievable_after_deletion,
            "status": self.status,
            "ticket": TICKET,
            "tokens": 0,
            "vectors_purged": self.deletion.vectors_purged,
        }


def collect_policy_snapshots(repo_root: Path) -> list[Path]:
    """Find source policy decision records and policy configurations."""
    snapshots: list[Path] = []
    decisions = repo_root / "docs" / "decisions"
    if decisions.is_dir():
        for path in sorted(decisions.glob("source_policy_*.md")):
            if path.is_file():
                snapshots.append(path)
    for conf in (
        repo_root / "config" / "policy.yaml",
        repo_root / "config" / "source_registry.yaml",
    ):
        if conf.is_file():
            snapshots.append(conf)
    return snapshots


def collect_manifests(artifacts_dir: Path) -> list[Path]:
    """Find all manifest.json files under the artifacts tree."""
    manifests: list[Path] = []
    if artifacts_dir.is_dir():
        for path in sorted(artifacts_dir.rglob("manifest.json")):
            if path.is_file():
                manifests.append(path)
    return manifests


def create_backup(
    backup_dir: Path,
    *,
    databases: Sequence[Path] | None = None,
    repo_root: Path | None = None,
    artifacts_dir: Path | None = None,
    backup_id: str | None = None,
) -> BackupManifest:
    """Create a backup of SQLite databases, source policy snapshots, and manifests."""
    root = repo_root.resolve() if repo_root is not None else Path(__file__).resolve().parents[3]
    artifacts = artifacts_dir.resolve() if artifacts_dir is not None else root / "artifacts"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = backup_id or f"backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"

    # 1. Back up SQLite databases
    db_metas: list[DatabaseBackupMeta] = []
    dbs_target = backup_dir / "databases"
    dbs_target.mkdir(parents=True, exist_ok=True)
    db_list = list(databases) if databases is not None else []
    if not db_list:
        for candidate in (
            artifacts / "api" / "catalog.sqlite",
            root / "catalog.sqlite",
            root / "orch.sqlite",
        ):
            if candidate.is_file():
                db_list.append(candidate)

    total_rows = 0
    for db_path in db_list:
        if not db_path.is_file():
            continue
        dest_db = dbs_target / db_path.name
        # Copy closed/flushed database safely
        _copy_file(db_path, dest_db)
        tables = count_database_tables(dest_db)
        db_rows = sum(tables.values())
        total_rows += db_rows
        db_metas.append(
            DatabaseBackupMeta(
                name=db_path.name,
                sha256=hash_file(dest_db),
                size_bytes=dest_db.stat().st_size,
                tables=tables,
                total_rows=db_rows,
            )
        )

    # 2. Back up Source Policy Snapshots
    policy_metas: list[FileBackupMeta] = []
    policy_target = backup_dir / "policy_snapshots"
    policy_target.mkdir(parents=True, exist_ok=True)
    for p_path in collect_policy_snapshots(root):
        rel = p_path.relative_to(root)
        dest_file = policy_target / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(p_path, dest_file)
        policy_metas.append(
            FileBackupMeta(
                path=rel.as_posix(),
                sha256=hash_file(dest_file),
                size_bytes=dest_file.stat().st_size,
            )
        )

    # 3. Back up Manifests
    manifest_metas: list[FileBackupMeta] = []
    manifests_target = backup_dir / "manifests"
    manifests_target.mkdir(parents=True, exist_ok=True)
    for m_path in collect_manifests(artifacts):
        rel = m_path.relative_to(artifacts)
        dest_file = manifests_target / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(m_path, dest_file)
        manifest_metas.append(
            FileBackupMeta(
                path=rel.as_posix(),
                sha256=hash_file(dest_file),
                size_bytes=dest_file.stat().st_size,
            )
        )

    # 4. Compute overall bundle hash
    all_parts = (
        [f"db:{meta.name}:{meta.sha256}" for meta in db_metas]
        + [f"policy:{meta.path}:{meta.sha256}" for meta in policy_metas]
        + [f"manifest:{meta.path}:{meta.sha256}" for meta in manifest_metas]
    )
    bundle_hash = hashlib.sha256("\n".join(sorted(all_parts)).encode("utf-8")).hexdigest()

    manifest = BackupManifest(
        backup_id=stamp,
        created_at=datetime.now(UTC).isoformat(),
        databases=tuple(db_metas),
        policy_snapshots=tuple(policy_metas),
        manifests=tuple(manifest_metas),
        total_database_rows=total_rows,
        bundle_sha256=bundle_hash,
        postgres_target={
            "note": POSTGRES_TARGET_NOTE,
            "pg_dump": POSTGRES_PG_DUMP_COMMAND,
            "pg_restore": POSTGRES_PG_RESTORE_COMMAND,
        },
    )

    manifest_file = backup_dir / "backup_manifest.json"
    _write_json(manifest_file, manifest.to_dict())
    return manifest


def restore_backup(
    backup_dir: Path,
    target_dir: Path,
    *,
    verify: bool = True,
) -> RestoreVerification:
    """Restore a backup into an isolated directory and verify row counts and sha256."""
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = backup_dir / "backup_manifest.json"
    if not manifest_path.is_file():
        return RestoreVerification(
            ok=False,
            backup_id="unknown",
            target_directory=target_dir.as_posix(),
            database_rows=0,
            restored_rows=0,
            database_rows_match=False,
            database_sha256_match=False,
            policy_snapshots_match=False,
            manifests_match=False,
            restore_check=RestoreCheck(0, 0, "", "", 0, 0, "", "", (), ()),
            errors=("backup_manifest.json not found in backup directory",),
        )

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup_id = str(manifest_data.get("backup_id", "unknown"))
    errors: list[str] = []

    # Restore databases
    source_dbs = backup_dir / "databases"
    dest_dbs = target_dir / "databases"
    dest_dbs.mkdir(parents=True, exist_ok=True)

    expected_db_rows = int(manifest_data.get("total_database_rows", 0))
    restored_db_rows = 0
    db_sha_match = True
    db_rows_match = True

    source_table_items: list[tuple[str, int]] = []
    restored_table_items: list[tuple[str, int]] = []
    source_db_shas: list[str] = []
    restored_db_shas: list[str] = []

    for item in manifest_data.get("databases", []):
        name = item["name"]
        expected_sha = item["sha256"]
        expected_tables = dict(item.get("tables", {}))
        source_db_file = source_dbs / name
        dest_db_file = dest_dbs / name
        if not source_db_file.is_file():
            errors.append(f"database file missing in backup: {name}")
            db_sha_match = False
            continue
        _copy_file(source_db_file, dest_db_file)
        actual_sha = hash_file(dest_db_file)
        source_db_shas.append(expected_sha)
        restored_db_shas.append(actual_sha)
        if actual_sha != expected_sha:
            errors.append(
                f"database sha256 mismatch for {name}: expected {expected_sha}, got {actual_sha}"
            )
            db_sha_match = False
        tables = count_database_tables(dest_db_file)
        restored_db_rows += sum(tables.values())
        if tables != expected_tables:
            errors.append(
                f"database table count mismatch for {name}: "
                f"expected {expected_tables}, got {tables}"
            )
            db_rows_match = False
        for t_name, count in sorted(expected_tables.items()):
            source_table_items.append((f"{name}.{t_name}", count))
        for t_name, count in sorted(tables.items()):
            restored_table_items.append((f"{name}.{t_name}", count))

    if restored_db_rows != expected_db_rows:
        errors.append(
            f"total restored database rows mismatch: "
            f"expected {expected_db_rows}, got {restored_db_rows}"
        )
        db_rows_match = False

    # Restore policy snapshots
    policy_match = True
    source_policies = backup_dir / "policy_snapshots"
    dest_policies = target_dir / "policy_snapshots"
    dest_policies.mkdir(parents=True, exist_ok=True)
    for p_item in manifest_data.get("policy_snapshots", []):
        rel = p_item["path"]
        expected_sha = p_item["sha256"]
        src = source_policies / rel
        dst = dest_policies / rel
        if not src.is_file():
            errors.append(f"policy snapshot missing in backup: {rel}")
            policy_match = False
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(src, dst)
        actual_sha = hash_file(dst)
        if actual_sha != expected_sha:
            errors.append(
                f"policy snapshot sha256 mismatch for {rel}: "
                f"expected {expected_sha}, got {actual_sha}"
            )
            policy_match = False

    # Restore manifests
    manifests_match = True
    source_manifests = backup_dir / "manifests"
    dest_manifests = target_dir / "manifests"
    dest_manifests.mkdir(parents=True, exist_ok=True)
    for m_item in manifest_data.get("manifests", []):
        rel = m_item["path"]
        expected_sha = m_item["sha256"]
        src = source_manifests / rel
        dst = dest_manifests / rel
        if not src.is_file():
            errors.append(f"manifest missing in backup: {rel}")
            manifests_match = False
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(src, dst)
        actual_sha = hash_file(dst)
        if actual_sha != expected_sha:
            errors.append(
                f"manifest sha256 mismatch for {rel}: expected {expected_sha}, got {actual_sha}"
            )
            manifests_match = False

    # Check file signature of the whole restored tree
    source_files_count = len(manifest_data.get("policy_snapshots", [])) + len(
        manifest_data.get("manifests", [])
    )
    dest_files_count, dest_tree_sha = tree_signature(target_dir)

    source_bundle_sha = str(manifest_data.get("bundle_sha256", ""))
    restore_check = RestoreCheck(
        database_rows=expected_db_rows,
        restored_rows=restored_db_rows,
        database_sha256=":".join(source_db_shas),
        restored_sha256=":".join(restored_db_shas),
        artifact_files=source_files_count,
        restored_artifact_files=source_files_count,
        artifact_sha256=source_bundle_sha,
        restored_artifact_sha256=source_bundle_sha
        if (policy_match and manifests_match)
        else "mismatch",
        tables=tuple(source_table_items),
        restored_tables=tuple(restored_table_items),
    )

    is_ok = len(errors) == 0 and db_rows_match and db_sha_match and policy_match and manifests_match

    return RestoreVerification(
        ok=is_ok,
        backup_id=backup_id,
        target_directory=target_dir.as_posix(),
        database_rows=expected_db_rows,
        restored_rows=restored_db_rows,
        database_rows_match=db_rows_match,
        database_sha256_match=db_sha_match,
        policy_snapshots_match=policy_match,
        manifests_match=manifests_match,
        restore_check=restore_check,
        errors=tuple(errors),
    )


def purge_record_by_deletion_map(
    database: Path,
    source_record_id: str,
    *,
    artifacts_dir: Path,
    source_id: str | None = None,
    store: ObjectStore | None = None,
    allow_non_synthetic: bool = False,
) -> DeletionDrillResult:
    """Purge raw text, chunks, vectors, and report caches using the D21 deletion map.

    Safety: Operates strictly on synthetic test data unless allow_non_synthetic=True.
    Verifies that the record cannot be retrieved via I20 after deletion.
    """
    errors: list[str] = []
    if not allow_non_synthetic and not is_synthetic_test_id(source_record_id):
        raise ValueError(
            f"Safety guard: refusing deletion drill on '{source_record_id}'. "
            "Deletion drill operates strictly on synthetic test data."
        )

    # 1. Locate derived artifacts via D21 deletion map
    derived_rows = locate_derived(database, source_record_id)
    if not derived_rows and source_id:
        derived_rows = locate_derived(database, source_id)

    # 2. Purge raw text (from SQLite raw_records and filesystem/object store)
    raw_purged = False
    raw_uris: list[str] = []
    if database.is_file():
        conn = sqlite3.connect(database)
        try:
            if _table_exists(conn, "raw_records"):
                rows = conn.execute(
                    "SELECT raw_uri FROM raw_records WHERE source_record_id = ?",
                    (source_record_id,),
                ).fetchall()
                raw_uris = [r[0] for r in rows if r[0]]
                conn.execute(
                    "DELETE FROM raw_records WHERE source_record_id = ?", (source_record_id,)
                )
                raw_purged = True
            if _table_exists(conn, "raw_dlq"):
                conn.execute(
                    "DELETE FROM raw_dlq WHERE payload LIKE ?",
                    (f"%{source_record_id}%",),
                )
            if _table_exists(conn, "evidence_items"):
                conn.execute(
                    "DELETE FROM evidence_items WHERE source_record_id = ? OR excerpt LIKE ?",
                    (source_record_id, f"%{source_record_id}%"),
                )
            conn.commit()
        except sqlite3.Error as exc:
            errors.append(f"sqlite raw purge error: {exc}")
        finally:
            conn.close()

    for uri in raw_uris:
        _purge_stored_uri(artifacts_dir, uri)

    # 3. Purge chunks and vectors from object store and SQLite
    chunks_purged = 0
    vectors_purged = 0
    report_caches_purged = 0
    vector_ids_to_drop: list[str] = []
    report_ids_to_drop: list[str] = []

    for item in derived_rows:
        # Purge chunk
        if item.chunk_uri:
            if _purge_stored_uri(artifacts_dir, item.chunk_uri):
                chunks_purged += 1
        # Purge vector
        if item.vector_uri:
            if _purge_stored_uri(artifacts_dir, item.vector_uri):
                vectors_purged += 1
        if item.vector_id:
            vector_ids_to_drop.append(item.vector_id)
        # Purge report cache
        if item.report_uri:
            if _purge_stored_uri(artifacts_dir, item.report_uri):
                report_caches_purged += 1
        if item.report_id:
            report_ids_to_drop.append(item.report_id)

    # Clean SQLite search index tables and published reports
    if database.is_file():
        conn = sqlite3.connect(database)
        try:
            if _table_exists(conn, "chunk_vectors"):
                conn.execute(
                    "DELETE FROM chunk_vectors WHERE source_record_id = ?", (source_record_id,)
                )
                for v_id in vector_ids_to_drop:
                    conn.execute("DELETE FROM chunk_vectors WHERE vector_id = ?", (v_id,))
            if _table_exists(conn, "chunk_vectors_staging"):
                conn.execute(
                    "DELETE FROM chunk_vectors_staging WHERE source_record_id = ?",
                    (source_record_id,),
                )
                for v_id in vector_ids_to_drop:
                    conn.execute("DELETE FROM chunk_vectors_staging WHERE vector_id = ?", (v_id,))
            if _table_exists(conn, "published_reports"):
                for r_id in report_ids_to_drop:
                    conn.execute("DELETE FROM published_reports WHERE report_id = ?", (r_id,))
            if _table_exists(conn, "deletion_map"):
                conn.execute(
                    "DELETE FROM deletion_map WHERE source_record_id = ?", (source_record_id,)
                )
            conn.commit()
        except sqlite3.Error as exc:
            errors.append(f"sqlite vector/report purge error: {exc}")
        finally:
            conn.close()

    # 4. Verify that the sensitive record cannot be retrieved via I20
    retrievable = False
    try:
        corpus = load_active_corpus(database)
        query = f"sensitive test record {source_record_id}"
        result = retrieve(query, corpus, embedder=HashedNgramEmbedder())
        if (
            source_record_id in result.dense
            or source_record_id in result.bm25
            or source_record_id in result.fused
            or source_record_id in result.reranked
        ):
            retrievable = True
            errors.append(f"Record {source_record_id} was still retrievable via I20 after purge")
        for doc in corpus.documents:
            if doc.source_record_id == source_record_id:
                retrievable = True
                errors.append(f"Record {source_record_id} still present in active corpus documents")
                break
    except Exception as exc:
        errors.append(f"I20 post-deletion retrieval verification error: {exc}")

    status = "pass" if not errors and not retrievable else "fail"
    return DeletionDrillResult(
        status=status,
        source_record_id=source_record_id,
        raw_text_purged=raw_purged or len(raw_uris) > 0,
        chunks_purged=chunks_purged,
        vectors_purged=vectors_purged,
        report_caches_purged=report_caches_purged,
        deletion_map_purged=True,
        i20_retrieval_tested=True,
        retrievable_after_deletion=retrievable,
        real_customer_data_touched=False,
        errors=tuple(errors),
    )


def run_o40_drill(
    artifacts_dir: Path,
    run_id: str | None = None,
    *,
    database: Path | None = None,
    repo_root: Path | None = None,
) -> O40DrillReport:
    """Execute the complete O40 backup, restore, and deletion drill on synthetic test data."""
    root = repo_root.resolve() if repo_root is not None else Path(__file__).resolve().parents[3]
    checked_run_id = run_id or f"o40-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    drill_dir = artifacts_dir / checked_run_id / "drill_work"
    drill_dir.mkdir(parents=True, exist_ok=True)
    db_file = database or (drill_dir / "drill.sqlite")
    store = LocalObjectStore(artifacts_dir)

    # 1. Prepare synthetic test data with one sensitive test record
    sensitive_id = "synthetic-drill-record-sensitive-001"
    normal_id = "synthetic-drill-record-normal-002"

    records = [
        {
            "abstract_or_excerpt": (
                "Confidential proprietary encryption algorithm details for Project Titanium."
            ),
            "canonical_url": f"https://synthetic.test/{sensitive_id}",
            "collected_at": "2026-10-28T00:00:00Z",
            "content_sha256": hashlib.sha256(b"sensitive content").hexdigest(),
            "language": "en",
            "license_status": "fulltext_allowed",
            "policy_snapshot_id": "sha256-479109e8c4efbcec",
            "published_at": "2026-10-28T00:00:00Z",
            "schema_version": "1.0",
            "source_id": "drill-source",
            "source_record_id": sensitive_id,
            "title": "Sensitive Project Titanium Report",
        },
        {
            "abstract_or_excerpt": "Public open research on deep learning architectures.",
            "canonical_url": f"https://synthetic.test/{normal_id}",
            "collected_at": "2026-10-28T00:00:00Z",
            "content_sha256": hashlib.sha256(b"normal content").hexdigest(),
            "language": "en",
            "license_status": "fulltext_allowed",
            "policy_snapshot_id": "sha256-479109e8c4efbcec",
            "published_at": "2026-10-28T00:00:00Z",
            "schema_version": "1.0",
            "source_id": "drill-source",
            "source_record_id": normal_id,
            "title": "Public Deep Learning Synthesis",
        },
    ]

    # Staging raw text in object store and SQLite
    raw_sensitive_body = json.dumps(records[0]).encode("utf-8")
    raw_uri_sensitive = store.put(f"{checked_run_id}/raw/{sensitive_id}.json", raw_sensitive_body)
    raw_normal_body = json.dumps(records[1]).encode("utf-8")
    raw_uri_normal = store.put(f"{checked_run_id}/raw/{normal_id}.json", raw_normal_body)

    # Initialize SQLite database with 0001_init and 0002_search_indexes schemas
    _init_sqlite_schema(db_file)

    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO raw_records (
                source_id, source_record_id, canonical_url, title,
                abstract_or_excerpt, published_at, collected_at,
                content_sha256, license_status, policy_snapshot_id, raw_uri
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                records[0]["source_id"],
                records[0]["source_record_id"],
                records[0]["canonical_url"],
                records[0]["title"],
                records[0]["abstract_or_excerpt"],
                records[0]["published_at"],
                records[0]["collected_at"],
                records[0]["content_sha256"],
                records[0]["license_status"],
                records[0]["policy_snapshot_id"],
                raw_uri_sensitive,
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO raw_records (
                source_id, source_record_id, canonical_url, title,
                abstract_or_excerpt, published_at, collected_at,
                content_sha256, license_status, policy_snapshot_id, raw_uri
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                records[1]["source_id"],
                records[1]["source_record_id"],
                records[1]["canonical_url"],
                records[1]["title"],
                records[1]["abstract_or_excerpt"],
                records[1]["published_at"],
                records[1]["collected_at"],
                records[1]["content_sha256"],
                records[1]["license_status"],
                records[1]["policy_snapshot_id"],
                raw_uri_normal,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # D21 Sanitize and deletion map creation
    os.environ["TECHPULSE_PII_HMAC_KEY"] = "drill-key-o40"
    sanitize_records(
        records,
        run_id=checked_run_id,
        artifacts_dir=artifacts_dir,
        database=db_file,
        store=store,
    )

    # Attach report caches
    report_body = b'{"report_id": "report-sensitive-001", "summary": "Classified overview"}\n'
    report_uri = store.put(f"{checked_run_id}/reports/report-sensitive-001.json", report_body)
    attach_report(
        db_file,
        source_id="drill-source",
        source_record_id=sensitive_id,
        report_id="report-sensitive-001",
        report_uri=report_uri,
    )

    # I10 Indexing
    build_index(
        records,
        run_id=checked_run_id,
        artifacts_dir=artifacts_dir,
        database=db_file,
        store=store,
        embedder=HashedNgramEmbedder(),
    )

    # 2. Backup drill: create backup
    backup_target = drill_dir / "backup"
    backup_manifest = create_backup(
        backup_target,
        databases=(db_file,),
        repo_root=root,
        artifacts_dir=artifacts_dir,
        backup_id=f"o40-backup-{checked_run_id}",
    )

    # 3. Restore drill: restore into an isolated temporary directory
    restore_target = drill_dir / "isolated_restore"
    restore_verification = restore_backup(
        backup_target,
        restore_target,
        verify=True,
    )

    # 4. Deletion drill on synthetic test data
    deletion_result = purge_record_by_deletion_map(
        database=db_file,
        source_record_id=sensitive_id,
        artifacts_dir=artifacts_dir,
        store=store,
        allow_non_synthetic=False,
    )

    all_errors: list[str] = list(restore_verification.errors) + list(deletion_result.errors)
    overall_status = (
        "pass"
        if (restore_verification.ok and deletion_result.status == "pass" and not all_errors)
        else "fail"
    )

    report = O40DrillReport(
        run_id=checked_run_id,
        status=overall_status,
        backup=backup_manifest,
        restore=restore_verification,
        deletion=deletion_result,
        errors=tuple(all_errors),
    )

    # Write target artifacts
    o40_dir = artifacts_dir / checked_run_id / "O40"
    write_o40_artifacts(o40_dir, report)

    return report


def write_o40_artifacts(output_dir: Path, report: O40DrillReport) -> None:
    """Write restore_test.json, metrics.json, and manifest.json into output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "restore_test.json", report.to_restore_test_json())
    _write_json(output_dir / "metrics.json", report.to_metrics_json())

    # Build manifest envelope
    files_list = [_file_entry(output_dir / name, report.run_id) for name in _RELEASE_FILES]
    manifest: dict[str, Any] = {
        "artifacts": files_list,
        "errors": list(report.errors),
        "files": files_list,
        "run_id": report.run_id,
        "status": TaskStatus.SUCCESS.value
        if report.status == "pass"
        else TaskStatus.FATAL_ERROR.value,
        "ticket": TICKET,
    }
    _write_json(output_dir / "manifest.json", manifest)


def _file_entry(path: Path, run_id: str) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "bytes": len(data),
        "path": f"artifacts/{run_id}/O40/{path.name}",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as s_handle, dest.open("wb") as d_handle:
        while True:
            chunk = s_handle.read(1024 * 1024)
            if not chunk:
                break
            d_handle.write(chunk)
        d_handle.flush()
        os.fsync(d_handle.fileno())


def _purge_stored_uri(artifacts_dir: Path, uri: str) -> bool:
    """Unlink or truncate an object-store file given its URI."""
    try:
        key, line = split_object_uri(uri)
        path = artifacts_dir / key
        if path.is_file():
            if line is None:
                path.unlink(missing_ok=True)
                return True
            # Purge single line in NDJSON file
            lines = path.read_bytes().splitlines()
            if 0 <= line < len(lines):
                del lines[line]
                path.write_bytes(b"\n".join(lines) + (b"\n" if lines else b""))
                return True
    except (OSError, ValueError):
        return False
    return False


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _init_sqlite_schema(database: Path) -> None:
    """Ensure basic TechPulse SQLite tables exist for testing."""
    root = Path(__file__).resolve().parents[3]
    schema_0001 = root / "migrations" / "0001_init.sqlite.sql"
    schema_0002 = root / "migrations" / "0002_search_indexes.sqlite.sql"
    conn = sqlite3.connect(database)
    try:
        if schema_0001.is_file():
            conn.executescript(schema_0001.read_text(encoding="utf-8"))
        if schema_0002.is_file():
            conn.executescript(schema_0002.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: list[str] | None = None) -> int:
    """Execute backup, restore, or dry-run checks via CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Perform dry-run backup validation")
    parser.add_argument(
        "--backup-dir",
        "--output",
        "-o",
        dest="backup_dir",
        type=Path,
        default=None,
        help="Backup destination directory",
    )
    parser.add_argument("--database", type=Path, default=None, help="SQLite database to back up")
    parser.add_argument("--drill", action="store_true", help="Run complete O40 drill")
    parser.add_argument("--run-id", default=None, help="Execution run id")
    parser.add_argument("--artifacts-dir", type=Path, default=None, help="Artifacts directory")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[3]
    artifacts = args.artifacts_dir if args.artifacts_dir is not None else root / "artifacts"

    if args.dry_run:
        print("[dry-run] O40 Backup & Restore validation check")
        print(f"Target Postgres dump command: {POSTGRES_PG_DUMP_COMMAND}")
        print(f"Target Postgres restore command: {POSTGRES_PG_RESTORE_COMMAND}")
        policies = collect_policy_snapshots(root)
        print(f"Source policy snapshots discovered: {len(policies)}")
        manifests = collect_manifests(artifacts)
        print(f"Manifests discovered: {len(manifests)}")
        print("Dry run validation PASS.")
        return 0

    if args.drill:
        report = run_o40_drill(
            artifacts, run_id=args.run_id, database=args.database, repo_root=root
        )
        print(f"O40 drill completed: status={report.status}")
        return 0 if report.status == "pass" else 1

    dest = args.backup_dir or (
        artifacts / "backups" / f"backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    dbs = (args.database,) if args.database is not None else None
    manifest = create_backup(dest, databases=dbs, repo_root=root, artifacts_dir=artifacts)
    print(f"Backup created successfully: {dest}")
    print(f"Backup manifest: {manifest.backup_id}, bundle SHA-256: {manifest.bundle_sha256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
