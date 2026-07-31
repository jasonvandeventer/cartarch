"""Deck legality panel (#176).

`check_deck_legality` reports banned cards, colour-identity violations, singleton
violations and deck size from persisted columns only.

**Every card in this file uses REAL stored strings** — `color_identity` is
space-separated ("B G", not "BG") and a snow basic's type line reads "Basic Snow
Land — Plains" without the "Basic Land" substring. Both of those produced
confident false positives during the measurement that motivated #176, and
synthesized single-format test data cannot catch either. Same rule as
`tests/test_commander_eligibility.py` (#160).
"""

import json

from app import deck_service
from app.models import DeckCommander
from tests.test_deck_card_shares import (
    _card,
    _deck,
    _fresh_session,
    _place,
    _user,
)


def _commander_deck(s, user_id, name="Deck", fmt="Commander"):
    deck = _deck(s, user_id, name)
    deck.format = fmt
    s.flush()
    return deck


def _c(s, name, *, identity="", type_line="Creature — Human", legalities=None, oracle=""):
    """A card carrying the REAL stored shapes: space-separated colour identity,
    an em-dash type line, and the Scryfall legalities JSON blob."""
    card = _card(s, name=name)
    card.color_identity = identity
    card.type_line = type_line
    card.oracle_text = oracle
    card.legalities = json.dumps(legalities or {"commander": "legal"})
    s.flush()
    return card


def _rows(s, deck):
    return deck_service.resolved_deck_rows(s, deck, deck.user_id)


def _seat_commander(s, u, deck, card):
    """Tag a commander the way the UI does — a role row in the deck location."""
    row = _place(s, u.id, card, deck.storage_location_id)
    row.role = "commander"
    s.flush()
    return row


# ── Banned cards ────────────────────────────────────────────────────────


def test_a_banned_card_is_reported():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    crypt = _c(s, "Mana Crypt", type_line="Artifact", legalities={"commander": "banned"})
    _place(s, u.id, crypt, deck.storage_location_id)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert [i["card"].name for i in out["banned"]] == ["Mana Crypt"]
    assert out["has_findings"] is True


def test_a_legal_card_is_not_reported():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _place(s, u.id, _c(s, "Sol Ring", type_line="Artifact"), deck.storage_location_id)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["banned"] == []


# ── Colour identity ─────────────────────────────────────────────────────


def test_an_off_colour_card_is_reported():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _seat_commander(s, u, deck, _c(s, "Mardu Commander", identity="B R W"))
    _place(s, u.id, _c(s, "Contest of Claws", identity="G"), deck.storage_location_id)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert [i["card"].name for i in out["off_color"]] == ["Contest of Claws"]


def test_a_two_colour_card_in_a_two_colour_deck_does_not_flag():
    """THE SPACE-SEPARATOR TRAP. `color_identity` is "B G", not "BG" — splitting
    per character makes the space a colour, and then every multicolour card in a
    deck flags. That produced 12 fake violations in one prod deck."""
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _seat_commander(s, u, deck, _c(s, "Golgari Commander", identity="B G"))
    _place(s, u.id, _c(s, "Golgari Signet", identity="B G"), deck.storage_location_id)
    _place(s, u.id, _c(s, "Llanowar Elves", identity="G"), deck.storage_location_id)
    _place(s, u.id, _c(s, "Wastes", identity=""), deck.storage_location_id)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["off_color"] == []


def test_partner_commanders_union_their_identities():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _seat_commander(s, u, deck, _c(s, "Kediss, Emberclaw Familiar", identity="R"))
    _seat_commander(s, u, deck, _c(s, "Malcolm, Keen-Eyed Navigator", identity="U"))
    _place(s, u.id, _c(s, "Izzet Signet", identity="R U"), deck.storage_location_id)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["off_color"] == []
    assert out["commander_identity"] == "U R"


def test_a_deck_with_no_commander_produces_no_colour_findings():
    """The most dangerous failure mode: an unknown identity is not an empty one.
    Flagging every coloured card in every #164 placeholder would make the panel
    noise, and a panel people dismiss catches nothing."""
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _place(s, u.id, _c(s, "Lightning Bolt", identity="R"), deck.storage_location_id)
    _place(s, u.id, _c(s, "Counterspell", identity="U"), deck.storage_location_id)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["off_color"] == []
    assert out["commander_identity"] == ""


