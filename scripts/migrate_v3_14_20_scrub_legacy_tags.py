"""Strip legacy 'Combo' and 'Payoff' tag values from inventory_rows.tags JSON.

The v3.14.13 taxonomy redesign removed Combo and Payoff in favor of Engine,
Synergy, Threat, and Hate. `get_row_tags()` filters legacy values out at read
time, but the bytes persist in the JSON column until a row is re-saved. This
one-shot migration cleans them up so the on-disk state matches what the app
shows.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from app.db import engine

VALID_TAGS = {
    "Ramp",
    "Draw",
    "Tutor",
    "Removal",
    "Wipe",
    "Protection",
    "Engine",
    "Synergy",
    "Threat",
    "Hate",
}


def main() -> None:
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, tags FROM inventory_rows WHERE tags IS NOT NULL")
        ).fetchall()

        scrubbed = 0
        for row_id, tags_json in rows:
            try:
                tags = json.loads(tags_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(tags, list):
                continue
            cleaned = sorted({t for t in tags if t in VALID_TAGS})
            if cleaned == sorted(tags):
                continue
            new_value = json.dumps(cleaned) if cleaned else None
            conn.execute(
                text("UPDATE inventory_rows SET tags = :tags WHERE id = :id"),
                {"tags": new_value, "id": row_id},
            )
            scrubbed += 1

        print(f"Scrubbed legacy tags from {scrubbed} inventory rows")


if __name__ == "__main__":
    main()
