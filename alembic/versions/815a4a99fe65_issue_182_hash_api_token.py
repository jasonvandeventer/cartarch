"""#182 — store a hash of the API token, never the token itself.

``users.api_token`` held the bearer token in plaintext, so any database read —
a backup, a support query, a dump — yielded working credentials for every
user's entire collection. Unlike a password, a bearer token is directly
replayable.

**Existing tokens keep working.** The column is renamed and its values are
hashed IN PLACE, so a client that already holds a token is unaffected; only the
stored representation changes. What users lose is the ability to re-read the
token from the account page, which is inherent to not storing it — the page now
shows it once at generation and says so.

``sha256()`` is a Postgres built-in from PG11 on, so the data migration is one
UPDATE with no Python round-trip. Alembic owns the Postgres schema only (the
chain cannot run on SQLite at all — ``a1d7e4f02b93`` issues an ``ALTER COLUMN
... TYPE BOOLEAN``); SQLite dev builds from ``create_all``.

Downgrade renames the column back but CANNOT restore the plaintext — the hash
is one-way. It nulls the values instead, which revokes every token rather than
silently leaving a hash where the code expects a token. Losing API access on a
downgrade is the honest outcome; authenticating against a hash-as-token is not.

Revision ID: 815a4a99fe65
Revises: fb79f10a773e
Create Date: 2026-08-16
"""

from __future__ import annotations

from alembic import op

revision: str = "815a4a99fe65"
down_revision: str | None = "fb79f10a773e"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column("users", "api_token", new_column_name="api_token_hash")
    # Hash what is already there so live tokens survive the migration.
    op.execute(
        """
        UPDATE users
           SET api_token_hash = encode(sha256(api_token_hash::bytea), 'hex')
         WHERE api_token_hash IS NOT NULL
        """
    )


def downgrade() -> None:
    # A hash cannot be turned back into a token. Revoke rather than leave a
    # value that looks like a credential and is not one.
    op.execute("UPDATE users SET api_token_hash = NULL")
    op.alter_column("users", "api_token_hash", new_column_name="api_token")
