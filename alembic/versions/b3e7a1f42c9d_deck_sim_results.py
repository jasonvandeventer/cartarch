"""deck_sim_results — aggregated AI-simulation strength evidence per deck.

One row per (deck, run_label, strategy): a simulation batch's win/game
aggregate for one pod-selection meta. Seeded at boot from
app/data/sim_results_seed.json (deploy-like-code, same as play profiles).

Revision ID: b3e7a1f42c9d
Revises: f90c38461d0d
Create Date: 2026-08-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3e7a1f42c9d"
down_revision: str | Sequence[str] | None = "f90c38461d0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deck_sim_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deck_id", sa.Integer(), nullable=False),
        sa.Column("run_label", sa.String(length=64), nullable=False),
        sa.Column("strategy", sa.String(length=32), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deck_id", "run_label", "strategy", name="uq_deck_sim_results_run"),
    )
    with op.batch_alter_table("deck_sim_results", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_deck_sim_results_deck_id"), ["deck_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("deck_sim_results", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_deck_sim_results_deck_id"))
    op.drop_table("deck_sim_results")
