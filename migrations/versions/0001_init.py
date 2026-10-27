"""Initial schema from migrations/0001_init.sql.

Revision ID: 0001
Revises:
Create Date: 2026-10-27
"""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_MIGRATIONS = Path(__file__).resolve().parents[1]
_DROP_ORDER = (
    "trend_evidence",
    "evidence_items",
    "trend_scores",
    "raw_records",
    "canonical_entities",
    "source_registry",
    "published_reports",
    "task_runs",
)


def _execute_script(name: str) -> None:
    sql = (_MIGRATIONS / name).read_text(encoding="utf-8")
    for statement in sql.split(";"):
        statement = statement.strip()
        if statement:
            op.execute(statement)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _execute_script("0001_init.sql")
    elif dialect == "sqlite":
        # This host has no PostgreSQL. The sqlite script is the same tables.
        _execute_script("0001_init.sqlite.sql")
    else:
        message = f"unsupported dialect: {dialect}"
        raise RuntimeError(message)


def downgrade() -> None:
    for table in _DROP_ORDER:
        op.drop_table(table)