def test_the_deck_commanders_anchor_resolves_the_identity_without_a_role_row():
    """A #164 placeholder has no role row — its commander lives only in the
    `deck_commanders` anchor. `deck_commander_cards` reads both, which is why
    this function calls it instead of filtering roles itself (v4.12.40)."""
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    cmd = _c(s, "Atraxa, Praetors' Voice", identity="B G U W")
    s.add(DeckCommander(deck_id=deck.id, card_id=cmd.id))
    s.flush()
    _place(s, u.id, _c(s, "Lightning Bolt", identity="R"), deck.storage_location_id)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert [i["card"].name for i in out["off_color"]] == ["Lightning Bolt"]


def test_a_colourless_commander_still_flags_coloured_cards():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _seat_commander(s, u, deck, _c(s, "Kozilek, Butcher of Truth", identity=""))
    _place(s, u.id, _c(s, "Lightning Bolt", identity="R"), deck.storage_location_id)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert [i["card"].name for i in out["off_color"]] == ["Lightning Bolt"]


# ── Singleton ───────────────────────────────────────────────────────────


def test_a_duplicated_card_is_reported():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _place(s, u.id, _c(s, "Brainstorm", identity="U"), deck.storage_location_id, qty=4)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert [(i["card"].name, i["copies"]) for i in out["duplicates"]] == [("Brainstorm", 4)]


def test_two_finish_rows_of_one_card_are_a_singleton_violation():
    """Singleton is a NAME-level rule. Two rows at quantity 1 each is still two
    copies, so the check sums by name rather than reading row quantities."""
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    sol = _c(s, "Sol Ring", type_line="Artifact")
    _place(s, u.id, sol, deck.storage_location_id, qty=1)
    _place(s, u.id, sol, deck.storage_location_id, qty=1, finish="foil")

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert [(i["card"].name, i["copies"]) for i in out["duplicates"]] == [("Sol Ring", 2)]


def test_ordinary_basics_are_exempt():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _place(
        s,
        u.id,
        _c(s, "Plains", identity="", type_line="Basic Land — Plains"),
        deck.storage_location_id,
        qty=30,
    )

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["duplicates"] == []


def test_snow_basics_are_exempt():
    """THE SNOW TRAP. "Basic Snow Land — Plains" does not contain the substring
    "Basic Land", so a naive filter flags 25 copies of a legal basic — which is
    exactly what happened on prod while measuring #176."""
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _place(
        s,
        u.id,
        _c(s, "Snow-Covered Plains", identity="", type_line="Basic Snow Land — Plains"),
        deck.storage_location_id,
        qty=25,
    )

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["duplicates"] == []


def test_any_number_of_cards_named_is_exempt():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _place(
        s,
        u.id,
        _c(
            s,
            "Rat Colony",
            identity="B",
            oracle="Rat Colony gets +1/+0 for each other Rat you control.\n"
            "A deck can have any number of cards named Rat Colony.",
        ),
        deck.storage_location_id,
        qty=20,
    )

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["duplicates"] == []


# ── Deck size ───────────────────────────────────────────────────────────


def test_a_short_deck_reports_its_size():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _place(s, u.id, _c(s, "Sol Ring", type_line="Artifact"), deck.storage_location_id, qty=99)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["size"] == {"total": 99, "expected": 100}


def test_a_hundred_card_deck_is_clean():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _seat_commander(s, u, deck, _c(s, "Kozilek, Butcher of Truth", identity=""))
    _place(
        s,
        u.id,
        _c(s, "Wastes", identity="", type_line="Basic Land — Wastes"),
        deck.storage_location_id,
        qty=99,
    )

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["has_findings"] is False
    assert out["size"] is None


# ── Format gating ───────────────────────────────────────────────────────


