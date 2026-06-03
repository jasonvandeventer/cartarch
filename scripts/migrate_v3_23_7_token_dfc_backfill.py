"""One-shot data fixup: coerce ``is_double_sided=True`` on TokenInventory
rows whose back-face fields are populated.

Before v3.23.7, the new-token form happily saved ``back_set_code`` and
``back_collector_number`` even when the user forgot to tick the
"Double-sided" checkbox. The row landed with ``is_double_sided=False``,
which made the Sets page's ``_get_owned_token_map`` skip the back side
(filter requires ``is_double_sided.is_(True)``) — so the back face read as
Missing even though the user owned it.

v3.23.7 fixes the create / update paths via ``_infer_double_sided`` in
``app/token_service.py``. This migration cleans up the rows already in
that broken state. Pure SQL via ``engine.begin()`` so it runs even when
SQLAlchemy session-level state isn't initialized.

Idempotent: the WHERE clause filters to rows where the invariant is
violated, so re-running is a no-op.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE token_inventory
                SET is_double_sided = 1
                WHERE is_double_sided = 0
                  AND back_set_code IS NOT NULL
                  AND TRIM(back_set_code) != ''
                  AND back_collector_number IS NOT NULL
                  AND TRIM(back_collector_number) != ''
                """
            )
        )
        print(
            f"v3.23.7 token DFC backfill: {result.rowcount} row(s) coerced to is_double_sided=True"
        )


if __name__ == "__main__":
    main()
