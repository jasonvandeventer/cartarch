from __future__ import annotations

from sqlalchemy import text

from app.db import engine
from app.game_service import (
    CANONICAL_GAME_FORMATS,
    DEFAULT_GAME_FORMAT,
    normalize_game_format,
)


def main() -> None:
    """Normalize ``games.format`` to the canonical taxonomy (v3.27.2 —
    Game.format enum normalization).

    Preventive hardening, not corrective. Production data at the time of
    this migration is clean (verified: 5 games, all ``Commander``, 0
    NULLs), so on production the backfill does nothing observable. The
    logic still needs to be correct for the general case — future free-
    text values, case drift, trailing whitespace — and for any installs
    whose history may contain something other than ``Commander``.

    Backfill rules:

    - NULL ``format`` → ``DEFAULT_GAME_FORMAT`` (Commander). Matches the
      new Python-side default; consistent with the v3.25.1 non-blocking
      philosophy (a missing format never blocks game creation, and never
      blocks analytics either).
    - Non-empty values: trim + case-fold + match against
      ``CANONICAL_GAME_FORMATS``. Exact-after-normalization match →
      canonical value. No match → ``Other`` (preserves the signal that
      historical data carried *something* the canonical set doesn't
      know about; explicit ``Other`` is more informative than silently
      collapsing into the default).

    Idempotency: only UPDATEs rows whose stored value DIFFERS from its
    normalized form. Re-running the migration is a no-op for rows
    already at a canonical value, and an unrecognized-pre-normalization
    value backfilled to ``Other`` on the first run stays ``Other`` on
    subsequent runs (``Other`` is itself canonical).

    No schema change — the column stays ``String(64)`` nullable. The
    "enum" constraint is enforced at the service layer
    (``normalize_game_format`` in ``app/game_service.py``), matching the
    existing ``VALID_LOCATION_TYPES`` / ``VALID_LOCATION_MODES`` pattern.
    Adding a DB-level CHECK constraint would require a SQLite table
    rebuild reserved for the v4 Postgres migration.
    """
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, format FROM games")).fetchall()

        normalized_count = 0
        unchanged_count = 0
        null_to_default = 0
        unknown_to_other = 0
        canonical_counts: dict[str, int] = dict.fromkeys(CANONICAL_GAME_FORMATS, 0)

        for game_id, raw_format in rows:
            # Backfill uses unknown_to="Other" so historical free-text
            # values that don't match the canonical set are preserved as
            # a distinct signal. Runtime submission (game_create) uses
            # the default (Commander) instead.
            target = normalize_game_format(raw_format, unknown_to="Other")
            canonical_counts[target] = canonical_counts.get(target, 0) + 1

            if raw_format == target:
                unchanged_count += 1
                continue

            conn.execute(
                text("UPDATE games SET format = :fmt WHERE id = :id"),
                {"fmt": target, "id": game_id},
            )
            normalized_count += 1
            if raw_format is None:
                null_to_default += 1
            elif target == "Other":
                unknown_to_other += 1

        print(
            f"games.format normalization: {len(rows)} total | "
            f"{normalized_count} updated, {unchanged_count} unchanged | "
            f"NULL→{DEFAULT_GAME_FORMAT}: {null_to_default}, "
            f"unknown→Other: {unknown_to_other}"
        )
        for fmt, count in canonical_counts.items():
            if count:
                print(f"  {fmt}: {count}")


if __name__ == "__main__":
    main()