def test_a_non_commander_deck_skips_the_commander_checks():
    """`format` is free text and demonstrably wrong on live data — a 75-card
    Pauper list is filed as "Commander" in prod. A deck honestly labelled
    something else must not be judged by Commander's rules."""
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id, fmt="Pauper")
    _place(s, u.id, _c(s, "Brainstorm", identity="U"), deck.storage_location_id, qty=4)
    _place(s, u.id, _c(s, "Counterspell", identity="U"), deck.storage_location_id, qty=4)

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["duplicates"] == []
    assert out["size"] is None
    assert out["off_color"] == []


def test_banned_is_read_against_the_decks_own_format():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id, fmt="Pauper")
    _place(
        s,
        u.id,
        _c(
            s,
            "Mana Crypt",
            type_line="Artifact",
            legalities={"commander": "banned", "pauper": "not_legal"},
        ),
        deck.storage_location_id,
    )

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["banned"] == []


# ── Proxies ─────────────────────────────────────────────────────────────


def test_a_proxy_only_card_is_marked_as_a_proxy():
    """Both real findings measured on prod involve proxies. A staged placeholder
    is a different thing from a card in the physical deck, so the panel says
    which rather than conflating them."""
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    _seat_commander(s, u, deck, _c(s, "Mardu Commander", identity="B R W"))
    _place(
        s,
        u.id,
        _c(s, "Contest of Claws", identity="G"),
        deck.storage_location_id,
        is_proxy=True,
    )

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["off_color"][0]["is_proxy"] is True


def test_a_card_held_in_both_real_and_proxy_is_not_marked_a_proxy():
    s = _fresh_session()
    u = _user(s)
    deck = _commander_deck(s, u.id)
    crypt = _c(s, "Mana Crypt", type_line="Artifact", legalities={"commander": "banned"})
    _place(s, u.id, crypt, deck.storage_location_id, is_proxy=True)
    _place(s, u.id, crypt, deck.storage_location_id, finish="foil")

    out = deck_service.check_deck_legality(s, deck, _rows(s, deck))
    assert out["banned"][0]["is_proxy"] is False


# ── Route (#152: a service test cannot see a missing context key) ────────


def test_the_legality_panel_actually_renders_on_the_deck_page(client, db, user):
    from app.models import Card, Deck, InventoryRow, StorageLocation

    loc = StorageLocation(user_id=user.id, name="Banned Deck", type="deck", mode="managed")
    db.add(loc)
    db.flush()
    deck = Deck(user_id=user.id, name="Banned Deck", storage_location_id=loc.id, format="Commander")
    db.add(deck)
    card = Card(
        scryfall_id="sid-legality-1",
        name="Mana Crypt",
        set_code="tst",
        set_name="Test",
        collector_number="1",
        type_line="Artifact",
        color_identity="",
        legalities=json.dumps({"commander": "banned"}),
    )
    db.add(card)
    db.flush()
    db.add(
        InventoryRow(
            card_id=card.id,
            user_id=user.id,
            storage_location_id=loc.id,
            finish="normal",
            quantity=1,
            is_pending=False,
        )
    )
    db.commit()

    page = client.get(f"/decks/{deck.id}").text
    assert "Legality" in page
    assert "Mana Crypt" in page
    assert "Banned in Commander" in page


def test_a_clean_deck_renders_no_legality_panel(client, db, user):
    from app.models import Card, Deck, InventoryRow, StorageLocation

    loc = StorageLocation(user_id=user.id, name="Clean Deck", type="deck", mode="managed")
    db.add(loc)
    db.flush()
    deck = Deck(user_id=user.id, name="Clean Deck", storage_location_id=loc.id, format="Commander")
    db.add(deck)
    card = Card(
        scryfall_id="sid-legality-2",
        name="Wastes",
        set_code="tst",
        set_name="Test",
        collector_number="2",
        type_line="Basic Land — Wastes",
        color_identity="",
        legalities=json.dumps({"commander": "legal"}),
    )
    db.add(card)
    db.flush()
    db.add(
        InventoryRow(
            card_id=card.id,
            user_id=user.id,
            storage_location_id=loc.id,
            finish="normal",
            quantity=100,
            is_pending=False,
        )
    )
    db.commit()

    page = client.get(f"/decks/{deck.id}").text
    assert "legality-panel" not in page
