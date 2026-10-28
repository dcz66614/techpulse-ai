"""Search index tables. PostgreSQL uses pgvector. SQLite stores float32 blobs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28
"""

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_MIGRATIONS = Path(__file__).resolve().parents[1]
_DROP_ORDER = (
    "chunk_vectors_staging",
    "chunk_vectors",
    "index_pointer",
    "index_versions",
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
        _execute_script("0002_search_indexes.sql")
    elif dialect == "sqlite":
        _execute_script("0002_search_indexes.sqlite.sql")
    else:
        message = f"unsupported dialect: {dialect}"
        raise RuntimeError(message)


def downgrade() -> None:
    for table in _DROP_ORDER:
        op.drop_table(table)
