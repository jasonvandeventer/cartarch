"""cards.produced_mana — Scryfall produced_mana for goldfish auto-mana (#100)

Additive nullable Text column (JSON array text like ``keywords``). NULL = not yet
backfilled; the passive trait-backfill loop populates it for owned cards, and every
card-write path sets it going forward. SQLite + Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f5a6b7c8d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cards", sa.Column("produced_mana", sa.Text(), nullable=True))
    # 28th column of the byte-identical scryfall_cards cache seam.
    op.add_column("scryfall_cards", sa.Column("produced_mana", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("scryfall_cards", "produced_mana")
    op.drop_column("cards", "produced_mana")
