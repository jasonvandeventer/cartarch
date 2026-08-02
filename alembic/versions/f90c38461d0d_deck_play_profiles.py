"""deck_play_profiles — per-deck piloting profile (how to play, not how to build)

Revision ID: f90c38461d0d
Revises: d4e5f6a7b8c9
Create Date: 2026-08-01 00:00:00.000000

One row per deck (``deck_id`` unique): the pilot's intent for the deck —
primary/secondary plan, hard rules, threat priorities — as a JSON blob.

DISTINCT from ``deck_strategy_profiles``, which holds deckbuilding targets
(lands 36-38, ramp 10-14) for the analyzer. Same table shape, different meaning:
that one describes how to CONSTRUCT a deck, this one how to PILOT it. Keeping
them separate is deliberate — merging them would conflate two audiences (the
deck builder vs. the Forge AI-player simulation).

It is also the authoritative place to CORRECT auto-derived data. ``deck_combos``
descriptions are generated and can be wrong — e.g. a terminating kill loop
classified as an infinite draw. A confidently wrong combo description is worse
than none, because a policy reading it avoids its own win condition. The pilot
needs one place to say otherwise that every consumer sees.

``deck_id`` is a NOT NULL, unique-indexed FK to ``decks.id`` ON DELETE CASCADE.
SQLite enforces no FKs (PRAGMA foreign_keys OFF), so ``delete_deck`` deletes the
profile explicitly; the DB CASCADE is Postgres defense-in-depth.

POST-AUTOGENERATE NOTE (gate #4 pattern): ``is_custom`` carries
``server_default=sa.false()`` — a hand-applied fixup (NEVER an integer literal,
which breaks CREATE TABLE on Postgres). Reapply on any regen, same discipline as
deck_strategy_profiles. ``created_at`` / ``updated_at`` are NOT NULL (the ORM
``default=utc_now`` always supplies values). Applies cleanly on Postgres AND
SQLite (plain CREATE TABLE; no SQLite-only types).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f90c38461d0d"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deck_play_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deck_id", sa.Integer(), nullable=False),
        sa.Column("profile_data", sa.Text(), nullable=False),
        sa.Column("is_custom", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("deck_play_profiles", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_deck_play_profiles_deck_id"), ["deck_id"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("deck_play_profiles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_deck_play_profiles_deck_id"))
    op.drop_table("deck_play_profiles")
