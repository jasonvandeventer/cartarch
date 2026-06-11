"""Brew Mode tests.

A "brew" is a deck built from cards the user may not own. Adding/importing an
UNOWNED card to a brew flags the created row as a proxy (so it never counts
toward owned totals).

Buy-list semantics REVISED 2026-06-11 (see brew-buylist-bug-2026-06-11): the
brew's own rows are self-describing — a real (non-proxy) deck row = owned, a
proxy deck row = to buy — regardless of where the physical copy sits. This
supersedes the v3.37.0 "real copy OUTSIDE this deck" rule, which false-flagged
every singleton the user deliberately decked.

Covers:
  - create_deck / update_deck is_brew round-trip
  - build_brew_buylist row-classification semantics (proxy deck row -> to buy;
    real deck row -> owned, even when it's the only copy and sits in the deck)
  - the proxy rule lives in _commit_deck_import_with_reconciliation, so BOTH a
    paste/CSV deck import AND the single-card add-card route proxy unowned cards
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


def test_buylist_proxy_rows_are_to_buy_real_rows_are_owned():
    s = _fresh()()
    u = _user(s)
    brew_loc = _loc(s, u.id, "Brew", type_="deck", mode="manual")
    sol = _card(s, "Sol Ring")
    man = _card(s, "Mana Crypt")
    # The brew holds a REAL (pulled) Sol Ring and a PROXY Mana Crypt.
    _place(s, u.id, sol, brew_loc.id, proxy=False)
    _place(s, u.id, man, brew_loc.id, proxy=True)
    s.commit()

    deck_rows = s.query(InventoryRow).filter(InventoryRow.storage_location_id == brew_loc.id).all()
    bl = build_brew_buylist(s, u.id, deck_rows, brew_loc.id)
    assert _names(bl["have"]) == ["Sol Ring"]  # real deck row -> owned
    assert _names(bl["missing"]) == ["Mana Crypt"]  # proxy deck row -> to buy


def test_buylist_real_copy_inside_deck_is_owned():
    """Regression for brew-buylist-bug-2026-06-11: a REAL (non-proxy) singleton
    pulled INTO the brew deck must count as OWNED, even though its only copy
    now lives in the deck. The old 'outside THIS deck' rule wrongly flagged it
    to buy."""
    s = _fresh()()
    u = _user(s)
    brew_loc = _loc(s, u.id, "Brew", type_="deck", mode="manual")
    sol = _card(s, "Sol Ring")
    _place(s, u.id, sol, brew_loc.id, proxy=False)  # real, the only copy, in the deck
    s.commit()
    deck_rows = s.query(InventoryRow).filter(InventoryRow.storage_location_id == brew_loc.id).all()
    bl = build_brew_buylist(s, u.id, deck_rows, brew_loc.id)
    assert _names(bl["have"]) == ["Sol Ring"]
    assert bl["missing"] == []


def test_buylist_partial_when_real_and_proxy_mix():
    """Want 2, one real + one proxy deck row -> partially owned."""
    s = _fresh()()
    u = _user(s)
    brew_loc = _loc(s, u.id, "Brew", type_="deck", mode="manual")
    bolt = _card(s, "Lightning Bolt")
    _place(s, u.id, bolt, brew_loc.id, qty=1, proxy=False)
    _place(s, u.id, bolt, brew_loc.id, qty=1, proxy=True)
    s.commit()
    deck_rows = s.query(InventoryRow).filter(InventoryRow.storage_location_id == brew_loc.id).all()
    bl = build_brew_buylist(s, u.id, deck_rows, brew_loc.id)
    assert _names(bl["partial"]) == ["Lightning Bolt"]
    entry = bl["partial"][0]
    assert entry["owned"] == 1
    assert entry["wanted"] == 2


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


def test_import_brew_proxies_unowned_moves_owned():
    """The paste/CSV deck-import path (the reconciliation commit handler) is the
    single source of the brew proxy rule: an UNOWNED row (import_new) becomes a
    proxy deck row, while an OWNED row (move_existing) is pulled in as a real
    (non-proxy) deck row. Regression for brew-buylist-bug-2026-06-11, where the
    paste import created real rows for everything and the buy-list then
    false-flagged owned singletons."""
    from app.routes.imports import _commit_deck_import_with_reconciliation

    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    box = _loc(s, u.id, "Box", mode="managed")
    owned = _card(s, "Smothering Tithe")
    unowned = _card(s, "Rhystic Study")
    _place(s, u.id, owned, box.id, qty=1, proxy=False)  # a REAL owned copy to pull
    s.commit()

    parsed_rows = [
        {
            "line_number": 1,
            "name": "",
            "scryfall_id": owned.scryfall_id,
            "set_code": "",
            "collector_number": "",
            "finish": "normal",
            "quantity": 1,
            "location": "",
        },
        {
            "line_number": 2,
            "name": "",
            "scryfall_id": unowned.scryfall_id,
            "set_code": "",
            "collector_number": "",
            "finish": "normal",
            "quantity": 1,
            "location": "",
        },
    ]
    _commit_deck_import_with_reconciliation(
        session=s,
        user_id=u.id,
        deck=deck,
        parsed_rows=parsed_rows,
        actions=["move_existing", "import_new"],
        move_qtys=[1, 0],
        new_qtys=[0, 1],
        filename="paste",
    )

    rows = {r.card_id: r for r in _deck_rows_for(s, deck)}
    assert rows[owned.id].is_proxy is False  # pulled real copy
    assert rows[unowned.id].is_proxy is True  # unowned -> proxy

    # And the buy-list reflects it: owned -> have, unowned -> to buy.
    bl = build_brew_buylist(s, u.id, list(rows.values()), deck.storage_location_id)
    assert _names(bl["have"]) == ["Smothering Tithe"]
    assert _names(bl["missing"]) == ["Rhystic Study"]


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


# --------------------------------------------------------------------------- #
# Phase 2 (brew-buylist-bug-2026-06-11): oracle-level matching within the
# UNASSIGNED pool only, proxy creation, and the Option-B buy-list bucket.
# --------------------------------------------------------------------------- #


def _card_set(s, name, set_code, coll) -> Card:
    """Card with an explicit set_code/collector (the _card helper hardcodes
    'tst' for everything, which can't model two printings of one card)."""
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code=set_code,
        set_name=set_code.upper(),
        collector_number=str(coll),
        rarity="rare",
        type_line="Creature",
        oracle_text="x",
        image_url="http://x/img.png",
        color_identity="",
        set_type="expansion",
    )
    s.add(c)
    s.flush()
    return c


