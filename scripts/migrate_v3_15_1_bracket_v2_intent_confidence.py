"""V2 of the Bracket Estimator: persistent intent survey + confidence.

Adds:
  - 5 intent_* columns on `decks` for the per-deck intent survey response
  - 4 confidence_* columns on `deck_bracket_estimates` for the multi-
    dimensional confidence values

Intent is per-deck (sticky across estimates). Confidence is per-estimate
(recomputed every time the bracket is re-evaluated).
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine

INTENT_COLUMNS = [
    ("intent_pod", "VARCHAR(16)"),
    ("intent_speed", "VARCHAR(16)"),
    ("intent_combo", "VARCHAR(16)"),
    ("intent_winning", "VARCHAR(16)"),
    ("intent_played", "VARCHAR(16)"),
]

CONFIDENCE_COLUMNS = [
    ("confidence_tagging_coverage", "REAL"),
    ("confidence_mechanics_clarity", "REAL"),
    ("confidence_intent_alignment", "REAL"),
    ("confidence_combo_detection_depth", "REAL"),
]


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def main() -> None:
    with engine.begin() as conn:
        deck_cols = _existing_columns(conn, "decks")
        for col, col_type in INTENT_COLUMNS:
            if col not in deck_cols:
                conn.execute(text(f"ALTER TABLE decks ADD COLUMN {col} {col_type}"))
        est_cols = _existing_columns(conn, "deck_bracket_estimates")
        for col, col_type in CONFIDENCE_COLUMNS:
            if col not in est_cols:
                conn.execute(
                    text(f"ALTER TABLE deck_bracket_estimates ADD COLUMN {col} {col_type}")
                )
    print("Added 5 intent_* columns to decks and 4 confidence_* columns to deck_bracket_estimates")


if __name__ == "__main__":
    main()
