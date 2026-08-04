"""commander_global_stats — worldwide per-commander priors from playgroup.gg.

One row per commander card name; harvested from playgroup.gg's open public
/commanders endpoints by an in-app daemon loop (no auth, no game-level data).

Revision ID: a9d1c3e57f24
Revises: f5c6d7e8a9b0
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9d1c3e57f24"
down_revision: str | Sequence[str] | None = "f5c6d7e8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "commander_global_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("commander_name", sa.String(length=256), nullable=False),
        sa.Column("pg_commander_id", sa.Integer(), nullable=True),
        sa.Column("elo", sa.Integer(), nullable=True),
        sa.Column("global_rank", sa.Integer(), nullable=True),
        sa.Column("games_won", sa.Integer(), nullable=True),
        sa.Column("games_lost", sa.Integer(), nullable=True),
        sa.Column("win_rate", sa.Float(), nullable=True),
        sa.Column("average_wins_by_turn", sa.Integer(), nullable=True),
        sa.Column("decks_count", sa.Integer(), nullable=True),
        sa.Column("games_count", sa.Integer(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("commander_global_stats", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_commander_global_stats_commander_name"),
            ["commander_name"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("commander_global_stats", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_commander_global_stats_commander_name"))
    op.drop_table("commander_global_stats")
