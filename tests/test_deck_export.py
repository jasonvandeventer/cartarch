"""Deck export round-trips foil/finish status (issue: exporter omits finish).

The deck text-list export (`GET /decks/{id}/export`) must emit the importer's
MTGA-style finish markers — ``*F*`` for foil, ``*E*`` for etched — so an
export→re-import round-trip matches the copy back to its ``(card_id, finish)``
inventory row instead of treating it as a brand-new card. Non-foil/etched rows
carry no marker, and older exports without any marker still parse as normal
finish (backward compatible).

Invoke via:

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_deck_export.py
"""

from __future__ import annotations

import itertools

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import deck_service
from app.db import Base
from app.import_service import _parse_list_line
from app.models import Card, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _fresh():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _user(s, username="u1") -> User:
    u = User(username=username, password_hash="x")
    s.add(u)
    s.flush()
    return u


def _card(s, name="Sol Ring") -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="Test",
        collector_number=str(next(_seq)),
        rarity="rare",
        type_line="Artifact",
        oracle_text="x",
        image_url="http://x/img.png",
        color_identity="",
        set_type="expansion",
    )
    s.add(c)
    s.flush()
    return c


def _drawer(s, user_id, name="Drawer 1") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type="drawer", mode="managed")
    s.add(loc)
    s.flush()
    return loc


def _place(s, user_id, card, loc_id, qty=1, finish="normal", role=None) -> InventoryRow:
    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=qty,
        finish=finish,
        is_proxy=False,
        storage_location_id=loc_id,
        is_pending=False,
        role=role,
    )
    s.add(row)
    s.flush()
    return row


def _client(sm, user):
    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    def _db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _db
    main.app.dependency_overrides[get_current_user] = lambda: user
    main.app.dependency_overrides[require_csrf_token] = lambda: None
    return TestClient(main.app, follow_redirects=False)


def _clear_overrides():
    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    for dep in (get_db_session, get_current_user, require_csrf_token):
        main.app.dependency_overrides.pop(dep, None)


def test_export_marks_foil_rows_and_omits_marker_for_normal():
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Stash")
    loc_id = deck.storage_location_id
    normal_card = _card(s, "Sol Ring")
    foil_card = _card(s, "Mana Crypt")
    _place(s, u.id, normal_card, loc_id, finish="normal")
    _place(s, u.id, foil_card, loc_id, finish="foil")
    s.commit()

    c = _client(sm, u)
    try:
        r = c.get(f"/decks/{deck.id}/export")
        assert r.status_code == 200
    finally:
        _clear_overrides()

    lines = {ln.split(" ", 1)[1].split(" (")[0]: ln for ln in r.text.splitlines() if " (" in ln}
    # The foil row carries the *F* marker; the normal row does not.
    assert lines["Mana Crypt"].endswith("*F*")
    assert "*F*" not in lines["Sol Ring"]


def test_exported_foil_line_reparses_as_foil():
    """Round-trip: the exported foil line, fed back through the importer's own
    line parser, resolves to finish='foil' so re-import matches (card_id, foil)."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Stash")
    foil_card = _card(s, "Cyclonic Rift")
    _place(s, u.id, foil_card, deck.storage_location_id, finish="foil")
    s.commit()

    c = _client(sm, u)
    try:
        r = c.get(f"/decks/{deck.id}/export")
    finally:
        _clear_overrides()

    foil_line = next(ln for ln in r.text.splitlines() if "Cyclonic Rift" in ln)
    parsed = _parse_list_line(foil_line)
    assert parsed is not None
    assert parsed["finish"] == "foil"


def test_legacy_export_without_marker_parses_as_normal():
    """A pre-fix export line (no *F* marker) still parses — defaults to normal."""
    parsed = _parse_list_line("1 Sol Ring (TST) 1")
    assert parsed is not None
    assert parsed["finish"] == "normal"


def test_export_marks_etched_rows_and_reparses_as_etched():
    """Etched finish is not silently downgraded to normal: the export emits the
    *E* marker and the importer parses it back to finish='etched'."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Stash")
    etched_card = _card(s, "Command Tower")
    _place(s, u.id, etched_card, deck.storage_location_id, finish="etched")
    s.commit()

    c = _client(sm, u)
    try:
        r = c.get(f"/decks/{deck.id}/export")
    finally:
        _clear_overrides()

    etched_line = next(ln for ln in r.text.splitlines() if "Command Tower" in ln)
    assert etched_line.endswith("*E*")
    parsed = _parse_list_line(etched_line)
    assert parsed is not None
    assert parsed["finish"] == "etched"


def test_json_export_has_metadata_and_rollup():
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Brew")
    loc_id = deck.storage_location_id
    ring = _card(s, "Sol Ring")
    ring.cmc = 1.0
    bolt = _card(s, "Lightning Bolt")
    bolt.cmc = 1.0
    bolt.type_line = "Instant"
    bolt.color_identity = "R"
    _place(s, u.id, ring, loc_id, qty=1)
    _place(s, u.id, bolt, loc_id, qty=2)
    s.commit()

    c = _client(sm, u)
    try:
        r = c.get(f"/decks/{deck.id}/export?format=json")
        assert r.status_code == 200
        assert "application/json" in r.headers["content-type"]
        data = r.json()
    finally:
        _clear_overrides()

    assert data["deck"]["name"] == "Brew"
    assert len(data["cards"]) == 2
    roll = data["rollup"]
    assert roll["color_identity"] == ["R"]  # union, sorted
    assert roll["type_counts"] == {"Artifact": 1, "Instant": 2}
    assert roll["mana_value_histogram"] == {"1": 3}  # qty-weighted


