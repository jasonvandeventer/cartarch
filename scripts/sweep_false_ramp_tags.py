"""Sweep persisted false `Ramp` tags left by the v1 tagger's self-cost-reduction bug.

BACKGROUND
    `_RAMP_NON_LAND_RE` used to carry a bare ``costs? \\{\\d+\\} less to cast``, which
    matched a card discounting ITSELF ("This spell costs {7} less to cast if…").
    That is a discount on one spell, not mana acceleration. Because
    `suggest_card_roles` appends Ramp FIRST and `classify_role_subtype` reads
    roles[0], a false Ramp also MASKED the card's real role — the reported symptom
    was a counterspell (Not of This World) analyzed as "Ramp / expensive_ramp".
    The regex is fixed; this sweeps the tags already written to disk.

SCOPE — deliberately narrow, and why
    A row's Ramp tag is swept ONLY when BOTH hold:
      1. the FIXED tagger no longer derives Ramp for that card, AND
      2. the card's oracle text contains the self-reduction phrase — i.e. the tag
         is *attributable to this specific bug*.

    Condition 2 is the important one. A naive "re-derive from the fixed tagger and
    drop what it no longer produces" sweep OVER-REACHES: measured on live prod,
    5 rows carry a hand-applied Ramp the auto-tagger could never derive for reasons
    unrelated to this bug — Bootleggers' Stash and The Reaver Cleaver put their mana
    production inside quoted granted abilities (stripped by `_QUOTED_ABILITY_RE`),
    Collective Voyage says "searches THEIR library" (the land-tutor regex wants
    "search your library"), plus Castle Garenbrig and World War Hulk. A pure
    re-derive would silently delete all five — correct tags, wrong bug. Attribution
    keeps this sweep to what it actually broke, and makes ``--include-user`` safe:
    even then, only genuinely-false Ramp is touched.

    Rows are otherwise preserved byte-for-byte: only the Ramp entry is removed, via
    `set_row_tags` with the surviving entries' own source/confidence carried through.
    No other tag is added, removed, or re-derived.

SAFETY
    - **Dry-run is the DEFAULT.** Pass ``--apply`` to write. (Note this inverts
      `sweep_fk_orphans.py`, which writes unless given ``--dry-run``; a tag sweep
      touching user data should not default to mutating.)
    - **source:user rows are REPORTED AND SKIPPED** unless ``--include-user``.
      Not of This World's row is one of these — a human tagged it, and a human
      decision is not overwritten by a regex fix without an explicit opt-in.

USAGE
    python -m scripts.sweep_false_ramp_tags                    # dry run (default)
    python -m scripts.sweep_false_ramp_tags --apply            # write auto rows
    python -m scripts.sweep_false_ramp_tags --apply --include-user   # also user rows

    In-cluster, DATABASE_URL lives only in PID 1's env (see CLAUDE.md → Telemetry):
      kubectl exec -n cnpg-system deploy/cartarch -- sh -c \\
        'export PYTHONPATH=/app; export $(tr "\\0" "\\n" < /proc/1/environ | grep ^DATABASE_URL=); \\
         python -m scripts.sweep_false_ramp_tags'
"""

from __future__ import annotations

import argparse

from sqlalchemy.orm import Session

import app.legacy_tables  # noqa: F401 — registers raw tables on Base.metadata
from app.db import SessionLocal
from app.deck_service import (
    _SELF_COST_REDUCTION_RE,
    get_row_tag_details,
    set_row_tags,
    suggest_card_roles,
)
from app.models import Card, InventoryRow


def _attributable_to_self_reduction_bug(card) -> bool:
    """True if this card's Ramp tag is explained by the fixed bug.

    The old pattern matched any ``costs {N} less to cast``; the new one requires the
    reduced subject to be a *spell other than this card*. The delta is therefore
    exactly the cards whose only cost-reduction text is SELF-reduction — which is
    what this phrase detects. Cards without it were never tagged Ramp by this bug,
    so their Ramp came from somewhere else and is none of this sweep's business.
    """
    return bool(_SELF_COST_REDUCTION_RE.search(card.oracle_text or ""))


