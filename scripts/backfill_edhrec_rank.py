"""#180 — copy ``edhrec_rank`` from the bulk cache onto owned ``cards`` rows.

Idempotent. Run with ``--apply`` to write; the default is a dry run that prints
exactly what it would do and writes nothing.

    python -m scripts.backfill_edhrec_rank            # dry run
    python -m scripts.backfill_edhrec_rank --apply

**This exists because the passive path is slow, not because it is broken.**
``main._run_price_refresh_batch`` already carries the rank from the payload onto
the Card row, but it only selects cards past the 7-day staleness cutoff — so a
card refreshed yesterday is not revisited for a week. Measured on prod
2026-08-07: **6,230 of 15,237** cards were stale at that moment, and the full
table cycles over ~7 days. Until a card cycles, its rank is NULL, the
recommender's popularity term contributes 0, and the brew scores exactly as it
did before #180.

**NO NETWORK.** Every value is already sitting in ``scryfall_cards``, which
``_bulk_data_loop`` repopulated in full on 2026-08-07 (116,731 rows, 101,831
carrying a rank). This is the same "the bulk cache is the fallback for anything
the ``cards`` table cannot answer" pattern the wishlist price column uses. Join
is on ``scryfall_id``, the PK of both sides — a printing, not a name, so no
oracle-level ambiguity arises.

**Coverage is 95.3%, and the shortfall is expected rather than a defect.**
14,522 of 15,237 owned printings resolve to a cache row carrying a rank. The rest
are cards Scryfall gives no EDHREC rank at all (basic lands, tokens,
non-EDH-legal printings — 12.7% of the whole export) plus a handful of printings
absent from ``default_cards``. Those stay NULL, which the scorer reads as
"unknown", never as "unpopular" — see ``EDHREC_RANK_TIERS``.

**Only writes a row whose value would actually CHANGE**, so a re-run reports 0
updated and the ``updated_at`` column is deliberately left alone: this is a
metadata top-up, not a refresh, and advancing ``updated_at`` would push the card
OUT of the price-refresh loop's staleness window and suppress a real refresh it
was otherwise due.
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db import SessionLocal
from app.models import Card

# Cache row → owned row, by printing. Only rows whose value would change are
# selected, which is what makes a re-run a no-op and the report honest.
_CANDIDATES_SQL = text("""
    SELECT c.id, c.name, c.set_code, sc.edhrec_rank
    FROM cards c
    JOIN scryfall_cards sc ON sc.scryfall_id = c.scryfall_id
    WHERE sc.edhrec_rank IS NOT NULL
      AND (c.edhrec_rank IS NULL OR c.edhrec_rank <> sc.edhrec_rank)
    ORDER BY sc.edhrec_rank
""")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    parser.add_argument("--show", type=int, default=15, help="how many rows to print (default 15)")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        # Snapshot BEFORE anything runs, including a dry run — the #164 lesson:
        # the write there was caught only because the prior count was known.
        total_cards = session.query(Card).count()
        before_ranked = session.query(Card).filter(Card.edhrec_rank.isnot(None)).count()
        print(f"before: {before_ranked}/{total_cards} cards carry an edhrec_rank")

        rows = session.execute(_CANDIDATES_SQL).all()
        if not rows:
            print("nothing to do — every resolvable card already matches the cache")
            return

        print(f"{len(rows)} card(s) would be updated; most-played first:")
        for card_id, name, set_code, rank in rows[: args.show]:
            print(f"  #{rank:>6,}  {name} ({(set_code or '?').upper()})  [card {card_id}]")
        if len(rows) > args.show:
            print(f"  … and {len(rows) - args.show:,} more")

        updated = 0
        for card_id, _name, _set_code, rank in rows:
            card = session.get(Card, card_id)
            if card is None:  # deleted between the SELECT and here
                continue
            card.edhrec_rank = rank
            updated += 1

        # FLUSH even on a dry run, so the UPDATEs really hit the database and a
        # dry run is a genuine rehearsal — a constraint or type error surfaces
        # here rather than on the day you --apply.
        session.flush()

        # A card can only be counted as unresolved once it is known which ones
        # the join could not reach — reported, not silently dropped.
        unresolved = session.execute(
            text("""
                SELECT COUNT(*) FROM cards c
                WHERE c.edhrec_rank IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM scryfall_cards sc
                      WHERE sc.scryfall_id = c.scryfall_id
                        AND sc.edhrec_rank IS NOT NULL
                  )
            """)
        ).scalar_one()

        print(
            f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {updated:,} card(s) updated; "
            f"{unresolved:,} left NULL (no rank in the bulk cache — basics, tokens, "
            f"non-EDH-legal printings; the scorer reads NULL as unknown, not unpopular)"
        )
        if args.apply:
            session.commit()
            after = session.query(Card).filter(Card.edhrec_rank.isnot(None)).count()
            print(f"committed — {after:,}/{total_cards:,} cards now carry a rank")
        else:
            # Roll back so a dry run is genuinely a dry run, rather than trusting
            # that nothing downstream commits (the #164 incident: a "dry run"
            # wrote five decks to production).
            #
            # HONEST NOTE: no test here can kill this line. The flush above is
            # uncommitted, so an observer session cannot see it, and closing the
            # session would roll it back anyway. It is defence-in-depth against a
            # FUTURE edit that adds a commit below this point — which is exactly
            # how #164 happened — not a currently-observable behaviour. Do not
            # remove it on the grounds that the suite stays green without it.
            session.rollback()
            print("rolled back — nothing written")
    finally:
        session.close()


if __name__ == "__main__":
    main()