def _parsed(card, qty=1, finish="normal", line=1):
    return {
        "line_number": line,
        "name": "",
        "scryfall_id": card.scryfall_id,
        "set_code": card.set_code,
        "collector_number": card.collector_number,
        "finish": finish,
        "quantity": qty,
        "location": "",
    }


def test_brew_oracle_fallback_claims_other_printing():
    """A brew owns a DIFFERENT printing of the requested card in the unassigned
    pool → it CLAIMS that owned printing (move_existing), never proxies. The
    deck ends up holding the physically-owned printing, not the paste's."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    drawer = _loc(s, u.id, "Drawer 2", mode="managed")
    isd = _card_set(s, "Guttersnipe", "isd", 100)  # paste requests ISD
    dmu = _card_set(s, "Guttersnipe", "dmu", 200)  # user owns DMU, unassigned
    _place(s, u.id, dmu, drawer.id, proxy=False)
    s.commit()

    out = deck_service.find_inventory_matches_for_deck_import(s, u.id, deck.id, [_parsed(isd)])[0]
    assert out["recommended_action"] == "move_existing"
    assert out["total_available"] == 1
    assert out["brew_claim_fallback"] is True
    assert out["brew_claim_set"] == "dmu"

    from app.routes.imports import _commit_deck_import_with_reconciliation

    _commit_deck_import_with_reconciliation(
        session=s,
        user_id=u.id,
        deck=deck,
        parsed_rows=[_parsed(isd)],
        actions=["move_existing"],
        move_qtys=[1],
        new_qtys=[0],
        filename="paste",
    )
    rows = _deck_rows_for(s, deck)
    assert len(rows) == 1
    assert rows[0].is_proxy is False  # claimed, not proxied
    assert rows[0].card_id == dmu.id  # the physically-owned printing


def test_brew_foil_request_falls_back_to_owned_nonfoil():
    """Foil is a preference, not a reason to proxy: a foil-requested line claims
    an owned NON-foil copy of the same printing rather than creating a proxy."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    drawer = _loc(s, u.id, "Drawer 2", mode="managed")
    card = _card_set(s, "Mizzix's Mastery", "c15", 10)
    _place(s, u.id, card, drawer.id, proxy=False)  # normal finish
    s.commit()

    out = deck_service.find_inventory_matches_for_deck_import(
        s, u.id, deck.id, [_parsed(card, finish="foil")]
    )[0]
    assert out["recommended_action"] == "move_existing"
    assert out["total_available"] == 1


