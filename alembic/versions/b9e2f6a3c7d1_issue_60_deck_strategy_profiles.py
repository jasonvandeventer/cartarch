"""issue #60 P3 — deck_strategy_profiles (persisted analyzer strategy profile)

Revision ID: b9e2f6a3c7d1
Revises: c8d2e5f7a1b4
Create Date: 2026-07-10 00:00:00.000000

One row per deck (``deck_id`` unique): the JSON strategy profile the deck
analyzer evaluates against (high/medium/low role lists + coverage targets).
``is_custom=False`` = auto-seeded; ``True`` = user-edited (never silently
re-seeded over).

``deck_id`` is a NOT NULL, unique-indexed FK to ``decks.id`` ON DELETE CASCADE.
SQLite enforces no FKs (PRAGMA foreign_keys OFF), so ``delete_deck`` deletes the
profile explicitly; the DB CASCADE is Postgres defense-in-depth.

POST-AUTOGENERATE NOTE (gate #4 pattern): ``is_custom`` carries
``server_default=sa.false()`` — a hand-applied fixup (NEVER an integer literal,
which breaks CREATE TABLE on Postgres). Reapply on any regen, same discipline as
deck_goals' ``sa.true()``. ``created_at`` / ``updated_at`` are NOT NULL (the ORM
``default=utc_now`` always supplies values). Applies cleanly on Postgres AND
SQLite (plain CREATE TABLE; no SQLite-only types).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9e2f6a3c7d1"
down_revision: str | Sequence[str] | None = "c8d2e5f7a1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deck_strategy_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deck_id", sa.Integer(), nullable=False),
        sa.Column("profile_data", sa.Text(), nullable=False),
        sa.Column("is_custom", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("deck_strategy_profiles", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_deck_strategy_profiles_deck_id"), ["deck_id"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("deck_strategy_profiles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_deck_strategy_profiles_deck_id"))
    op.drop_table("deck_strategy_profiles")
