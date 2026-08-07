"""cards.edhrec_rank — Scryfall's EDHREC popularity rank (#180)

Additive nullable Integer column, mirroring a7b8c9d0e1f2 (produced_mana). NULL =
not yet backfilled OR no EDHREC rank exists for the printing; the passive
price-refresh / trait-backfill loops populate it for owned cards, and every
card-write path sets it going forward. SQLite + Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "63ccafbb292e"
down_revision: str | Sequence[str] | None = "a9d1c3e57f24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cards", sa.Column("edhrec_rank", sa.Integer(), nullable=True))
    # 29th column of the byte-identical scryfall_cards cache seam.
    op.add_column("scryfall_cards", sa.Column("edhrec_rank", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("scryfall_cards", "edhrec_rank")
    op.drop_column("cards", "edhrec_rank")
