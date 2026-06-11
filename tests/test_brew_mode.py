"""Brew Mode (v3.37.0) tests.

A "brew" is a deck built from cards the user may not own. Adding an UNOWNED
card to a brew flags the created row as a proxy (so it never counts toward owned
totals); the deck detail shows an owned/missing buy-list where "owned" means a
REAL (non-proxy) copy OUTSIDE this deck (owner decision 2026-06-10).

Covers (design note §Migration-plan touchpoint 3):
  - create_deck / update_deck is_brew round-trip
  - build_brew_buylist exclusion semantics (proxy-in-deck excluded; real-outside
    counts; real-INSIDE-deck excluded)
  - compare_entries_to_owned with no exclusions == pre-extraction bucketing
    (have / partial / missing / basics) — pins the /decklist extraction
  - add-card route: brew + unowned -> proxy row in deck location;
    brew + owned -> moved, NOT proxied; non-brew + unowned -> NOT proxied
    (the regression guard that the existing add-card path is byte-identical)
"""

from __future__ import annotations

import itertools

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import deck_service
from app.db import Base
from app.decklist_service import build_brew_buylist, compare_entries_to_owned
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
    """Non-stale Card so get_or_create_card never refetches (no request-path network)."""
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


def _loc(s, user_id, name, type_="box", mode="managed") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode=mode)
    s.add(loc)
    s.flush()
    return loc


def _place(s, user_id, card, loc_id, qty=1, proxy=False) -> InventoryRow:
    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=qty,
        finish="normal",
        is_proxy=proxy,
        storage_location_id=loc_id,
        is_pending=False,
    )
    s.add(row)
    s.flush()
    return row


def _names(bucket):
    return sorted(e["name"] for e in bucket)


# --------------------------------------------------------------------------- #
# Schema / service round-trip
# --------------------------------------------------------------------------- #


def test_create_and_update_is_brew():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    normal = deck_service.create_deck(s, u.id, "Normal")
    assert brew.is_brew is True
    assert normal.is_brew is False

    # update_deck always sets is_brew (the edit form always posts the checkbox).
    deck_service.update_deck(s, deck_id=brew.id, user_id=u.id, name="Brew", is_brew=False)
    s.refresh(brew)
    assert brew.is_brew is False
    deck_service.update_deck(s, deck_id=normal.id, user_id=u.id, name="Normal", is_brew=True)
    s.refresh(normal)
    assert normal.is_brew is True


# --------------------------------------------------------------------------- #
# build_brew_buylist — the owner-chosen "owned" semantics
# --------------------------------------------------------------------------- #


def test_buylist_excludes_proxies_and_own_deck():
    s = _fresh()()
    u = _user(s)
    brew_loc = _loc(s, u.id, "Brew", type_="deck", mode="manual")
    box = _loc(s, u.id, "Box")
    sol = _card(s, "Sol Ring")
    man = _card(s, "Mana Crypt")
    # Brew holds both as its own proxies; a REAL Sol Ring lives in a box.
    _place(s, u.id, sol, brew_loc.id, proxy=True)
    _place(s, u.id, man, brew_loc.id, proxy=True)
    _place(s, u.id, sol, box.id, proxy=False)
    s.commit()

    deck_rows = s.query(InventoryRow).filter(InventoryRow.storage_location_id == brew_loc.id).all()
    bl = build_brew_buylist(s, u.id, deck_rows, brew_loc.id)
    assert _names(bl["have"]) == ["Sol Ring"]  # real copy outside the deck
    assert _names(bl["missing"]) == ["Mana Crypt"]  # only proxies -> must buy


def test_buylist_real_copy_inside_deck_still_missing():
    """A REAL (non-proxy) copy pulled INTO the brew deck is still excluded by
    location — owner decision is 'outside THIS deck'."""
    s = _fresh()()
    u = _user(s)
    brew_loc = _loc(s, u.id, "Brew", type_="deck", mode="manual")
    sol = _card(s, "Sol Ring")
    _place(s, u.id, sol, brew_loc.id, proxy=False)  # real, but inside the deck
    s.commit()
    deck_rows = s.query(InventoryRow).filter(InventoryRow.storage_location_id == brew_loc.id).all()
    bl = build_brew_buylist(s, u.id, deck_rows, brew_loc.id)
    assert _names(bl["missing"]) == ["Sol Ring"]
    assert bl["have"] == []


