"""game_seats.elimination_cause — persist per-seat elimination cause (#114)

Additive nullable string. Auto-tracked cause (life/cmd/poison/deck) or a manual
sub-cause captured at finalize; corrigible via post-finalization result editing.
Plain ADD COLUMN — SQLite + Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | Sequence[str] | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("game_seats", sa.Column("elimination_cause", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("game_seats", "elimination_cause")
