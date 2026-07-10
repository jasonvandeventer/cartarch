"""issue #85 — daily_collection_values (per-user daily placed-value snapshot)

Revision ID: a7f3c9e21b58
Revises: b9e2f6a3c7d1
Create Date: 2026-07-10 00:00:00.000000

One row per ``(user_id, snapshot_date)``: the user's placed collection value on
that day, written by the daily price-ingest job after prices refresh.
``UNIQUE(user_id, snapshot_date)`` makes the same-day re-run an idempotent
upsert. FK to ``users.id`` ON DELETE CASCADE.

SQLite enforces no FKs (PRAGMA foreign_keys OFF), so the cascade is Postgres
defense-in-depth; on SQLite a deleted user leaves harmless orphan rows until the
Postgres cutover. ``total_value`` is NOT NULL (the writer always supplies a
value). Plain CREATE TABLE — applies on SQLite AND Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7f3c9e21b58"
down_revision: str | Sequence[str] | None = "b9e2f6a3c7d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_collection_values",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("total_value", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "snapshot_date", name="uq_daily_collection_values_user_date"
        ),
    )
    with op.batch_alter_table("daily_collection_values", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_daily_collection_values_user_id"), ["user_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("daily_collection_values", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_daily_collection_values_user_id"))
    op.drop_table("daily_collection_values")
