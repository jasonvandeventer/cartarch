"""companion mode — game_live_states (live mid-game state, first mid-game write)

One row per game (game_id UNIQUE) holding the live tracker JSON blob during
in_progress play — the same shape the localStorage tracker uses. Created by
live_game_service.start_live_game, mutated by apply_live_action, deleted on
finalize / game delete. FK game_id ON DELETE CASCADE is Postgres
defense-in-depth (SQLite runs PRAGMA foreign_keys OFF until the v4 cutover; the
row is dropped in Python via the Game.live_state delete-orphan cascade and
explicitly on finalize). Plain CREATE TABLE — applies on SQLite AND Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2799fb0ad81d"
down_revision: str | Sequence[str] | None = "a7b2971e2820"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "game_live_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", name="uq_game_live_states_game_id"),
    )
    with op.batch_alter_table("game_live_states", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_game_live_states_game_id"), ["game_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("game_live_states", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_game_live_states_game_id"))
    op.drop_table("game_live_states")
