from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``users.last_signed_in_at`` (v3.27.4 — fourth sub-patch of the
    v3.27.0 sequel).

    The Admin page previously showed a "last activity" column derived
    from ``func.max(TransactionLog.created_at)`` per user — a poor proxy
    for actual engagement (users who play games, edit decks, or log in
    regularly but don't touch inventory show stale dates). The label
    "Last Activity" and the data didn't match.

    This column tracks actual sign-in timestamps directly: ``POST
    /login`` writes ``datetime.utcnow()`` on every successful auth.
    ``_build_user_rows`` in ``app/routes/admin.py`` reads the column
    directly, dropping the TransactionLog aggregate subquery. The
    admin template column header changes from "Last Activity" to
    "Last Signed In" to match.

    **No backfill.** The proxy data (max TransactionLog.created_at) is
    not equivalent to actual login dates — it reflects inventory
    activity, not authentication events. Backfilling would import the
    same misleading signal under the new name. Existing users show
    ``—`` (template fallback) until their next login; the value is
    correct from that point forward.

    Additive single-column ALTER, no constraint changes — safe under
    the SQLite-until-v4 constraint. Idempotent: skips if the column
    already exists.
    """
    with engine.begin() as conn:
        if not column_exists(conn, "users", "last_signed_in_at"):
            conn.execute(text("ALTER TABLE users ADD COLUMN last_signed_in_at DATETIME"))
            print("Added users.last_signed_in_at (NULL until next login)")
        else:
            print("users.last_signed_in_at already exists, skipping")


if __name__ == "__main__":
    main()
