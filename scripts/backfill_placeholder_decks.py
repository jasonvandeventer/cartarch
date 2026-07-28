"""#164 — backfill placeholder decks for seats that named a deck but never linked one.

Idempotent, owner-scoped, read-mostly. Run with ``--apply`` to write; the default is a
dry run that prints exactly what it would do.

**7 of 8 seats, not 8.** The eighth ("Elemental combos") has BOTH a null ``user_id``
and a null ``commander_name_at_game`` — no owner to create a ``decks`` row under
(``decks.user_id`` is NOT NULL) and no commander to resolve. It is reported as
unrecoverable rather than silently skipped, which is the difference between a gap you
know about and one you don't.

**Resolves on ``commander_name_at_game``, never ``deck_name_at_game``.** The deck name
may be a FLAVOR name the catalog has no record of — prod's "Buttercup, Provincial
Princess" is really Sisay, Weatherlight Captain — so resolving on it would fail or, if
find-or-create were sloppier, mint a deck under the wrong commander.

    python -m scripts.backfill_placeholder_decks            # dry run
    python -m scripts.backfill_placeholder_decks --apply
"""

from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.deck_service import _split_commander_names, resolve_commander_to_deck
from app.models import Card, Game, GameSeat, InventoryRow


def _deck_owner_for(session, seat) -> tuple[int, str]:
    """Who should own the placeholder — the seat's pilot, or the card's owner?

    Normally the pilot. But this is a BORROWED-deck backfill: game 27 is the one
    seat in the database where deck and pilot varied independently (#156). MasonRex
    piloted a Quandrix precon whose commander, Zimone, sits in **MURPGM's**
    inventory, so the deck is MURPGM's and crediting it to MasonRex would record the
    borrow as ownership.

    The rule fires only when it is UNAMBIGUOUS: exactly one user owns the commander
    card, and it is not the seat's user. Two owners, or none, and the pilot keeps it
    — an inference this thin should never be made on a tie. Every application is
    printed, because it is the one place this script guesses.
    """
    names = _split_commander_names(seat.commander_name_at_game)
    owners: set[int] = set()
    for name in names:
        rows = (
            session.query(InventoryRow.user_id)
            .join(Card, Card.id == InventoryRow.card_id)
            .filter(
                Card.name.ilike(name),
                InventoryRow.is_proxy.is_(False),
            )
            .distinct()
            .all()
        )
        owners |= {uid for (uid,) in rows if uid is not None}

    if len(owners) == 1:
        (only,) = owners
        if only != seat.user_id:
            return only, f" [BORROWED: commander owned solely by user {only}]"
    return seat.user_id, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry-run")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        seats = (
            session.query(GameSeat)
            .join(Game, Game.id == GameSeat.game_id)
            .filter(
                GameSeat.deck_id.is_(None),
                GameSeat.deck_name_at_game.isnot(None),
                GameSeat.deck_name_at_game != "",
            )
            .order_by(GameSeat.id)
            .all()
        )

        linked = skipped = 0
        for seat in seats:
            label = (
                f"seat {seat.id} (game {seat.game_id}) "
                f"deck_name={seat.deck_name_at_game!r} "
                f"commander={seat.commander_name_at_game!r}"
            )
            if seat.user_id is None:
                print(f"  UNRECOVERABLE  {label} — no user to own the deck")
                skipped += 1
                continue
            if not (seat.commander_name_at_game or "").strip():
                print(f"  UNRECOVERABLE  {label} — no commander recorded")
                skipped += 1
                continue

            owner_id, borrowed_note = _deck_owner_for(session, seat)
            deck, missing = resolve_commander_to_deck(
                session, owner_id, seat.commander_name_at_game, commit=False
            )
            if deck is None:
                print(f"  UNRESOLVED     {label} — catalog has no {missing}")
                skipped += 1
                continue

            print(f"  LINK           {label} -> deck {deck.id} {deck.name!r}{borrowed_note}")
            if args.apply:
                seat.deck_id = deck.id
            linked += 1

        if args.apply:
            session.commit()
            print(f"\napplied: {linked} seat(s) linked, {skipped} unrecoverable")
        else:
            session.rollback()
            print(f"\nDRY RUN: would link {linked} seat(s); {skipped} unrecoverable")
            print("re-run with --apply to write")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
