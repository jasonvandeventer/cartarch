"""live_action_conflicts — persist detected lost updates (#155, for #153)

The v4.12.7 instrumentation logs to stdout only. The cluster runs no log
aggregator and prod restarts on every deploy, so a detected lost update is erased
before anyone can read it — measured 2026-08-07, zero live games had been played
since it shipped, so it had never observed one at all.

Only CLOBBERS land here; the per-action line stays in the log. No FK on
``game_id`` (the ``card_prices.scryfall_id`` precedent): the record should
outlive the game, and a diagnostic must not add an edge to the delete topology.

TEMPORARY — drop with the instrumentation when #153 closes.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3d9da7716f9d"
down_revision: str | Sequence[str] | None = "63ccafbb292e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_action_conflicts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("version_read", sa.Integer(), nullable=False),
        sa.Column("version_written", sa.Integer(), nullable=False),
        sa.Column("already_written", sa.Integer(), nullable=True),
        sa.Column("concurrent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_live_action_conflicts_game_id", "live_action_conflicts", ["game_id"])


def downgrade() -> None:
    op.drop_index("ix_live_action_conflicts_game_id", table_name="live_action_conflicts")
    op.drop_table("live_action_conflicts")