def plan(session: Session) -> dict[str, list[dict]]:
    """Classify every Ramp-tagged inventory row. Pure read; mutates nothing."""
    rows = (
        session.query(InventoryRow, Card)
        .join(Card, Card.id == InventoryRow.card_id)
        .filter(InventoryRow.tags.isnot(None))
        .order_by(Card.name, InventoryRow.id)
        .all()
    )
    out: dict[str, list[dict]] = {"sweep": [], "skipped_user": [], "not_attributable": []}
    for row, card in rows:
        details = get_row_tag_details(row)
        ramp = next((d for d in details if d["tag"] == "Ramp"), None)
        if ramp is None:
            continue
        if "Ramp" in suggest_card_roles(card):
            continue  # fixed tagger still says Ramp — correct tag, leave it
        rec = {
            "row_id": row.id,
            "card": card.name,
            "source": ramp.get("source"),
            "confidence": ramp.get("confidence"),
            "keeps": [d["tag"] for d in details if d["tag"] != "Ramp"],
        }
        if not _attributable_to_self_reduction_bug(card):
            out["not_attributable"].append(rec)
        elif ramp.get("source") == "user":
            out["skipped_user"].append(rec)
        else:
            out["sweep"].append(rec)
    return out


def _strip_ramp(session: Session, row_id: int) -> None:
    row = session.get(InventoryRow, row_id)
    keep = [d for d in get_row_tag_details(row) if d["tag"] != "Ramp"]
    set_row_tags(row, keep)  # dict entries carry their own source/confidence


def main(argv: list[str] | None = None) -> dict[str, list[dict]]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument(
        "--include-user",
        action="store_true",
        help="also sweep source:user rows (default: report + skip — a human tagged those)",
    )
    args = ap.parse_args(argv)

    session = SessionLocal()
    try:
        p = plan(session)
        targets = p["sweep"] + (p["skipped_user"] if args.include_user else [])

        print(f"{'DRY RUN — no changes written' if not args.apply else 'APPLYING'}\n")

        print(f"False Ramp attributable to the self-cost-reduction bug ({len(p['sweep'])} rows):")
        for r in p["sweep"]:
            print(
                f"  row {r['row_id']:<6} {r['card'][:34]:34} "
                f"{r['source']}/{r['confidence']:8} Ramp -> drop; keeps {r['keeps'] or '[]'}"
            )

        print(
            f"\nsource:user — {'INCLUDED via flag' if args.include_user else 'REPORTED, SKIPPED'} "
            f"({len(p['skipped_user'])} rows):"
        )
        for r in p["skipped_user"]:
            print(f"  row {r['row_id']:<6} {r['card'][:34]:34} {r['source']}/{r['confidence']}")
        if p["skipped_user"] and not args.include_user:
            print("  (a human applied these; re-run with --include-user to sweep them too)")

        print(
            f"\nRamp the fixed tagger also does not derive, but NOT attributable to this bug "
            f"— left alone ({len(p['not_attributable'])} rows):"
        )
        for r in p["not_attributable"]:
            print(f"  row {r['row_id']:<6} {r['card'][:34]:34} {r['source']}/{r['confidence']}")
        if p["not_attributable"]:
            print(
                "  (a blanket re-derive would have wrongly stripped these — see module docstring)"
            )

        if args.apply and targets:
            for r in targets:
                _strip_ramp(session, r["row_id"])
            session.commit()
            print(f"\nAPPLIED: dropped Ramp from {len(targets)} row(s).")
        elif args.apply:
            print("\nNothing to apply.")
        else:
            print(f"\nWould change {len(targets)} row(s). Re-run with --apply to write.")
        return p
    finally:
        session.close()


if __name__ == "__main__":
    main()
