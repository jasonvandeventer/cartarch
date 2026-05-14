"""One-shot data fixup: promote pre-v3.23.2 `auto/medium` intrinsic tags
to `auto/certain` so they match v3.23.2's per-pattern confidence semantics.

v3.22.0's migration backfilled every auto tag at `confidence=medium` because
there was no per-pattern semantics yet. v3.23.2 introduced
`_AUTO_TAG_CONFIDENCE`: intrinsic role tags (Ramp/Draw/Tutor/Removal/Wipe/
Protection/Engine/Threat/Hate) emit at `certain` because they fire on
unambiguous oracle-text rules; Synergy stays `medium` because
`card_matches_theme` is a heuristic.

Without this fixup, the v3.23.3 review-tags panel would surface every
pre-v3.23.2 auto tag as "needs review" — easily 50+ rows per Commander
deck of correct intrinsic tags. This migration walks every row and
promotes only the intrinsic tags. Synergy and any user-source tags
are left untouched.

Idempotent: a row whose tags are already in the post-v3.23.2 shape
(every intrinsic tag at certain, Synergy at medium) has no entries
matching the promote condition, so the function is a no-op on re-run.
"""

from __future__ import annotations

import json

from app.db import engine

# Mirrors _AUTO_TAG_CONFIDENCE in app/deck_service.py — kept inline here so
# the migration script can run before deck_service imports succeed.
_INTRINSIC_TAGS_TO_PROMOTE = frozenset(
    {
        "Ramp",
        "Draw",
        "Tutor",
        "Removal",
        "Wipe",
        "Protection",
        "Engine",
        "Threat",
        "Hate",
    }
)


def main() -> None:
    from sqlalchemy.orm import Session

    from app.models import InventoryRow

    promoted_rows = 0
    promoted_tags = 0

    with Session(engine) as session:
        rows = session.query(InventoryRow).filter(InventoryRow.tags.isnot(None)).all()

        for row in rows:
            raw = row.tags
            if not raw or not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(data, list):
                continue

            changed = False
            new_list: list[dict] = []
            for entry in data:
                # Only the structured shape is touched. Legacy list[str] shape
                # would have been handled by the v3.22.0 migration; if some
                # row is still in that shape, skip and let it through.
                if not isinstance(entry, dict):
                    new_list.append(entry)
                    continue
                tag = entry.get("tag")
                source = entry.get("source")
                confidence = entry.get("confidence")
                if (
                    tag in _INTRINSIC_TAGS_TO_PROMOTE
                    and source == "auto"
                    and confidence == "medium"
                ):
                    new_entry = dict(entry)
                    new_entry["confidence"] = "certain"
                    new_list.append(new_entry)
                    changed = True
                    promoted_tags += 1
                else:
                    new_list.append(entry)

            if changed:
                row.tags = json.dumps(new_list)
                promoted_rows += 1

        session.commit()

    print(
        f"v3.23.3 intrinsic-tag promotion complete: "
        f"{promoted_rows} rows touched, {promoted_tags} tags promoted "
        f"from auto/medium to auto/certain."
    )


if __name__ == "__main__":
    main()