def test_brew_deck_assigned_copy_never_claimed_and_not_duplicated():
    """Defect C pin: a card whose ONLY real copy lives in ANOTHER deck is NOT
    claimed and NOT duplicated — it becomes a proxy in the brew, the other
    deck's row is untouched, and no second real row is fabricated."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    other = deck_service.create_deck(s, u.id, "Buttercup")
    card = _card_set(s, "Cyclonic Rift", "soa", 14)
    _place(s, u.id, card, other.storage_location_id, proxy=False)  # only copy, in Buttercup
    s.commit()

    out = deck_service.find_inventory_matches_for_deck_import(s, u.id, brew.id, [_parsed(card)])[0]
    assert out["recommended_action"] == "import_new"
    assert out["total_available"] == 0  # unassigned pool is empty
    assert out["brew_owned_in_other_deck"] is True
    assert out["brew_other_deck_names"] == ["Buttercup"]

    from app.routes.imports import _commit_deck_import_with_reconciliation

    _commit_deck_import_with_reconciliation(
        session=s,
        user_id=u.id,
        deck=brew,
        parsed_rows=[_parsed(card)],
        actions=["import_new"],
        move_qtys=[0],
        new_qtys=[1],
        filename="paste",
    )
    # Brew holds exactly one PROXY copy.
    brew_rows = _deck_rows_for(s, brew)
    assert len(brew_rows) == 1
    assert brew_rows[0].is_proxy is True
    # Buttercup's real copy is untouched; no duplicate REAL row exists anywhere.
    real_rows = (
        s.query(InventoryRow)
        .filter(InventoryRow.card_id == card.id, InventoryRow.is_proxy.is_(False))
        .all()
    )
    assert len(real_rows) == 1
    assert real_rows[0].storage_location_id == other.storage_location_id


def test_brew_truly_unowned_creates_proxy_not_real_row():
    """Defect A pin: an UNOWNED card imported into a brew becomes a proxy row —
    never a phantom REAL inventory row that pollutes owned totals."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    card = _card_set(s, "Ponder", "m12", 73)  # owned nowhere
    s.commit()

    from app.routes.imports import _commit_deck_import_with_reconciliation

    _commit_deck_import_with_reconciliation(
        session=s,
        user_id=u.id,
        deck=brew,
        parsed_rows=[_parsed(card)],
        actions=["import_new"],
        move_qtys=[0],
        new_qtys=[1],
        filename="paste",
    )
    real_rows = s.query(InventoryRow).filter(InventoryRow.is_proxy.is_(False)).all()
    assert real_rows == []  # no phantom real row
    brew_rows = _deck_rows_for(s, brew)
    assert len(brew_rows) == 1 and brew_rows[0].is_proxy is True


def test_non_brew_has_no_oracle_fallback():
    """Brew-specific guard: a NORMAL deck does NOT claim a different printing —
    printing-exact matching is unchanged, so a different-printing owned copy
    still recommends import_new."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Normal")  # is_brew False
    drawer = _loc(s, u.id, "Drawer 2", mode="managed")
    isd = _card_set(s, "Guttersnipe", "isd", 100)
    dmu = _card_set(s, "Guttersnipe", "dmu", 200)
    _place(s, u.id, dmu, drawer.id, proxy=False)
    s.commit()

    out = deck_service.find_inventory_matches_for_deck_import(s, u.id, deck.id, [_parsed(isd)])[0]
    assert out["recommended_action"] == "import_new"
    assert out["total_available"] == 0


def test_buylist_owned_elsewhere_bucket():
    """Option B: a proxy brew row whose oracle card is owned (real) in ANOTHER
    deck goes to the owned_elsewhere bucket with the holding deck's name — NOT
    to 'to buy'."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    brew_loc = _loc(s, u.id, "Brew", type_="deck", mode="manual")
    other = deck_service.create_deck(s, u.id, "Buttercup")
    rift = _card_set(s, "Cyclonic Rift", "soa", 14)
    # brew holds a PROXY; a REAL copy lives in another deck.
    _place(s, u.id, rift, brew_loc.id, proxy=True)
    _place(s, u.id, rift, other.storage_location_id, proxy=False)
    s.commit()

    deck_rows = s.query(InventoryRow).filter(InventoryRow.storage_location_id == brew_loc.id).all()
    bl = build_brew_buylist(s, u.id, deck_rows, brew_loc.id)
    assert _names(bl["owned_elsewhere"]) == ["Cyclonic Rift"]
    assert bl["missing"] == []
    assert bl["owned_elsewhere"][0]["in_deck"] == "Buttercup"


def test_buylist_truly_unowned_still_to_buy():
    """Option B must not over-reach: a proxy whose card is owned NOWHERE (not
    even in another deck) stays in 'to buy'."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    brew_loc = _loc(s, u.id, "Brew", type_="deck", mode="manual")
    ponder = _card_set(s, "Ponder", "m12", 73)
    _place(s, u.id, ponder, brew_loc.id, proxy=True)
    s.commit()

    deck_rows = s.query(InventoryRow).filter(InventoryRow.storage_location_id == brew_loc.id).all()
    bl = build_brew_buylist(s, u.id, deck_rows, brew_loc.id)
    assert _names(bl["missing"]) == ["Ponder"]
    assert bl["owned_elsewhere"] == []
