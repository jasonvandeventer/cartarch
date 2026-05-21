from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``games.status`` enum column (v3.27.3 — third sub-patch of the
    v3.27.0 sequel).

    Closes the "created-but-abandoned vs finalized-with-placements" gap.
    Before this patch, a game with no seat placements was indistinguishable
    from one that ended (``is_ended`` was derived as "any seat has
    placement" everywhere it was checked). After this patch, ``Game.status``
    is the explicit source of truth.

    Canonical taxonomy (see ``CANONICAL_GAME_STATUSES`` in
    ``app/game_service.py``): ``created``, ``in_progress``, ``finalized``,
    ``abandoned``. Backfill rule:

    - Games with at least one seat placement → ``finalized``.
    - Games with zero seat placements → ``abandoned``.

    The third value ``created`` is the Python-side default for newly-
    inserted rows going forward (see ``Game.status`` in ``app/models.py``);
    it's not used by the backfill because historical pre-patch rows are
    by definition either finalized (have placements) or abandoned (don't).
    ``in_progress`` is reserved for a future tracker-server integration.

    Additive single-column ALTER + Python-loop backfill — safe under
    SQLite-until-v4. Idempotent at two levels: ADD COLUMN skips if the
    column exists, and the backfill UPDATE only touches rows where
    ``status IS NULL`` (so manual re-runs are no-ops even outside the
    registry).
    """
    with engine.begin() as conn:
        if not column_exists(conn, "games", "status"):
            conn.execute(text("ALTER TABLE games ADD COLUMN status TEXT"))
            print("Added games.status (NULL until backfill below)")
        else:
            print("games.status already exists, skipping ADD COLUMN")

        # Idempotent backfill: only touches rows where status is still NULL.
        # Manual re-runs are no-ops once the migration has completed once.
        rows = conn.execute(
            text(
                "SELECT g.id, COUNT(s.placement) AS placement_count "
                "FROM games g LEFT JOIN game_seats s ON s.game_id = g.id "
                "WHERE g.status IS NULL "
                "GROUP BY g.id"
            )
        ).fetchall()

        if not rows:
            print("No games need status backfill, skipping")
            return

        finalized = 0
        abandoned = 0
        for game_id, placement_count in rows:
            new_status = "finalized" if placement_count else "abandoned"
            conn.execute(
                text("UPDATE games SET status = :status WHERE id = :id"),
                {"status": new_status, "id": game_id},
            )
            if new_status == "finalized":
                finalized += 1
            else:
                abandoned += 1

        print(
            f"games.status backfill: {len(rows)} backfilled "
            f"({finalized} finalized, {abandoned} abandoned)"
        )


if __name__ == "__main__":
    main()
