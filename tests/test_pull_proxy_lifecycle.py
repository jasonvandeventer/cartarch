"""Issue #134 — proxy lifecycle on pull_card_to_deck.

Pulling a REAL owned copy into a brew deck that holds a PROXY of the same
printing used to `quantity += 1` straight onto the proxy row (the merge key was
is_proxy-blind), so is_proxy stayed True and build_brew_buylist kept reporting
the card as unowned. The fix: proxy-aware merge key (real→real, proxy→proxy)
plus consume-the-proxy semantics (decrement the proxy by the incoming real
quantity, delete at zero) — the same intent as materialize_brew.
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.legacy_tables  # noqa: F401 — registers deck_bracket_* tables standalone
from app import deck_service
from app.db import Base
from app.decklist_service import build_brew_buylist
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


def _loc(s, user_id, name, type_="box", mode="managed") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode=mode)
    s.add(loc)
    s.flush()
    return loc


def _place(s, user_id, card, loc_id, qty=1, proxy=False, finish="normal") -> InventoryRow:
    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=qty,
        finish=finish,
        is_proxy=proxy,
        storage_location_id=loc_id,
        is_pending=False,
    )
    s.add(row)
    s.flush()
    return row


def _deck_rows(s, loc_id):
    return s.query(InventoryRow).filter(InventoryRow.storage_location_id == loc_id).all()


# --------------------------------------------------------------------------- #
# 1 — the named case: real copy consumes the proxy, buy-list flips to owned
# --------------------------------------------------------------------------- #


def test_pull_real_into_proxy_consumes_proxy():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    box = _loc(s, u.id, "Box")
    sol = _card(s, "Sol Ring")
    _place(s, u.id, sol, brew.storage_location_id, qty=1, proxy=True)  # deck proxy
    src = _place(s, u.id, sol, box.id, qty=1, proxy=False)  # owned real copy
    s.commit()

    assert deck_service.pull_card_to_deck(s, u.id, brew.id, src.id, 1) is True

    rows = _deck_rows(s, brew.storage_location_id)
    assert len(rows) == 1  # proxy consumed, not left beside the real row
    assert rows[0].is_proxy is False
    assert rows[0].quantity == 1
    assert s.query(InventoryRow).filter(InventoryRow.id == src.id).first() is None  # moved

    bl = build_brew_buylist(s, u.id, rows, brew.storage_location_id)
    assert sorted(e["name"] for e in bl["have"]) == ["Sol Ring"]
    assert bl["missing"] == []


# --------------------------------------------------------------------------- #
# 2 — multi-quantity proxy: pulling one shrinks the proxy, leaves a real row
# --------------------------------------------------------------------------- #


def test_pull_one_real_shrinks_multi_proxy():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    box = _loc(s, u.id, "Box")
    bolt = _card(s, "Lightning Bolt")
    _place(s, u.id, bolt, brew.storage_location_id, qty=2, proxy=True)
    src = _place(s, u.id, bolt, box.id, qty=1, proxy=False)
    s.commit()

    assert deck_service.pull_card_to_deck(s, u.id, brew.id, src.id, 1) is True

    rows = _deck_rows(s, brew.storage_location_id)
    real = [r for r in rows if not r.is_proxy]
    proxy = [r for r in rows if r.is_proxy]
    assert len(real) == 1 and real[0].quantity == 1
    assert len(proxy) == 1 and proxy[0].quantity == 1  # proxy 2 → 1


# --------------------------------------------------------------------------- #
# 3 — excess real quantity: proxy fully consumed, real row carries the total
# --------------------------------------------------------------------------- #


def test_pull_excess_real_removes_proxy():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    box = _loc(s, u.id, "Box")
    study = _card(s, "Rhystic Study")
    _place(s, u.id, study, brew.storage_location_id, qty=1, proxy=True)
    src = _place(s, u.id, study, box.id, qty=2, proxy=False)
    s.commit()

    assert deck_service.pull_card_to_deck(s, u.id, brew.id, src.id, 2) is True

    rows = _deck_rows(s, brew.storage_location_id)
    assert len(rows) == 1
    assert rows[0].is_proxy is False
    assert rows[0].quantity == 2  # both real copies


# --------------------------------------------------------------------------- #
# 4 — finish mismatch: a foil real copy never consumes a nonfoil proxy
# --------------------------------------------------------------------------- #


def test_pull_finish_mismatch_keeps_distinct_rows():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    box = _loc(s, u.id, "Box")
    crypt = _card(s, "Mana Crypt")
    _place(s, u.id, crypt, brew.storage_location_id, qty=1, proxy=True, finish="normal")
    src = _place(s, u.id, crypt, box.id, qty=1, proxy=False, finish="foil")
    s.commit()

    assert deck_service.pull_card_to_deck(s, u.id, brew.id, src.id, 1) is True

    rows = _deck_rows(s, brew.storage_location_id)
    assert len(rows) == 2  # no cross-finish consume/merge
    proxy = next(r for r in rows if r.is_proxy)
    real = next(r for r in rows if not r.is_proxy)
    assert proxy.finish == "normal" and proxy.quantity == 1
    assert real.finish == "foil" and real.quantity == 1


# --------------------------------------------------------------------------- #
# 5 — regression: pulling into an existing REAL deck row still merges qty
# --------------------------------------------------------------------------- #


def test_pull_into_existing_real_row_merges():
    s = _fresh()()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Normal")
    box = _loc(s, u.id, "Box")
    ring = _card(s, "The One Ring")
    _place(s, u.id, ring, deck.storage_location_id, qty=1, proxy=False)  # existing real
    src = _place(s, u.id, ring, box.id, qty=2, proxy=False)
    s.commit()

    assert deck_service.pull_card_to_deck(s, u.id, deck.id, src.id, 2) is True

    rows = _deck_rows(s, deck.storage_location_id)
    assert len(rows) == 1
    assert rows[0].is_proxy is False
    assert rows[0].quantity == 3  # 1 existing + 2 pulled
