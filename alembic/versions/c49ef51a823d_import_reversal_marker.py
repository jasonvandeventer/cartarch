"""Record import reversals so retries cannot subtract inventory twice.

Revision ID: c49ef51a823d
Revises: da5cbaf6aa20
"""

import sqlalchemy as sa

from alembic import op

revision = "c49ef51a823d"
down_revision = "da5cbaf6aa20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transaction_logs", sa.Column("reversed_at", sa.DateTime(timezone=True)))
    # Older undo events named their original log in the note. Preserve those
    # reversals, including the legacy placement-merge no-op: replaying an old
    # undo cannot safely reconstruct where those copies subsequently moved.
    op.execute("""
        UPDATE transaction_logs AS original
        SET reversed_at = undone.created_at
        FROM (
            SELECT user_id,
                   substring(note from '^Undid import log ([0-9]+)') AS original_id,
                   min(created_at) AS created_at
            FROM transaction_logs
            WHERE event_type IN ('undo_import', 'undo_batch_import')
              AND note ~ '^Undid import log [0-9]+( from batch [0-9]+)?$'
            GROUP BY user_id, substring(note from '^Undid import log ([0-9]+)')
        ) AS undone
        WHERE original.id::text = undone.original_id
          AND original.user_id = undone.user_id
          AND original.event_type = 'import'
    """)


def downgrade() -> None:
    op.drop_column("transaction_logs", "reversed_at")
