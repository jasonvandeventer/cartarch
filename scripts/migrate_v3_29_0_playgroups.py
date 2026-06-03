"""Playgroups schema (v3.29.0).

Opens the v3.29.x social-features minor. Adds two additive tables that
form the membership substrate the v3.29.1 collection sharing and v3.29.2
pairwise trading releases scope to.

- ``playgroups`` — a membership-based grouping of users. Carries a
  server-generated opaque ``join_code`` (``secrets.token_urlsafe(8)``
  per the v3.27.0 ``Game.client_token`` precedent); NULL = the owner
  toggled the join code off (disabled). Any member may view and share
  the active code; only the owner may regenerate or disable it.
  ``created_by`` is immutable audit and NEVER the authority check —
  the live authority is always ``PlaygroupMember.role == 'owner'``.

- ``playgroup_members`` — the codebase's first explicit M2M join.
  Surrogate primary key + ``UniqueConstraint(playgroup_id, user_id)``;
  matches every other join-bearing table in the schema (no model
  uses a composite PK). ``role`` is a service-layer canonical enum
  (``CANONICAL_PLAYGROUP_ROLES`` in ``app/playgroup_service.py``) —
  the v3.27.2 / v3.27.3 pattern, no DB ``CHECK``. v3.29.0 ships two
  roles, ``owner`` and ``member``; the enum can widen additively
  later (e.g. ``admin``) with no schema change.

**No backfill** — the existing single-tenant install starts with zero
playgroups. The picker's C2 fallback (``get_pickable_users`` in
``app/playgroup_service.py``) preserves pre-v3.29.0 behavior for users
who haven't joined any playgroup yet, so day-one game creation does
not regress.

**Idempotent** — every ``CREATE TABLE`` / ``CREATE INDEX`` uses
``IF NOT EXISTS``; the registry's ``_is_applied`` gate in
``run_migrations.py`` provides the outer idempotency layer as well.

The partial-unique ``join_code`` index permits multiple disabled
playgroups (each with ``join_code = NULL``) while enforcing
uniqueness across active codes — same partial-index idiom v3.27.12
established for the watchlist XOR uniqueness. SQLite has supported
partial indexes since 3.8.0; project SQLite is far newer.

Per the project SQLite-until-v4 posture: additive tables only, no
existing-table alteration, no ``CHECK`` constraints, no constraint
changes to existing tables. The single new ``ALTER`` the spec
considered (``Game.playgroup_id``, decision C4) was deferred per the
settled register, so this migration touches no existing schema.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS playgroups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(128) NOT NULL,
                    created_by INTEGER NOT NULL REFERENCES users(id),
                    notes TEXT,
                    join_code VARCHAR(32),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        # Regular indexes — also lands via ``Base.metadata.create_all`` in
        # ``init_db()`` from the model's ``index=True`` annotations, but
        # this migration runs first under the startup sequence (per
        # ``app/main.py:on_startup`` — run_migrations → init_db). Declaring
        # the index here keeps the migration self-describing.
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_playgroups_name ON playgroups(name)"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_playgroups_created_by ON playgroups(created_by)")
        )
        # Partial-unique join_code: at most one active code per value across
        # the table; multiple disabled (NULL) playgroups are allowed. Same
        # partial-index pattern v3.27.12 used for the watchlist XOR shape.
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_playgroups_join_code "
                "ON playgroups(join_code) WHERE join_code IS NOT NULL"
            )
        )

        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS playgroup_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playgroup_id INTEGER NOT NULL REFERENCES playgroups(id),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    role VARCHAR(16) NOT NULL DEFAULT 'member',
                    joined_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_playgroup_members_playgroup_id "
                "ON playgroup_members(playgroup_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_playgroup_members_user_id "
                "ON playgroup_members(user_id)"
            )
        )
        # Unique (playgroup_id, user_id) — prevents duplicate memberships;
        # ``join_by_code`` relies on this to make joining idempotent (already
        # a member → IntegrityError caught at the service layer, returns
        # existing playgroup).
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_playgroup_members_pg_user "
                "ON playgroup_members(playgroup_id, user_id)"
            )
        )
        print(
            "v3.29.0 playgroups migration: 2 tables + 5 indexes applied "
            "(playgroups + playgroup_members; partial-unique join_code; "
            "unique-pair membership)"
        )


if __name__ == "__main__":
    main()
