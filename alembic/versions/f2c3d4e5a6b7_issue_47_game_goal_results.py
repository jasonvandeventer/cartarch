"""issue #47 — game_goal_results (per-game goal completion, Feature 2 of 2)

Revision ID: f2c3d4e5a6b7
Revises: e1b2c3d4f5a6
Create Date: 2026-06-27 00:00:00.000000

Adds ``game_goal_results``: per-game completion of a deck goal. The grain is the
SEAT (one deck in one game) — ``game_seat_id`` + ``deck_goal_id``, both NOT NULL,
indexed FKs ON DELETE CASCADE. ``UNIQUE(game_seat_id, deck_goal_id)`` makes the
finalize upsert idempotent. FKs ``deck_goals.id`` so this revision MUST come
after #46 (``e1b2c3d4f5a6``).

SQLite enforces no FKs (PRAGMA foreign_keys OFF), so the cascades are also done
in service code: the game/seat side by the ORM delete-orphan cascade
(``GameSeat.goal_results`` / ``Game.seats``), the goal/deck side by explicit
cleanup in ``deck_service`` (``delete_deck_goal`` + ``delete_deck``). The DB
CASCADE here is Postgres defense-in-depth.

POST-AUTOGENERATE NOTE (gate #4 pattern): ``achieved`` carries
``server_default=sa.false()`` — a hand-applied fixup (NEVER an integer literal,
which breaks CREATE TABLE on Postgres). Reapply it on any regen. ``created_at``
is NOT NULL (the ORM ``default=utc_now`` always supplies a value). Applies
cleanly on Postgres AND SQLite (plain CREATE TABLE; no SQLite-only types).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c3d4e5a6b7"
down_revision: str | Sequence[str] | None = "e1b2c3d4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "game_goal_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_seat_id", sa.Integer(), nullable=False),
        sa.Column("deck_goal_id", sa.Integer(), nullable=False),
        sa.Column("achieved", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["game_seat_id"], ["game_seats.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deck_goal_id"], ["deck_goals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_seat_id", "deck_goal_id", name="uq_game_goal_results_seat_goal"),
    )
    with op.batch_alter_table("game_goal_results", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_game_goal_results_game_seat_id"), ["game_seat_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_game_goal_results_deck_goal_id"), ["deck_goal_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("game_goal_results", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_game_goal_results_deck_goal_id"))
        batch_op.drop_index(batch_op.f("ix_game_goal_results_game_seat_id"))
    op.drop_table("game_goal_results")
