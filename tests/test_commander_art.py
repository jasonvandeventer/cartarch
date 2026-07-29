"""Commander art on the tablet and on the player's phone.

Reported 2026-07-29: the art background is intermittent, and had been long before
commanders could be typed in. It is not intermittent — it is deterministic on a
distinction no player can see.

There were THREE implementations of "who is this deck's commander". #165 taught
one of them (`_capture_deck_identity`) to fall back to #163's `deck_commanders`
anchor when no inventory row is tagged `role='commander'`; the two ART resolvers
never learned it. So a deck whose commander was never tagged recorded its
commander NAME correctly and rendered NO art. Measured on prod that day: 11 of
the 68 seats holding a deck had no art, 10 of them with an anchor sitting right
there.

All three now share `deck_commander_cards`, so a name and its art cannot disagree.
"""

from __future__ import annotations

import itertools

from app import game_service
from app.models import Card, Deck, DeckCommander, Game, GameSeat, InventoryRow, StorageLocation

_seq = itertools.count(1)


def _card(db, name):
    n = next(_seq)
    c = Card(
        scryfall_id=f"art-sid-{n}",
        name=name,
        set_code="tst",
        collector_number=str(n),
        type_line="Legendary Creature — Human",
        image_url=f"https://cards.scryfall.io/normal/{n}.jpg",
    )
    db.add(c)
    db.commit()
    return c


def _deck(db, user, name, *, anchor=None, tagged=None):
    """A deck whose commander is recorded by anchor, by tagged row, or both."""
    loc = StorageLocation(user_id=user.id, name=name, type="deck", mode="manual")
    db.add(loc)
    db.commit()
    d = Deck(user_id=user.id, name=name, storage_location_id=loc.id)
    db.add(d)
    db.commit()
    for card in anchor or []:
        db.add(DeckCommander(deck_id=d.id, card_id=card.id))
    for card in tagged or []:
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=card.id,
                finish="normal",
                quantity=1,
                is_pending=False,
                storage_location_id=loc.id,
                role="commander",
            )
        )
    db.commit()
    return d


def _game_with(db, user, deck):
    g = Game(user_id=user.id, format="Commander", status="in_progress")
    db.add(g)
    db.commit()
    db.add(
        GameSeat(
            game_id=g.id,
            seat_number=1,
            player_name="P1",
            starting_life=40,
            user_id=user.id,
            deck_id=deck.id,
        )
    )
    db.commit()
    return g


# ── The reported bug ────────────────────────────────────────────────────────


def test_an_anchor_only_deck_gets_art_on_the_tablet(db, user):
    """The 10 prod seats. No tagged row, but the commander is recorded."""
    card = _card(db, "Auntie Ool, Cursewretch")
    game = _game_with(db, user, _deck(db, user, "Auntie Ool", anchor=[card]))

    urls = game_service.get_seat_commander_image_urls(db, game)

    assert urls[game.seats[0].id] == [card.image_url]


def test_an_anchor_only_deck_gets_art_on_the_phone(db, user):
    """The phone reads a scryfall_id and builds a mirror URL from it. Same gap,
    a different function — which is why both now share one resolver."""
    card = _card(db, "Krang, Utrom Warlord")
    game = _game_with(db, user, _deck(db, user, "Krang", anchor=[card]))

    ids = game_service.get_seat_commander_scryfall_ids(db, game)

    assert ids[game.seats[0].id] == card.scryfall_id


def test_a_typed_commander_placeholder_gets_art(db, user):
    """The path a typed commander takes: #164 creates a deck with an anchor and
    no cards at all. Nothing about it can produce a tagged row, ever."""
    from app.deck_service import resolve_commander_to_deck

    _card(db, "Wolverine, Best There Is")
    deck, unresolved = resolve_commander_to_deck(db, user.id, "Wolverine, Best There Is")
    assert unresolved == []
    game = _game_with(db, user, deck)

    assert game_service.get_seat_commander_image_urls(db, game)[game.seats[0].id]
    assert game_service.get_seat_commander_scryfall_ids(db, game)[game.seats[0].id]


