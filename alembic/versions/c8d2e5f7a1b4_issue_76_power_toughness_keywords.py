"""#76 — cards + scryfall_cards: power, toughness, keywords

Revision ID: c8d2e5f7a1b4
Revises: a1d7e4f02b93
Create Date: 2026-07-10 00:00:00.000000

Additive, nullable, no default — the v3.36.1 loyalty/defense template three
times over. ``cards`` gets VARCHAR(16) power/toughness (raw Scryfall strings,
can be "*"/"1+*"/"X") + TEXT keywords (JSON array text). ``scryfall_cards``
(the raw-SQL bulk cache, all-TEXT columns) gets the same three as TEXT so the
27-column ``_CACHE_COLUMNS`` upsert has somewhere to land. Backward
compatible: the prior release selects only declared columns and never reads
these.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d2e5f7a1b4"
down_revision: str | Sequence[str] | None = "a1d7e4f02b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cards", sa.Column("power", sa.String(length=16), nullable=True))
    op.add_column("cards", sa.Column("toughness", sa.String(length=16), nullable=True))
    op.add_column("cards", sa.Column("keywords", sa.Text(), nullable=True))
    op.add_column("scryfall_cards", sa.Column("power", sa.Text(), nullable=True))
    op.add_column("scryfall_cards", sa.Column("toughness", sa.Text(), nullable=True))
    op.add_column("scryfall_cards", sa.Column("keywords", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("scryfall_cards", "keywords")
    op.drop_column("scryfall_cards", "toughness")
    op.drop_column("scryfall_cards", "power")
    op.drop_column("cards", "keywords")
    op.drop_column("cards", "toughness")
    op.drop_column("cards", "power")
