"""game event history — game_events table + games.win_condition

Phase 1: an append-only game_events log (one row per live action, plus
live_started / finalized bookends), written inside the same transaction as the
state mutation it records. Phase 2: an optional operator-picked win_condition on
games, captured at finalize. FK game_id / seat_id ON DELETE CASCADE are Postgres
defense-in-depth (SQLite runs PRAGMA foreign_keys OFF; the ORM delete-orphan
cascade drops events on game delete). Plain CREATE TABLE + ADD COLUMN — applies
on SQLite AND Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d43e38ef6a"
down_revision: str | Sequence[str] | None = "2799fb0ad81d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "game_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("seat_id", sa.Integer(), nullable=True),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["seat_id"], ["game_seats.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("game_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_game_events_game_id"), ["game_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_game_events_action_type"), ["action_type"], unique=False
        )

    with op.batch_alter_table("games", schema=None) as batch_op:
        batch_op.add_column(sa.Column("win_condition", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("games", schema=None) as batch_op:
        batch_op.drop_column("win_condition")

    with op.batch_alter_table("game_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_game_events_action_type"))
        batch_op.drop_index(batch_op.f("ix_game_events_game_id"))
    op.drop_table("game_events")
