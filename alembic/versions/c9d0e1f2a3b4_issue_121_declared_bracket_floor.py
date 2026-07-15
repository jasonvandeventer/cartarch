"""#121 — decks.declared_bracket + deck_bracket_estimates.floor_bracket

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-14 00:00:00.000000

Additive, nullable, no default. ``decks.declared_bracket`` is the owner's
declaration (1-5; NULL = undeclared — the UI prompts, never fills).
``deck_bracket_estimates.floor_bracket`` is the computed minimum the deck's
contents impose on what may be declared (pure function over hard findings).
The blended score/confidence columns stay for one release for migration
simplicity; no user surface reads them after #121.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decks", sa.Column("declared_bracket", sa.Integer(), nullable=True))
    op.add_column("deck_bracket_estimates", sa.Column("floor_bracket", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("deck_bracket_estimates", "floor_bracket")
    op.drop_column("decks", "declared_bracket")
