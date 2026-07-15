"""#123 — game_changer_cards: date-real rules_version stamps

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-14 00:00:00.000000

Data-only. The Game Changers LIST is versioned by date ('2026-02-09' = the
original official list); the legacy '1.0.0' stamps on game_changer_cards
migrate to that date. commander_bracket_rules keeps '1.0.0' (the bracket-rules
STRUCTURE version — a separate stream). deck_bracket_estimates rows are left
untouched on purpose: their '1.0.0' stamps now differ from gc_list_version(),
so the combo-refresh daemon re-floors every deck once — which also backfills
floor_bracket for pre-#121 estimates.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0e1f2a3b4c5"
down_revision: str | Sequence[str] | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE game_changer_cards SET rules_version = '2026-02-09' WHERE rules_version = '1.0.0'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE game_changer_cards SET rules_version = '1.0.0' WHERE rules_version = '2026-02-09'"
    )
