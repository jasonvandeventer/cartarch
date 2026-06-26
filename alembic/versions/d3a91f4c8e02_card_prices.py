"""card_prices — MTGJSON per-printing/finish price table (MTGJSON ingest issue)

Revision ID: d3a91f4c8e02
Revises: c2f5a83b41d7
Create Date: 2026-06-26 00:00:00.000000

Adds ``card_prices``: one row per ``(scryfall_id, finish)`` holding the daily
MTGJSON USD retail values per provider (tcgplayer / cardkingdom / cardsphere)
plus a manual override that always wins, and ``price_updated_at`` (advances only
when a fresh value arrives, so a transient miss surfaces staleness). This is the
new source of truth for displayed price, replacing Scryfall; the ingest
denormalizes the resolved value back onto ``Card.price_usd*``.

``cardmarket`` (EUR) is deliberately not a column — mixing it into a USD price
corrupts valuation. Prices are TEXT to match the existing ``cards.price_usd*``
columns. ``scryfall_id`` is a plain indexed column (not an FK): cards are global
reference data rarely deleted, and the ingest only ever upserts rows for
printings already in ``cards``.

POST-AUTOGENERATE NOTE (gate #4 pattern): no boolean columns and no
``server_default`` here — nothing to hand-fix beyond confirming the unique
constraint name matches the model's ``uq_card_prices_printing_finish``. Applies
cleanly on Postgres (plain CREATE TABLE; TEXT/DateTime, no SQLite-only types).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3a91f4c8e02"
down_revision: str | Sequence[str] | None = "c2f5a83b41d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "card_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scryfall_id", sa.String(length=64), nullable=False),
        sa.Column("finish", sa.String(length=16), nullable=False),
        sa.Column("tcgplayer_retail", sa.String(length=32), nullable=True),
        sa.Column("cardkingdom_retail", sa.String(length=32), nullable=True),
        sa.Column("cardsphere_retail", sa.String(length=32), nullable=True),
        sa.Column("manual_override", sa.String(length=32), nullable=True),
        sa.Column("price_updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scryfall_id", "finish", name="uq_card_prices_printing_finish"),
    )
    with op.batch_alter_table("card_prices", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_card_prices_scryfall_id"), ["scryfall_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("card_prices", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_card_prices_scryfall_id"))
    op.drop_table("card_prices")
