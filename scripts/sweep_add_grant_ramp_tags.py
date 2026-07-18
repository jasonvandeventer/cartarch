"""Add the Ramp tag to owned rows the #139 fix now recognizes as ramp.

BACKGROUND
    The v1 tagger stripped ALL quoted granted abilities before the ramp check, so a
    mana ability GRANTED to permanents you control (Cryptolith Rite:
    `Creatures you control have "{T}: Add …"`; Bootleggers' Stash: `Lands you
    control have "… Create a Treasure token."`) read as non-ramp — a systematic
    false NEGATIVE. It also missed group land-ramp phrased as "searches their
    library" (Collective Voyage). #139 fixed the tagger; deck health / role
    suggestions recompute LIVE so they are already correct, but tag CHIPS persisted
    on already-imported rows stay stale until re-derived. This sweeps them.

    Users were hand-adding `source:user` Ramp to some of these (5 rows) — this
    aligns the auto tags so they stop having to.

SCOPE — narrow, mirrors sweep_false_ramp_tags.py's attribution discipline
    A row gets Ramp ADDED only when ALL hold:
      1. the FIXED tagger derives Ramp for the card (`"Ramp" in suggest_card_roles`), AND
      2. the row's persisted tags DON'T already include Ramp, AND
      3. the new detection is WHY it now qualifies — a "you control" mana grant, or
         a "searches their library for … land" the old "search your library" rule
         missed. (Condition 3 keeps the sweep to exactly what #139 changed; a card
         missing Ramp for any other reason is a different issue, untouched.)
    Existing tags are preserved; the added entry is `source:auto, confidence:medium`.
    Rows that already carry Ramp (incl. the 5 hand-applied `source:user` ones) are
    a no-op — nothing is overwritten.

SAFETY
    - **Dry-run is the DEFAULT.** Pass `--apply` to write.

USAGE
    python -m scripts.sweep_add_grant_ramp_tags                 # dry run (default)
    python -m scripts.sweep_add_grant_ramp_tags --apply         # write

    In-cluster, DATABASE_URL lives only in PID 1's env (see CLAUDE.md -> Telemetry):
      kubectl exec -n cnpg-system deploy/cartarch -- sh -c \\
        'export PYTHONPATH=/app; export $(tr "\\0" "\\n" < /proc/1/environ | grep ^DATABASE_URL=); \\
         python -m scripts.sweep_add_grant_ramp_tags'
"""

from __future__ import annotations

import argparse
import re

from sqlalchemy.orm import Session

import app.legacy_tables  # noqa: F401 — registers raw tables on Base.metadata
from app.db import SessionLocal
from app.deck_service import (
    _RAMP_LAND_RE,
    _grants_mana_to_your_board,
    add_auto_tags,
    get_row_tag_details,
    suggest_card_roles,
)
from app.models import Card, InventoryRow

# The pre-#139 land-tutor rule ("search your library …"). A card attributable to
# the #139 land broadening matches the new _RAMP_LAND_RE but NOT this.
_OLD_RAMP_LAND_RE = re.compile(
    r"search your library for .{0,60}\b(?:land|forest|island|plains|mountain|swamp)\b",
    re.IGNORECASE,
)


def _attributable_to_139(card: Card) -> bool:
    """True if the card newly-qualifies as ramp BECAUSE of the #139 change."""
    oracle = card.oracle_text or ""
    if _grants_mana_to_your_board(oracle):
        return True
    return bool(_RAMP_LAND_RE.search(oracle)) and not bool(_OLD_RAMP_LAND_RE.search(oracle))


def plan(session: Session) -> list[dict]:
    """Owned rows that should gain a Ramp tag. Pure read; mutates nothing."""
    rows = (
        session.query(InventoryRow, Card)
        .join(Card, Card.id == InventoryRow.card_id)
        .order_by(Card.name, InventoryRow.id)
        .all()
    )
    out: list[dict] = []
    for row, card in rows:
        details = get_row_tag_details(row)
        if any(d["tag"] == "Ramp" for d in details):
            continue  # already tagged (incl. hand-applied source:user) — no-op
        if "Ramp" not in suggest_card_roles(card):
            continue  # fixed tagger still doesn't call it ramp
        if not _attributable_to_139(card):
            continue  # derives ramp for some other reason — out of scope
        out.append({"row_id": row.id, "card": card.name, "keeps": [d["tag"] for d in details]})
    return out


def _add_ramp(session: Session, row_id: int) -> None:
    # add_auto_tags unions the suggestion into the row's existing tags, preserving
    # every existing entry's source/confidence (the canonical retag primitive).
    add_auto_tags(
        session.get(InventoryRow, row_id),
        [{"tag": "Ramp", "confidence": "medium", "source": "auto"}],
    )


def main(argv: list[str] | None = None) -> list[dict]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args(argv)

    session = SessionLocal()
    try:
        targets = plan(session)
        print(f"{'APPLYING' if args.apply else 'DRY RUN — no changes written'}\n")
        print(f"Rows gaining a Ramp tag (attributable to #139): {len(targets)}")
        for t in targets:
            print(f"  row {t['row_id']:<7} {t['card'][:38]:38} keeps={t['keeps']}")
        if args.apply:
            for t in targets:
                _add_ramp(session, t["row_id"])
            session.commit()
            print(f"\nAPPLIED: added Ramp to {len(targets)} row(s).")
        else:
            print("\n(dry run — pass --apply to write.)")
        return targets
    finally:
        session.close()


if __name__ == "__main__":
    main()
