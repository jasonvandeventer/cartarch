"""Brew-placeholder scoping (#140, Intent A).

A "brew placeholder" is an ``is_proxy`` InventoryRow whose storage location
belongs to an ``is_brew`` deck — a card the user does NOT own, existing only in
the context of that deck. Intent A keeps it as an inventory row (no new
representation) but scopes it out of everywhere it would masquerade as owned:

  - excluded from the collection index / exports / stats
    (``build_collection_filter_query`` + ``get_inventory_row_stats``)
  - can't be moved out of its deck (``move_inventory_row_to_location``)
  - discarded, NOT returned as a real pending row, when its deck slot clears
    (``return_card_from_deck`` — the fabricated-ownership defect this issue found)
  - can't be attached to a Showcase (bulk + per-card)

The discriminator is ``inventory_service.is_brew_placeholder_row`` / the SQL
``brew_placeholder_exclusion``; every call site reuses it (no inline drift).

The trade-offer guard (``trade_service.create_trade``) is the same
``if is_brew_placeholder_row(...): raise`` shape as the per-card showcase guard
tested here; a full playgroup+share+membership scaffold to exercise one branch
is disproportionate, so it rides on the helper test below.
ponytail: add a trade integration test if trade coverage grows a reusable setup.
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.legacy_tables  # noqa: F401 — registers deck_bracket_* for delete_deck cleanup
from app import deck_service, inventory_service, share_service
from app.db import Base
from app.models import Card, Deck, InventoryRow, StorageLocation, User

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


def _card(s, name) -> Card:
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


def _loc(s, user_id, name, type_="box") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode="managed")
    s.add(loc)
    s.flush()
    return loc


def _place(s, user_id, card, loc_id, qty=1, proxy=False, pending=False) -> InventoryRow:
    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=qty,
        finish="normal",
        is_proxy=proxy,
        storage_location_id=loc_id,
        is_pending=pending,
    )
    s.add(row)
    s.flush()
    return row


# --------------------------------------------------------------------------- #
# The discriminator itself
# --------------------------------------------------------------------------- #


def test_is_brew_placeholder_row_predicate():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    normal = deck_service.create_deck(s, u.id, "Normal")

    proxy_in_brew = _place(s, u.id, _card(s, "Ponder"), brew.storage_location_id, proxy=True)
    real_in_brew = _place(s, u.id, _card(s, "Sol Ring"), brew.storage_location_id, proxy=False)
    proxy_in_normal = _place(
        s, u.id, _card(s, "Brainstorm"), normal.storage_location_id, proxy=True
    )
    pending_proxy = _place(s, u.id, _card(s, "Opt"), None, proxy=True, pending=True)
    s.commit()

    assert inventory_service.is_brew_placeholder_row(s, proxy_in_brew) is True
    # a REAL card in a brew deck is owned — not a placeholder
    assert inventory_service.is_brew_placeholder_row(s, real_in_brew) is False
    # a proxy in a NON-brew deck is a physical proxy you own — not a placeholder
    assert inventory_service.is_brew_placeholder_row(s, proxy_in_normal) is False
    # a proxy with no location can't be in a brew deck
    assert inventory_service.is_brew_placeholder_row(s, pending_proxy) is False


# --------------------------------------------------------------------------- #
# Lifecycle: return_card_from_deck (the fabricated-ownership defect)
# --------------------------------------------------------------------------- #


def test_return_brew_proxy_discards_and_creates_no_pending_real_row():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    proxy = _place(s, u.id, _card(s, "Ponder"), brew.storage_location_id, proxy=True)
    s.commit()
    pid = proxy.id

    assert deck_service.return_card_from_deck(s, u.id, pid) is True

    rows = s.query(InventoryRow).all()
    assert rows == []  # discarded outright — NOT converted to a pending real row


def test_return_real_card_from_brew_still_returns_pending():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    real = _place(s, u.id, _card(s, "Sol Ring"), brew.storage_location_id, proxy=False)
    s.commit()

    assert deck_service.return_card_from_deck(s, u.id, real.id) is True
    rows = s.query(InventoryRow).all()
    assert len(rows) == 1
    assert rows[0].is_proxy is False
    assert rows[0].is_pending is True
    assert rows[0].storage_location_id is None


def test_return_proxy_from_non_brew_deck_is_not_discarded():
    """The discard is brew-gated: a proxy in a NON-brew deck follows the normal
    return path (a physical proxy you own returns to the collection)."""
    s = _fresh()()
    u = _user(s)
    normal = deck_service.create_deck(s, u.id, "Normal")
    proxy = _place(s, u.id, _card(s, "Brainstorm"), normal.storage_location_id, proxy=True)
    s.commit()

    assert deck_service.return_card_from_deck(s, u.id, proxy.id) is True
    rows = s.query(InventoryRow).all()
    assert len(rows) == 1  # returned, not deleted
    assert rows[0].is_pending is True


# --------------------------------------------------------------------------- #
# Lifecycle: move guard
# --------------------------------------------------------------------------- #


def test_move_brew_placeholder_out_of_deck_rejected():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    box = _loc(s, u.id, "Bulk")
    proxy = _place(s, u.id, _card(s, "Ponder"), brew.storage_location_id, proxy=True)
    s.commit()

    with pytest.raises(ValueError, match="brew placeholder"):
        inventory_service.move_inventory_row_to_location(s, proxy.id, u.id, box.id)
    # still in the deck, untouched
    assert s.get(InventoryRow, proxy.id).storage_location_id == brew.storage_location_id


def test_move_non_brew_deck_proxy_allowed():
    s = _fresh()()
    u = _user(s)
    normal = deck_service.create_deck(s, u.id, "Normal")
    box = _loc(s, u.id, "Bulk")
    proxy = _place(s, u.id, _card(s, "Brainstorm"), normal.storage_location_id, proxy=True)
    s.commit()

    inventory_service.move_inventory_row_to_location(s, proxy.id, u.id, box.id)
    assert s.get(InventoryRow, proxy.id).storage_location_id == box.id


# --------------------------------------------------------------------------- #
# Collection index / stats exclusion
# --------------------------------------------------------------------------- #


def test_collection_query_excludes_only_brew_placeholders():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    normal = deck_service.create_deck(s, u.id, "Normal")
    box = _loc(s, u.id, "Box")

    placeholder = _place(s, u.id, _card(s, "Ponder"), brew.storage_location_id, proxy=True)
    real_in_brew = _place(s, u.id, _card(s, "Sol Ring"), brew.storage_location_id, proxy=False)
    proxy_in_normal = _place(
        s, u.id, _card(s, "Brainstorm"), normal.storage_location_id, proxy=True
    )
    owned = _place(s, u.id, _card(s, "Island"), box.id, proxy=False)
    s.commit()

    ids = {r.id for r in inventory_service.build_collection_filter_query(s, u.id).all()}
    assert placeholder.id not in ids  # the only thing excluded
    assert real_in_brew.id in ids
    assert proxy_in_normal.id in ids
    assert owned.id in ids


def test_stats_exclude_brew_placeholder():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    box = _loc(s, u.id, "Box")
    _place(s, u.id, _card(s, "Ponder"), brew.storage_location_id, proxy=True)
    _place(s, u.id, _card(s, "Island"), box.id, proxy=False)
    s.commit()

    stats = inventory_service.get_inventory_row_stats(s, u.id)
    assert stats["unique_cards"] == 1  # only the owned Island, not the placeholder


# --------------------------------------------------------------------------- #
# Showcase attachment guards
# --------------------------------------------------------------------------- #


def test_add_rows_to_showcase_skips_brew_placeholder_even_with_include_proxies():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    _place(s, u.id, _card(s, "Ponder"), brew.storage_location_id, proxy=True)
    s.commit()
    showcase = share_service.get_or_create_showcase(s, u.id)

    result = share_service.add_rows_to_showcase(s, u.id, showcase.id, include_proxies=True)
    assert result["added"] == 0


def test_add_showcase_item_rejects_brew_placeholder():
    s = _fresh()()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "Brew", is_brew=True)
    placeholder = _place(s, u.id, _card(s, "Ponder"), brew.storage_location_id, proxy=True)
    s.commit()
    showcase = share_service.get_or_create_showcase(s, u.id)

    assert share_service.add_showcase_item(s, u.id, placeholder.id, showcase.id) is None


def test_a_brew_placeholder_is_not_offered_in_the_trade_picker(db):
    """The trade picker listed cards that could not be traded (SaintWacko,
    2026-08-21): ``create_trade`` has always REFUSED a brew placeholder, so
    offering one was an error message waiting to happen — and until v4.13.37 it
    also cost you every other pick on the page.

    A real proxy in an ordinary deck is NOT a placeholder and stays offerable;
    that distinction is the whole reason this uses the shared discriminator
    rather than a local ``is_proxy`` check.
    """
    from app import trade_service
    from app.models import Playgroup, PlaygroupMember, Share, Showcase, ShowcaseItem

    owner = _user(db, "owner@x.com")
    other = _user(db, "other@x.com")
    pg = Playgroup(name="Pod", created_by=owner.id)
    db.add(pg)
    db.flush()
    db.add_all(
        [
            PlaygroupMember(playgroup_id=pg.id, user_id=owner.id, role="owner"),
            PlaygroupMember(playgroup_id=pg.id, user_id=other.id, role="member"),
        ]
    )

    binder = _loc(db, owner.id, "Binder")
    brew_loc = _loc(db, owner.id, "Brew", type_="deck")
    db.add(Deck(user_id=owner.id, name="Brew", storage_location_id=brew_loc.id, is_brew=True))
    plain_loc = _loc(db, owner.id, "Real deck", type_="deck")
    db.add(Deck(user_id=owner.id, name="Real deck", storage_location_id=plain_loc.id))
    db.flush()

    real = _place(db, owner.id, _card(db, "Sol Ring"), binder.id)
    placeholder = _place(db, owner.id, _card(db, "Mana Crypt"), brew_loc.id, proxy=True)
    deck_proxy = _place(db, owner.id, _card(db, "Gaea's Cradle"), plain_loc.id, proxy=True)

    # The recipient needs a shared showcase for the picker to resolve at all.
    their_loc = _loc(db, other.id, "Theirs")
    theirs = _place(db, other.id, _card(db, "Rhystic Study"), their_loc.id)
    sc = Showcase(user_id=other.id, name="SC")
    db.add(sc)
    db.flush()
    db.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=theirs.id, quantity_offered=1))
    db.add(Share(user_id=other.id, showcase_id=sc.id, playgroup_id=pg.id))
    db.commit()

    opts = trade_service.get_construction_options(db, owner.id, other.id, pg.id)
    offered_row_ids = {i["inventory_row_id"] for i in opts["proposer_inventory"]}

    assert real.id in offered_row_ids
    assert deck_proxy.id in offered_row_ids, "an ordinary deck's proxy is still tradeable"
    assert placeholder.id not in offered_row_ids

    # And the service still refuses it, so the two agree rather than one covering
    # for the other.
    with pytest.raises(ValueError, match="brew placeholder"):
        trade_service.create_trade(
            db,
            proposer_user_id=owner.id,
            recipient_user_id=other.id,
            playgroup_id=pg.id,
            offered=[{"inventory_row_id": placeholder.id, "quantity": 1}],
            requested=[{"showcase_item_id": db.query(ShowcaseItem).first().id, "quantity": 1}],
        )
