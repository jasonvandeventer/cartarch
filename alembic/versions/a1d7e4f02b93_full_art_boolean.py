"""scryfall_cards.full_art: integer -> boolean

Revision ID: a1d7e4f02b93
Revises: f2c3d4e5a6b7
Create Date: 2026-06-28 00:00:00.000000

The ingest wrote a Python ``bool`` into ``scryfall_cards.full_art`` (an INTEGER
column). SQLite coerced ``bool``->0/1; Postgres does not, so the whole 24-column
batch upsert failed every cycle with ``DatatypeMismatch`` and every cache field
went stale (prices, legalities, image_url included). The column's semantics were
always boolean and every reader already wraps it in ``bool(...)`` (recon: no raw
``= 1`` comparison exists), so promote the column to native Boolean to match the
value being written.

SQLite is a no-op-on-type DB; ``postgresql_using`` carries the real cast on PG.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1d7e4f02b93"
down_revision: str | Sequence[str] | None = "f2c3d4e5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "scryfall_cards",
        "full_art",
        type_=sa.Boolean(),
        existing_type=sa.Integer(),
        existing_nullable=True,
        postgresql_using="full_art <> 0",
    )


def downgrade() -> None:
    op.alter_column(
        "scryfall_cards",
        "full_art",
        type_=sa.Integer(),
        existing_type=sa.Boolean(),
        existing_nullable=True,
        postgresql_using="full_art::int",
    )
