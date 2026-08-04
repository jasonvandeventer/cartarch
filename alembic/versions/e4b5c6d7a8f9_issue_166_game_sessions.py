"""#166 — game_sessions + games.session_id (play sessions, playgroup-scoped)

Revision ID: e4b5c6d7a8f9
Revises: c1a2b3d4e5f6
Create Date: 2026-08-04 00:00:00.000000

A session is one evening at one table: an ordered set of games belonging to a
PLAYGROUP. Scoping by playgroup rather than by date is the whole point — see the
``GameSession`` docstring for the 2026-06-28 case where date clustering demonstrably
folds a foreign game into a playgroup's session.

``uq_game_sessions_open_per_playgroup`` is a PARTIAL unique index
(``WHERE ended_at IS NULL``), the same posture as ``uq_games_join_code`` and
``uq_decks_user_name``: at most one OPEN session per playgroup, while closed ones
repeat freely. Without the predicate a playgroup could hold exactly one session ever.

``games.session_id`` is nullable and ``ON DELETE SET NULL`` — a game with no
playgroup has no session to join (permanent and correct, not a backfill gap), and
deleting a session must never take its games with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b5c6d7a8f9"
down_revision: str | Sequence[str] | None = "c1a2b3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "game_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("playgroup_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["playgroup_id"], ["playgroups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_game_sessions_playgroup_id", "game_sessions", ["playgroup_id"], unique=False
    )
    op.create_index(
        "uq_game_sessions_open_per_playgroup",
        "game_sessions",
        ["playgroup_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
        sqlite_where=sa.text("ended_at IS NULL"),
    )
    op.add_column("games", sa.Column("session_id", sa.Integer(), nullable=True))
    op.create_index("ix_games_session_id", "games", ["session_id"], unique=False)
    op.create_foreign_key(
        "fk_games_session_id", "games", "game_sessions", ["session_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_games_session_id", "games", type_="foreignkey")
    op.drop_index("ix_games_session_id", table_name="games")
    op.drop_column("games", "session_id")
    op.drop_index("uq_game_sessions_open_per_playgroup", table_name="game_sessions")
    op.drop_index("ix_game_sessions_playgroup_id", table_name="game_sessions")
    op.drop_table("game_sessions")
