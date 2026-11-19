"""File already-assigned matching copies at their earliest drawer slot."""

from alembic import op

revision = "af01c584e72b"
down_revision = "d76b3120c9a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        WITH slots AS (
            SELECT r.id, first_value(r.slot) OVER (
                PARTITION BY r.user_id, r.storage_location_id, r.drawer, r.card_id,
                             r.finish, coalesce(nullif(r.language, ''), 'en'), r.is_proxy
                ORDER BY length(r.slot), r.slot, r.id
            ) AS target
            FROM inventory_rows r
            JOIN storage_locations l ON l.id = r.storage_location_id AND l.user_id = r.user_id
            WHERE l.type = 'drawer' AND r.drawer IS NOT NULL AND r.slot ~ '^[1-9][0-9]*$'
        )
        UPDATE inventory_rows r SET slot = slots.target FROM slots
        WHERE r.id = slots.id AND r.slot <> slots.target
    """)


def downgrade() -> None:
    # Both versions accept shared slots. The earlier per-row numbers are unrecoverable.
    pass
