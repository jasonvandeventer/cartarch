"""price history (#98) + watchlist alert prefs/dedup (#99)

Additive: new card_price_history table (per-(scryfall_id, finish, day) resolved
price), users.price_alerts_enabled opt-in, and watchlist.last_alerted_at /
last_alerted_price dedup state. Plain CREATE TABLE / ADD COLUMN — SQLite + Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: str | Sequence[str] | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "card_price_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scryfall_id", sa.String(length=64), nullable=False),
        sa.Column("finish", sa.String(length=16), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scryfall_id", "finish", "snapshot_date", name="uq_card_price_history_day"
        ),
    )
    with op.batch_alter_table("card_price_history", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_card_price_history_scryfall_id"), ["scryfall_id"], unique=False
        )
    op.add_column("users", sa.Column("price_alerts_enabled", sa.Boolean(), nullable=True))
    op.add_column("watchlist", sa.Column("last_alerted_at", sa.DateTime(), nullable=True))
    op.add_column("watchlist", sa.Column("last_alerted_price", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("watchlist", "last_alerted_price")
    op.drop_column("watchlist", "last_alerted_at")
    op.drop_column("users", "price_alerts_enabled")
    with op.batch_alter_table("card_price_history", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_card_price_history_scryfall_id"))
    op.drop_table("card_price_history")