def test_the_name_and_the_art_agree(db, user):
    """The actual defect class: the snapshot knew the commander and the art did
    not, for the same seat, at the same moment."""
    card = _card(db, "Sisay, Weatherlight Captain")
    deck = _deck(db, user, "Sisay", anchor=[card])
    game = _game_with(db, user, deck)

    _, commander_name = game_service._capture_deck_identity(db, deck.id)

    assert commander_name == "Sisay, Weatherlight Captain"
    assert game_service.get_seat_commander_image_urls(db, game)[game.seats[0].id]


# ── Controls: the pre-existing behaviour must not move ──────────────────────


def test_a_tagged_row_still_wins_over_the_anchor(db, user):
    """The 57 seats that already worked. The tagged row is the deck's own
    ordering and stays authoritative."""
    tagged = _card(db, "Tagged Commander")
    anchored = _card(db, "Anchored Commander")
    game = _game_with(db, user, _deck(db, user, "Both", anchor=[anchored], tagged=[tagged]))

    assert game_service.get_seat_commander_image_urls(db, game)[game.seats[0].id] == [
        tagged.image_url
    ]


def test_two_commanders_are_capped_at_two(db, user):
    """Partner / Background / Friends Forever ceiling the art rendering enforces."""
    a, b, c = _card(db, "Partner A"), _card(db, "Partner B"), _card(db, "Partner C")
    game = _game_with(db, user, _deck(db, user, "Partners", anchor=[a, b, c]))

    assert len(game_service.get_seat_commander_image_urls(db, game)[game.seats[0].id]) == 2


def test_a_seat_with_no_deck_has_no_art(db, user):
    g = Game(user_id=user.id, format="Commander", status="in_progress")
    db.add(g)
    db.commit()
    db.add(GameSeat(game_id=g.id, seat_number=1, player_name="P1", starting_life=40))
    db.commit()

    assert game_service.get_seat_commander_image_urls(db, g)[g.seats[0].id] == []
    assert game_service.get_seat_commander_scryfall_ids(db, g)[g.seats[0].id] is None


def test_a_commander_with_no_cached_image_degrades_quietly(db, user):
    card = _card(db, "No Art Yet")
    card.image_url = None
    db.commit()
    game = _game_with(db, user, _deck(db, user, "Artless", anchor=[card]))

    assert game_service.get_seat_commander_image_urls(db, game)[game.seats[0].id] == []
    # …but the phone still gets its id: the mirror can serve art the cache lacks.
    assert game_service.get_seat_commander_scryfall_ids(db, game)[game.seats[0].id]


def test_a_borrowed_deck_resolves_against_its_OWNER(db, user):
    """#156 — a seat can hold someone else's deck; filtering by the game owner
    would silently blank the art for every borrowed seat."""
    from app.models import User as UserModel

    owner = UserModel(username="lender@ex.com", password_hash="x", display_name="Lender")
    db.add(owner)
    db.commit()
    card = _card(db, "Borrowed Commander")
    deck = _deck(db, owner, "Lent Deck", tagged=[card])
    game = _game_with(db, user, deck)

    assert game_service.get_seat_commander_image_urls(db, game)[game.seats[0].id] == [
        card.image_url
    ]


# ── The tablet has to NOTICE ────────────────────────────────────────────────


def test_the_lobby_signature_covers_deck_changes_not_just_claims(client, db, user):
    """A seated player picking their deck from their phone changes no seat count,
    so a count-only poll left the tablet artless until a manual reload — the same
    symptom from a different cause. The signature is seeded server-side so a
    change between render and the first poll is caught, not swallowed."""
    card = _card(db, "Signature Commander")
    deck = _deck(db, user, "Sig Deck", anchor=[card])
    g = Game(user_id=user.id, format="Commander", status="created")
    db.add(g)
    db.commit()
    seat = GameSeat(
        game_id=g.id, seat_number=1, player_name="P1", starting_life=40, user_id=user.id
    )
    db.add(seat)
    db.commit()

    before = client.get(f"/games/{g.id}").text
    assert f'data-sig="{seat.id}:1:|"' in before, "an undecided seat carries an empty slot"

    seat.deck_id = deck.id
    seat.commander_name_at_game = "Signature Commander"
    db.commit()

    after = client.get(f"/games/{g.id}").text
    assert f'data-sig="{seat.id}:1:Signature Commander|"' in after
