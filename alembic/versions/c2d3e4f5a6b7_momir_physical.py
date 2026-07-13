"""games.momir_physical — Momir physical-table mode (#113)

Additive nullable boolean: a Momir game played with real basic-land decks skips
the app's digital mana/hand/library tracking. Plain ADD COLUMN — SQLite + Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("games", sa.Column("momir_physical", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("games", "momir_physical")
