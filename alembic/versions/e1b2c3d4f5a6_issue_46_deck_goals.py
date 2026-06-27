"""issue #46 — deck_goals (per-deck goals, Feature 1 of 2)

Revision ID: e1b2c3d4f5a6
Revises: d3a91f4c8e02
Create Date: 2026-06-26 00:00:00.000000

Adds ``deck_goals``: a custom, ordered list of what a deck is trying to do
(e.g. "Win by combo"), SEPARATE from win rate and DISTINCT from the
``decks.intent_*`` columns. Removal is a soft-delete (``is_active=False``);
hard delete is a separate explicit action. Feature 2 (#47) FKs this table for
per-game completion tracking, so this table ships + releases first.

``deck_id`` is a NOT NULL, indexed FK to ``decks.id`` ON DELETE CASCADE (a goal
is meaningless without its deck). SQLite enforces no FKs (PRAGMA foreign_keys
OFF), so ``delete_deck`` deletes goals explicitly; the DB CASCADE is Postgres
defense-in-depth.

POST-AUTOGENERATE NOTE (gate #4 pattern): ``is_active`` carries
``server_default=sa.true()`` — a hand-applied fixup (NEVER an integer literal,
which breaks CREATE TABLE on Postgres). Reapply it on any regen, same discipline
as the baseline's ``sa.false()/sa.true()`` boolean fixups. ``position`` carries
``server_default=sa.text("0")`` so the DB enforces the spec's ``default 0`` for
non-ORM inserts (an Integer literal default is fine here — only BOOLEAN breaks on
PG). ``created_at`` is
NOT NULL (the ORM ``Mapped[datetime]`` + ``default=utc_now`` always supplies a
value) — ``alembic check`` is clean against the model. Applies cleanly on
Postgres AND SQLite (plain CREATE TABLE; no SQLite-only types).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1b2c3d4f5a6"
down_revision: str | Sequence[str] | None = "d3a91f4c8e02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deck_goals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deck_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("deck_goals", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_deck_goals_deck_id"), ["deck_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("deck_goals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_deck_goals_deck_id"))
    op.drop_table("deck_goals")
