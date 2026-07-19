"""#148 — decks.considering_location_id (per-deck "Considering" holding area)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-19 00:00:00.000000

Additive, nullable, indexed FK to ``storage_locations.id`` (ON DELETE SET NULL).
Points a deck at its optional "Considering" StorageLocation (``type='considering'``)
— a holding area for cards being evaluated while brewing, kept SEPARATE from the
deck proper so every existing "cards in this deck" query (which filters on
``deck.storage_location_id``) auto-excludes them. Lazily created on first add.
NULL = no considering area yet. The ``considering`` location type is enforced at
the service layer (``VALID_LOCATION_TYPES``), not with a DB CHECK — same pattern as
the other location types — so no enum migration is needed here.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "decks",
        sa.Column("considering_location_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_decks_considering_location_id", "decks", ["considering_location_id"])
    op.create_foreign_key(
        "fk_decks_considering_location_id",
        "decks",
        "storage_locations",
        ["considering_location_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_decks_considering_location_id", "decks", type_="foreignkey")
    op.drop_index("ix_decks_considering_location_id", table_name="decks")
    op.drop_column("decks", "considering_location_id")