def test_buylist_basics_not_in_buy_sections():
    s = _fresh()()
    u = _user(s)
    brew_loc = _loc(s, u.id, "Brew", type_="deck", mode="manual")
    forest = _card(s, "Forest")
    _place(s, u.id, forest, brew_loc.id, qty=10, proxy=True)
    s.commit()
    deck_rows = s.query(InventoryRow).filter(InventoryRow.storage_location_id == brew_loc.id).all()
    bl = build_brew_buylist(s, u.id, deck_rows, brew_loc.id)
    # Basics bucket out — you don't "buy" basic lands.
    assert _names(bl["basics"]) == ["Forest"]
    assert bl["missing"] == []


# --------------------------------------------------------------------------- #
# compare_entries_to_owned — pins the /decklist extraction (no exclusions)
# --------------------------------------------------------------------------- #


def test_compare_buckets_have_partial_missing():
    s = _fresh()()
    u = _user(s)
    box = _loc(s, u.id, "Box")
    have_card = _card(s, "Counterspell")
    partial_card = _card(s, "Brainstorm")
    _missing_card = _card(s, "Ponder")
    _place(s, u.id, have_card, box.id, qty=2)
    _place(s, u.id, partial_card, box.id, qty=1)
    s.commit()

    entries = [
        {"name": "Counterspell", "quantity": 1, "is_basic": False, "line_numbers": []},
        {"name": "Brainstorm", "quantity": 2, "is_basic": False, "line_numbers": []},
        {"name": "Ponder", "quantity": 1, "is_basic": False, "line_numbers": []},
        {"name": "Island", "quantity": 5, "is_basic": True, "line_numbers": []},
    ]
    buckets = compare_entries_to_owned(s, u.id, entries)
    assert _names(buckets["have"]) == ["Counterspell"]  # own 2 >= want 1
    assert _names(buckets["partial"]) == ["Brainstorm"]  # own 1 < want 2
    assert _names(buckets["missing"]) == ["Ponder"]  # own 0
    assert _names(buckets["basics"]) == ["Island"]  # basic regardless


# --------------------------------------------------------------------------- #
# add-card route — proxy branching + non-brew regression
# --------------------------------------------------------------------------- #


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


def _deck_rows_for(s, deck):
    return (
        s.query(InventoryRow)
        .filter(InventoryRow.storage_location_id == deck.storage_location_id)
        .all()
    )


def test_addcard_brew_unowned_becomes_proxy():
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    card = _card(s, "Rhystic Study")  # owned nowhere
    s.commit()

    c = _client(sm, u)
    try:
        r = c.post(
            f"/decks/{deck.id}/add-card",
            data={"scryfall_id": card.scryfall_id, "finish": "normal", "quantity": "1"},
        )
        assert r.status_code == 303
    finally:
        _clear_overrides()

    rows = _deck_rows_for(s, deck)
    assert len(rows) == 1
    assert rows[0].is_proxy is True
    assert rows[0].is_pending is False
    assert rows[0].card_id == card.id


def test_addcard_brew_owned_is_moved_not_proxied():
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    box = _loc(s, u.id, "Box", mode="managed")
    card = _card(s, "Smothering Tithe")
    _place(s, u.id, card, box.id, qty=1, proxy=False)  # a REAL owned copy
    s.commit()

    c = _client(sm, u)
    try:
        r = c.post(
            f"/decks/{deck.id}/add-card",
            data={"scryfall_id": card.scryfall_id, "finish": "normal", "quantity": "1"},
        )
        assert r.status_code == 303
    finally:
        _clear_overrides()

    rows = _deck_rows_for(s, deck)
    assert len(rows) == 1
    # Owned add uses normal pull semantics — moved in, NOT flagged proxy.
    assert rows[0].is_proxy is False
    assert rows[0].card_id == card.id


def test_addcard_non_brew_unowned_not_proxied():
    """Regression: the existing add-card path on a NORMAL deck is unchanged —
    an unowned add is a real (non-proxy) row, exactly as before v3.37.0."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Normal")  # is_brew False
    card = _card(s, "Cyclonic Rift")  # owned nowhere
    s.commit()

    c = _client(sm, u)
    try:
        r = c.post(
            f"/decks/{deck.id}/add-card",
            data={"scryfall_id": card.scryfall_id, "finish": "normal", "quantity": "1"},
        )
        assert r.status_code == 303
    finally:
        _clear_overrides()

    rows = _deck_rows_for(s, deck)
    assert len(rows) == 1
    assert rows[0].is_proxy is False
