"""A borrowed deck counts toward the deck AND is labelled (#156, Option C).

`game_seats.user_id` (pilot) and `.deck_id` (deck played) are independent, so a
lent-out deck is representable. The open question was whether that result counts
toward the deck's record. Owner decision 2026-07-27: **it counts, and the surface
says how many were piloted by someone else** — discarding them hides real games in
a corpus averaging <2 games per deck, and counting them silently conflates deck
strength with pilot skill.

The load-bearing part is that the TWO deck-stat surfaces now answer this the same
way. They previously did not: `compute_deck_game_stats` filtered on viewer
visibility while `dashboard_service` counted every seat, and the former's docstring
claimed they matched. They agreed only because no borrowed seat existed (prod
2026-07-27: 0 rows). These tests construct the seat that separates them.
"""

from __future__ import annotations

from app.dashboard_service import get_dashboard_data
from app.deck_service import compute_deck_game_stats
from app.models import Deck, Game, GameSeat, StorageLocation, User


def _deck(db, owner, name="Lent Deck"):
    loc = StorageLocation(user_id=owner.id, name=name, type="deck", mode="manual")
    db.add(loc)
    db.commit()
    deck = Deck(user_id=owner.id, name=name, storage_location_id=loc.id)
    db.add(deck)
    db.commit()
    return deck


def _finalized_game(db, creator, seats):
    """seats = [(pilot_user_or_None, deck_or_None, placement), ...]"""
    game = Game(user_id=creator.id, status="finalized")
    db.add(game)
    db.commit()
    for i, (pilot, deck, placement) in enumerate(seats, start=1):
        db.add(
            GameSeat(
                game_id=game.id,
                seat_number=i,
                player_name=f"P{i}",
                user_id=pilot.id if pilot else None,
                deck_id=deck.id if deck else None,
                placement=placement,
            )
        )
    db.commit()
    return game


def _other(db, name="borrower@example.com"):
    u = User(username=name, password_hash="x")
    db.add(u)
    db.commit()
    return u


def test_owner_piloting_their_own_deck_is_not_borrowed(db, user):
    deck = _deck(db, user)
    _finalized_game(db, user, [(user, deck, 1)])

    stats = compute_deck_game_stats(db, user_id=user.id, deck_ids=[deck.id])[deck.id]

    assert stats["games"] == 1
    assert stats["wins"] == 1
    assert stats["borrowed_games"] == 0


def test_a_borrowed_deck_counts_and_is_labelled(db, user):
    deck = _deck(db, user)
    borrower = _other(db)
    _finalized_game(db, user, [(borrower, deck, 1)])

    stats = compute_deck_game_stats(db, user_id=user.id, deck_ids=[deck.id])[deck.id]

    assert stats["games"] == 1, "Option C counts the game"
    assert stats["wins"] == 1
    assert stats["borrowed_games"] == 1, "and says it was piloted by someone else"


def test_a_guest_seat_counts_as_borrowed(db, user):
    """NULL user_id is the null-safety case: `!=` would evaluate to NULL and drop it."""
    deck = _deck(db, user)
    _finalized_game(db, user, [(None, deck, 2)])

    stats = compute_deck_game_stats(db, user_id=user.id, deck_ids=[deck.id])[deck.id]

    assert stats["games"] == 1
    assert stats["borrowed_games"] == 1


def test_a_game_the_owner_neither_created_nor_sat_at_still_counts(db, user):
    """THE seat that used to split the two surfaces.

    The old viewer-visibility filter dropped this from the Decks page while the
    Dashboard counted it. Under Option C both count it.
    """
    deck = _deck(db, user)
    borrower = _other(db)
    _finalized_game(db, borrower, [(borrower, deck, 1)])  # owner absent entirely

    stats = compute_deck_game_stats(db, user_id=user.id, deck_ids=[deck.id])[deck.id]

    assert stats["games"] == 1
    assert stats["borrowed_games"] == 1


def test_the_two_surfaces_agree_on_the_seat_that_used_to_split_them(db, user):
    """The reconciliation itself — the docstring's claimed parity, made true."""
    deck = _deck(db, user)
    borrower = _other(db)
    _finalized_game(db, user, [(user, deck, 1)])  # owner-piloted
    _finalized_game(db, borrower, [(borrower, deck, 2)])  # borrowed, owner absent

    decks_page = compute_deck_game_stats(db, user_id=user.id, deck_ids=[deck.id])[deck.id]
    dash = next(
        d for d in get_dashboard_data(db, user.id)["deck_performance"] if d["deck_id"] == deck.id
    )

    assert decks_page["games"] == dash["games"] == 2
    assert decks_page["wins"] == dash["wins"] == 1
    assert decks_page["borrowed_games"] == dash["borrowed_games"] == 1


def test_unfinalized_and_placementless_seats_still_do_not_count(db, user):
    """Option C changed WHOSE seats count, not WHICH games are results."""
    deck = _deck(db, user)
    borrower = _other(db)

    in_progress = Game(user_id=user.id, status="live")
    db.add(in_progress)
    db.commit()
    db.add(
        GameSeat(
            game_id=in_progress.id,
            seat_number=1,
            player_name="P1",
            user_id=borrower.id,
            deck_id=deck.id,
            placement=None,
        )
    )
    db.commit()

    assert compute_deck_game_stats(db, user_id=user.id, deck_ids=[deck.id]) == {}


def test_the_lent_out_note_reaches_the_decks_page_when_featured(client, db, user):
    """Route-level: the loop copies stats key by key, so a new key needs a line.

    This is the #152 failure mode — every service assertion above can pass while
    the page shows nothing. A single deck renders in the FEATURED block.
    """
    deck = _deck(db, user, name="Borrowed Brew")
    borrower = _other(db)
    _finalized_game(db, borrower, [(borrower, deck, 1)])

    body = client.get("/decks").text

    assert "1 lent out" in body


def test_the_lent_out_note_reaches_the_compact_rows_too(client, db, user):
    """The Decks page has TWO stat render paths — featured and compact rows.

    The first cut of this note only covered the compact rows, so the single-deck
    case (which renders featured) showed nothing while every service test passed.
    Both paths need the note, and both need a test.
    """
    borrower = _other(db)
    featured = _deck(db, user, name="Played A Lot")
    for _ in range(3):
        _finalized_game(db, user, [(user, featured, 1)])

    lent = _deck(db, user, name="Borrowed Brew")
    _finalized_game(db, borrower, [(borrower, lent, 1)])

    body = client.get("/decks").text

    # "Played A Lot" takes the featured slot; "Borrowed Brew" is a compact row.
    assert "Played A Lot" in body and "Borrowed Brew" in body
    assert "1 lent out" in body