def test_reimport_matches_existing_foil_inventory_row():
    """The functional round-trip: an exported foil line, re-imported, MATCHES the
    user's foil inventory row on (card_id, finish) instead of importing new.
    A normal-finish request against the same foil holding must NOT match."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    drawer = _drawer(s, u.id)
    target = deck_service.create_deck(s, u.id, "Target")
    foil_card = _card(s, "Cyclonic Rift")
    # The user owns exactly one FOIL copy, sitting in a drawer (movable).
    _place(s, u.id, foil_card, drawer.id, finish="foil")
    s.commit()

    # Re-import line the exporter would emit, parsed by the importer's grammar.
    parsed = _parse_list_line(
        f"1 Cyclonic Rift ({foil_card.set_code.upper()}) {foil_card.collector_number} *F*"
    )
    assert parsed is not None and parsed["finish"] == "foil"

    def _row(finish):
        return [
            {
                "line_number": 1,
                "scryfall_id": foil_card.scryfall_id,
                "finish": finish,
                "quantity": 1,
            }
        ]

    # Foil request → the foil drawer row is a movable match.
    foil_match = deck_service.find_inventory_matches_for_deck_import(
        s, u.id, target.id, _row(parsed["finish"])
    )[0]
    assert foil_match["card_id"] == foil_card.id
    assert foil_match["total_available"] == 1
    assert foil_match["recommended_action"] == "move_existing"

    # Normal-finish request → the foil row is a DIFFERENT (card_id, finish) key,
    # so it must NOT match; the card is treated as a new import.
    normal_match = deck_service.find_inventory_matches_for_deck_import(
        s, u.id, target.id, _row("normal")
    )[0]
    assert normal_match["total_available"] == 0
    assert normal_match["recommended_action"] == "import_new"


# --------------------------------------------------------------------------
# #179 — the deck JSON export names the commander via the ONE definition.
# --------------------------------------------------------------------------


def test_json_export_reports_a_role_tagged_commander():
    """The common case: 41 of 49 live decks carry a role='commander' row."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Atraxa")
    atraxa = _card(s, "Atraxa, Praetors' Voice")
    _place(s, u.id, atraxa, deck.storage_location_id, role="commander")
    s.commit()

    c = _client(sm, u)
    try:
        data = c.get(f"/decks/{deck.id}/export?format=json").json()
    finally:
        _clear_overrides()

    assert [x["name"] for x in data["commanders"]] == ["Atraxa, Praetors' Voice"]


def test_json_export_falls_back_to_the_deck_commanders_anchor():
    """THE regression this exists to prevent (v4.12.40, in a fourth surface).

    A deck whose commander lives ONLY in the #163 ``deck_commanders`` anchor has
    no ``role='commander'`` inventory row, so a consumer reading commanders off
    each card's ``role`` reports none — silently, with nothing distinguishing it
    from a deck that genuinely has no commander. Measured on prod 2026-08-03:
    **4 of 49 live decks are in exactly this state.**
    """
    from app.models import DeckCommander

    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Anchor Only")
    kenrith = _card(s, "Kenrith, the Returned King")
    # A card in the deck, but NOTHING tagged as the commander role.
    filler = _card(s, "Sol Ring")
    _place(s, u.id, filler, deck.storage_location_id)
    s.add(DeckCommander(deck_id=deck.id, card_id=kenrith.id))
    s.commit()

    c = _client(sm, u)
    try:
        data = c.get(f"/decks/{deck.id}/export?format=json").json()
    finally:
        _clear_overrides()

    assert [x["name"] for x in data["commanders"]] == ["Kenrith, the Returned King"]
    # The anchor commander has NO inventory row, so it is deliberately absent
    # from `cards` — which is why it is a top-level key and not a card flag.
    assert [x["name"] for x in data["cards"]] == ["Sol Ring"]


def test_json_export_reports_no_commander_when_there_is_none():
    """The control: an unknown commander must not be manufactured."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Bare")
    _place(s, u.id, _card(s, "Sol Ring"), deck.storage_location_id)
    s.commit()

    c = _client(sm, u)
    try:
        data = c.get(f"/decks/{deck.id}/export?format=json").json()
    finally:
        _clear_overrides()

    assert data["commanders"] == []


def test_the_text_export_does_NOT_gain_the_anchor_fallback():
    """Deliberate asymmetry, and it is load-bearing.

    The text export is an MTGA round-trip format, so a ``Commander`` line there
    asserts a card the deck CONTAINS. An anchor-only commander has no inventory
    row, so emitting it would make export → re-import add a card that was never
    in the deck. The JSON key is metadata; the text line is inventory.
    """
    from app.models import DeckCommander

    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Anchor Only")
    kenrith = _card(s, "Kenrith, the Returned King")
    _place(s, u.id, _card(s, "Sol Ring"), deck.storage_location_id)
    s.add(DeckCommander(deck_id=deck.id, card_id=kenrith.id))
    s.commit()

    c = _client(sm, u)
    try:
        text = c.get(f"/decks/{deck.id}/export").text
    finally:
        _clear_overrides()

    assert "Kenrith" not in text
    assert "Commander" not in text
    assert "Sol Ring" in text
