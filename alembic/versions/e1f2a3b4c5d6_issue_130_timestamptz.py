"""#130 — flip naive datetime columns to timezone-aware timestamptz

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-15 00:00:00.000000

The deferred v3.36.13 single-flip-point. ``utc_now()`` now returns aware UTC and
the models use ``timestamptz`` (the ``UTCDateTime`` type), so every naive
``timestamp without time zone`` column is converted here.

Assumption gate (pre-cleared, do not re-derive — PROVEN 2026-07-15 against live
prod): naive stored values are UTC by the project's long-standing convention.
Evidence: ``deck_bracket_estimates.generated_at`` read "2026-07-15 13:04:49" at a
wall-clock moment of ~09:45 Central — a local-Central reading would put it in the
future (impossible), so the value is UTC (13:04 UTC = 08:04 Central, the daemon
cadence). Corroboration: the 2026-07-12 session games read 16:06–19:52 UTC = an
11 AM–3 PM Central Saturday afternoon. Consequence: the conversion is a straight
``USING col AT TIME ZONE 'UTC'`` — no offset correction anywhere.

Enumerated from ``information_schema`` at runtime (not hardcoded), so it stays
correct as columns come and go. Idempotent: columns already ``timestamptz`` are
never selected, so a re-run is a no-op and the one pre-existing tz column
(``deck_card_shares.created_at``, b7c4e1a9d2f3 / #27) is left untouched. Table
sizes are modest (largest: inventory_rows / game_events / card_price_history, all
< 100k rows), so a straight in-place ALTER under the PreSync hook is fine — no
batching. Postgres-only; SQLite/test schemas are built from the models, not this.

Deploy ordering (the honest version): PreSync applies this migration BEFORE the
pod swap, so for the sync window the OUTGOING (old, naive-code) pod serves
traffic against timestamptz columns and receives aware datetimes. Its display
filter and aware-vs-aware arithmetic tolerate that; naive-vs-aware comparisons in
auth-adjacent paths (login throttle, password-reset expiry, last-active) can
raise TypeError during the window. Accepted knowingly: ~9 users, sub-two-minute
window, self-healing at the swap. What IS guaranteed: the new code never sees
naive columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The one column that was already timestamptz before this migration (#27). It is
# never selected by the upgrade (data_type filter), and downgrade must not revert
# it — it did not become naive here.
_PREEXISTING_TZ = {("deck_card_shares", "created_at")}


def _public_columns(bind, data_type: str) -> list[tuple[str, str]]:
    rows = bind.execute(
        sa.text(
            "SELECT c.table_name, c.column_name "
            "FROM information_schema.columns c "
            "JOIN information_schema.tables t "
            "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE c.table_schema = 'public' "
            "  AND t.table_type = 'BASE TABLE' "
            "  AND c.data_type = :dt "
            "ORDER BY c.table_name, c.column_name"
        ),
        {"dt": data_type},
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table_name, column_name in _public_columns(bind, "timestamp without time zone"):
        op.execute(
            f'ALTER TABLE "{table_name}" '
            f'ALTER COLUMN "{column_name}" TYPE timestamptz '
            f"USING \"{column_name}\" AT TIME ZONE 'UTC'"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table_name, column_name in _public_columns(bind, "timestamp with time zone"):
        if (table_name, column_name) in _PREEXISTING_TZ:
            continue
        op.execute(
            f'ALTER TABLE "{table_name}" '
            f'ALTER COLUMN "{column_name}" TYPE timestamp '
            f"USING \"{column_name}\" AT TIME ZONE 'UTC'"
        )
