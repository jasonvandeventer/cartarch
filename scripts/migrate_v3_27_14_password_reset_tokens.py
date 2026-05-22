"""Password reset tokens schema (v3.27.14).

Adds one additive table for the self-service password recovery feature.
Each row represents one outstanding (or used) reset token. The raw
token is NEVER stored — only the SHA-256 hash. The emailed link is the
only place the raw token exists.

Token lifecycle (enforced at the service layer, not at the DB):

- Generate via ``secrets.token_urlsafe(32)`` — cryptographically random,
  ~43 base64 characters. High entropy means SHA-256 (fast hash) is
  correct here; we're not protecting a low-entropy user secret, so a
  slow password hash is not needed and would just make verification
  slower.
- Store ``hashlib.sha256(token).hexdigest()`` in ``token_hash`` (64 hex
  chars). Look-up by hash on the reset path.
- Lifetime: 30 minutes from creation — ``expires_at`` is set at insert
  time, checked at validation time.
- Single-use: ``used_at`` is set on successful reset; a row with
  ``used_at IS NOT NULL`` never validates again.
- Invalidate-on-new-request: when a user requests a NEW reset, the
  service layer DELETEs the user's existing unused tokens before
  inserting the new one — guarantees at most one outstanding token per
  user.

Idempotent — ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT
EXISTS``. No backfill (no historical data to migrate). The migration
registry's ``_is_applied`` check provides the outer idempotency layer
as well; this script is safe to re-run at the SQL level too.

``user_id`` is a documentary FK only (project doesn't enable
``PRAGMA foreign_keys`` — same pattern v3.27.5 / v3.27.12 established
for this codebase). The user-deletion cascade in
``app/routes/admin.py:delete_user`` explicitly DELETEs the user's
password_reset_tokens rows before deleting the user row. Plain delete
— no historical retention value (tokens have no "X reset Y's password"
meaning to preserve, unlike v3.27.5's ``GameSeat.user_name_at_game``
snapshot).
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    used_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        # Index on token_hash for the reset-path lookup (user submits
        # the raw token via the email link; we hash it and look up).
        # Not UNIQUE — token_urlsafe(32) collisions are astronomically
        # unlikely but enforcing uniqueness would add a write-time
        # check we don't need.
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_token_hash "
                "ON password_reset_tokens(token_hash)"
            )
        )
        # Note: the user_id index lands via Base.metadata.create_all()
        # in init_db() from the model's index=True annotation. Same
        # pattern as v3.27.12 watchlist — don't duplicate here.
        print("v3.27.14 password_reset_tokens migration: table + 1 index applied")


if __name__ == "__main__":
    main()
