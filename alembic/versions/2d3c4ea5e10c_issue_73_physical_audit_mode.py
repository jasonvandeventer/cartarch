"""issue #73 — Physical Audit Mode (audit_sessions, audit_scans, audit_log)

Three tables backing an audit session that reconciles one storage location
against the database. ``audit_sessions`` is the session (one active/paused per
user, enforced in the service layer); ``audit_scans`` records each scan event
(``inventory_row_id`` NULL for extras); ``audit_log`` is the completed-audit
record. All FKs are ON DELETE CASCADE — Postgres defense-in-depth; SQLite runs
with PRAGMA foreign_keys OFF until the v4 cutover, so cascades are a no-op there
(the service deletes scans explicitly on abandon). Plain CREATE TABLE — applies
on SQLite AND Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2d3c4ea5e10c"
down_revision: str | Sequence[str] | None = "a7f3c9e21b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("storage_location_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["storage_location_id"], ["storage_locations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_audit_sessions_user_id"), ["user_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_audit_sessions_storage_location_id"),
            ["storage_location_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_audit_sessions_status"), ["status"], unique=False)

    op.create_table(
        "audit_scans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("audit_session_id", sa.Integer(), nullable=False),
        sa.Column("inventory_row_id", sa.Integer(), nullable=True),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("finish", sa.String(length=32), nullable=False),
        sa.Column("scan_type", sa.String(length=16), nullable=False),
        sa.Column("quantity_scanned", sa.Integer(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["audit_session_id"], ["audit_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_scans", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_audit_scans_audit_session_id"),
            ["audit_session_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_audit_scans_inventory_row_id"),
            ["inventory_row_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_audit_scans_card_id"), ["card_id"], unique=False)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("audit_session_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("storage_location_id", sa.Integer(), nullable=False),
        sa.Column("cards_expected", sa.Integer(), nullable=False),
        sa.Column("cards_seen", sa.Integer(), nullable=False),
        sa.Column("cards_missing", sa.Integer(), nullable=False),
        sa.Column("cards_extra", sa.Integer(), nullable=False),
        sa.Column("actions_applied", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["audit_session_id"], ["audit_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["storage_location_id"], ["storage_locations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_audit_log_audit_session_id"),
            ["audit_session_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_audit_log_user_id"), ["user_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_audit_log_storage_location_id"),
            ["storage_location_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("audit_scans")
    op.drop_table("audit_sessions")
