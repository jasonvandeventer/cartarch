"""Multi-showcase: drop the one-per-user constraint (v3.31.0).

The v3.29.1 ``showcases`` schema enforced one Showcase per user via a
``UNIQUE(user_id)`` index (``uq_showcases_user``) — decision A5. v3.31.0
lifts that cap so a user can curate several Showcases for different
purposes (a trade binder, a brag list, a sell pile, …).

The constraint was created as a *standalone* unique index (see
``migrate_v3_29_1_collection_sharing``: ``CREATE UNIQUE INDEX
uq_showcases_user ON showcases(user_id)``), NOT as an inline table
constraint. That is the lucky part: SQLite cannot ``ALTER TABLE ... DROP
CONSTRAINT``, but a standalone index is removed with a plain ``DROP
INDEX`` — no table rebuild, fully in keeping with the project's
SQLite-until-v4 "additive / no-rebuild" posture.

The non-unique helper index ``ix_showcases_user_id`` is left in place —
``user_id`` is still the hot lookup column ("all of this user's
showcases"), it just no longer carries a uniqueness guarantee.

**Idempotent** — ``DROP INDEX IF EXISTS`` is a no-op when the index is
already gone (fresh databases built from the v3.31.0 model never create
it); the registry's ``_is_applied`` gate in ``run_migrations.py`` is the
outer idempotency layer.

**No backfill** — existing single Showcases are already valid rows; they
simply stop being the only one a user may own.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_showcases_user"))
        print(
            "v3.31.0 multi-showcase migration: dropped UNIQUE index "
            "uq_showcases_user (one-per-user cap lifted; "
            "ix_showcases_user_id retained)"
        )


if __name__ == "__main__":
    main()
