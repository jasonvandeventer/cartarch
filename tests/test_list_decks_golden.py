"""Golden characterization of ``list_decks`` (#103 Phase C safety net).

Pins every annotation the Decks page reads — card_count, total_value,
color_identity, consistency, bracket/bracket_stale — across the tricky cases
(commander colors, proxies excluded from value, inbound variant shares counted,
empty deck, location-less deck) BEFORE the N+1 rewrite. If the rewrite changes
any of these, that's a bug, not a refactor.
"""

from __future__ import annotations

import itertools

import app.legacy_tables  # noqa: F401 — registers deck_bracket_* tables on Base.metadata
from app import deck_service
from app.models import Card, Deck, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _user(db) -> User:
    u = User(username=f"u{next(_seq)}@ex.com", password_hash="x")
    db.add(u)
    db.flush()
    return u


def _deck(db, user_id, name, group_id=None) -> Deck:
    loc = StorageLocation(user_id=user_id, name=name, type="deck", mode="managed")
    db.add(loc)
    db.flush()
    d = Deck(user_id=user_id, name=name, storage_location_id=loc.id, variant_group_id=group_id)
    db.add(d)
    db.flush()
    return d


def _card(db, name, *, ci="", price="2.00") -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="S",
        collector_number=str(next(_seq)),
        rarity="rare",
        type_line="Creature",
        oracle_text="x",
        color_identity=ci,
        set_type="expansion",
        price_usd=price,
    )
    db.add(c)
    db.flush()
    return c


def _row(db, user_id, card, loc_id, *, qty=1, role=None, proxy=False, finish="normal"):
    r = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=qty,
        finish=finish,
        storage_location_id=loc_id,
        role=role,
        is_proxy=proxy,
        is_pending=False,
    )
    db.add(r)
    db.flush()
    return r


def test_list_decks_golden(db):
    u = _user(db)

    # Deck A — commander (WU identity), a 4-of, and a proxy (value-excluded).
    a = _deck(db, u.id, "Alpha")
    _row(
        db,
        u.id,
        _card(db, "Cmdr", ci="W U", price="10.00"),
        a.storage_location_id,
        role="commander",
    )
    _row(db, u.id, _card(db, "Filler", price="1.50"), a.storage_location_id, qty=4)
    _row(db, u.id, _card(db, "Proxy Bomb", price="99.00"), a.storage_location_id, proxy=True)

    # Deck B — empty (has a location, no rows).
    _deck(db, u.id, "Beta")

    # Deck C — no storage location at all.
    c = Deck(user_id=u.id, name="Gamma", storage_location_id=None)
    db.add(c)
    db.flush()

    # Variant pair — E shares a card INTO F (F's count includes it; value doesn't).
    g = deck_service.create_variant_group(db, u.id, "Group")
    e = _deck(db, u.id, "Echo", group_id=g.id)
    f = _deck(db, u.id, "Foxtrot", group_id=g.id)
    shared_row = _row(db, u.id, _card(db, "Shared Staple", price="5.00"), e.storage_location_id)
    _row(db, u.id, _card(db, "F Own", price="3.00"), f.storage_location_id, qty=2)
    deck_service.share_card_to_deck(db, u.id, inventory_row_id=shared_row.id, target_deck_id=f.id)
    db.commit()

    by_name = {d.name: d for d in deck_service.list_decks(db, u.id)}
    assert set(by_name) == {"Alpha", "Beta", "Gamma", "Echo", "Foxtrot"}

    A = by_name["Alpha"]
    assert A.card_count == 6  # 1 + 4 + 1 (proxy counts as a card)
    assert A.total_value == 10.00 + 4 * 1.50  # proxy excluded
    assert A.color_identity == "W U"
    assert A.consistency is not None
    assert A.bracket is None and A.bracket_stale is True  # no estimate yet

    B = by_name["Beta"]
    assert B.card_count == 0
    assert B.total_value == 0.0
    assert B.color_identity == ""
    assert B.consistency is None

    C = by_name["Gamma"]
    assert C.card_count == 0 and C.total_value == 0.0
    assert C.bracket is None and C.bracket_stale is False

    E = by_name["Echo"]
    assert E.card_count == 1
    assert E.total_value == 5.00

    F = by_name["Foxtrot"]
    assert F.card_count == 3  # 2 own + 1 shared in
    assert F.total_value == 2 * 3.00  # shared copy NOT double-counted


def test_list_decks_query_count_is_flat(db):
    """The #103 Phase C contract: queries must NOT grow with (non-variant) deck
    count. Fixed set: decks + rows + estimates + fingerprints (+ nothing per
    deck). Variant decks may add per-deck share queries — none here."""
    from sqlalchemy import event

    u = _user(db)
    for i in range(6):
        d = _deck(db, u.id, f"D{i}")
        _row(db, u.id, _card(db, f"C{i}"), d.storage_location_id)
    db.commit()

    counted = []
    engine = db.get_bind()

    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counted.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        deck_service.list_decks(db, u.id)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    # 4 fixed queries (decks, estimates, fingerprints, batched rows); allow a
    # little SQLAlchemy slack but fail LOUDLY if per-deck queries return
    # (6 decks × 3-4 queries would be ~20+).
    assert len(counted) <= 6, f"{len(counted)} SELECTs — N+1 is back:\n" + "\n".join(counted)
