"""deck_combos — persisted Spellbook combo results per deck (#103 Phase A)

One row per deck (UNIQUE deck_id), written only by the combo-refresh daemon.
``fingerprint`` = hash of the played card names (the daemon's change detector);
``payload`` = compute_deck_combos JSON. FK CASCADE is Postgres defense-in-depth
(SQLite FKs off; delete_deck cleans up explicitly).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deck_combos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deck_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deck_id"),
    )


def downgrade() -> None:
    op.drop_table("deck_combos")
