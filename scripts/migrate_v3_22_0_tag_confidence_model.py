"""Backfill ``InventoryRow.tags`` from the legacy ``list[str]`` shape to the
v3.22.0 structured shape ``list[dict]`` with ``{tag, confidence, source}``.

Heuristic per the design doc (docs/tag_system_overhaul.md §2.4): we can't
distinguish between auto-tagger output and user-confirmed tags in legacy
data, so we approximate.

For each row with non-null tags:
  - If the JSON is already a list-of-dicts (idempotent re-run), skip.
  - Otherwise, run the current `suggest_card_roles` against the card.
  - Tags in the user's list that the auto-tagger ALSO suggests get
    `source=auto, confidence=medium` (likely auto-tagger output that
    the user hasn't explicitly touched).
  - Tags the user has that the auto-tagger does NOT suggest get
    `source=user, confidence=high` (user must have added them manually
    since the auto-tagger wouldn't produce them).

Caveats:
  - The auto-tagger is called without commander themes — same conservative
    setup the v3.9.8 auto-tag-on-deck-load uses. A user-tagged "Synergy"
    that a themes-aware auto-tagger would now suggest will land as
    user/high here. Acceptable approximation for a one-shot migration.
  - The migration walks every InventoryRow with tags, including non-deck
    rows (drawer/binder/box rows that have been tagged at some point).
    suggest_card_roles is pure-regex against type_line/oracle_text, so
    no Scryfall calls.

Idempotent: a re-run finds nothing to update because the first run leaves
every row in the structured shape.
"""

from __future__ import annotations

import json

from app.db import engine


def _is_already_structured(raw_json: str) -> bool:
    """True when the JSON value is already in the v3.22.0 dict-list shape."""
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(data, list):
        return False
    if not data:
        # Empty list — treat as already-migrated (NULL is the canonical
        # empty form anyway, so this is a defensive case).
        return True
    return all(isinstance(entry, dict) and "tag" in entry for entry in data)


def main() -> None:
    # Local imports — the migration runs at startup after the SQLAlchemy
    # engine is ready, so app.* modules are importable.
    from sqlalchemy.orm import Session, joinedload

    from app.deck_service import suggest_card_roles
    from app.models import InventoryRow

    rewritten = 0
    skipped_structured = 0
    skipped_empty = 0
    cleared_invalid = 0

    with Session(engine) as session:
        rows = (
            session.query(InventoryRow)
            .options(joinedload(InventoryRow.card))
            .filter(InventoryRow.tags.isnot(None))
            .all()
        )

        for row in rows:
            raw = row.tags
            if not raw or not raw.strip():
                skipped_empty += 1
                continue

            if _is_already_structured(raw):
                skipped_structured += 1
                continue

            try:
                legacy = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                # Malformed legacy data — nothing to salvage. Clear it.
                row.tags = None
                cleared_invalid += 1
                continue

            if not isinstance(legacy, list):
                row.tags = None
                cleared_invalid += 1
                continue

            user_tags = [t for t in legacy if isinstance(t, str)]
            if not user_tags:
                row.tags = None
                skipped_empty += 1
                continue

            # Heuristic split: tags the auto-tagger ALSO produces → auto/medium;
            # tags only the user has → user/high.
            auto_suggested = set(suggest_card_roles(row.card)) if row.card else set()
            structured = []
            for t in user_tags:
                if t in auto_suggested:
                    structured.append({"tag": t, "confidence": "medium", "source": "auto"})
                else:
                    structured.append({"tag": t, "confidence": "high", "source": "user"})
            structured.sort(key=lambda d: d["tag"])
            row.tags = json.dumps(structured)
            rewritten += 1

        session.commit()

    print(
        f"v3.22.0 tag-confidence backfill complete: "
        f"{rewritten} rows rewritten, {skipped_structured} already structured, "
        f"{skipped_empty} empty/cleared, {cleared_invalid} invalid JSON cleared."
    )


if __name__ == "__main__":
    main()
