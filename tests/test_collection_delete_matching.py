"""Filter-scoped "Delete matching" Collection action (Stage 2).

Covers: blast-radius counts (decks / showcases / shared showcases + card sum),
typed-confirmation required for an UNFILTERED whole-collection delete, filtered
delete leaves non-matching rows untouched, and the FK-safe primitive
(`bulk_delete_inventory_rows`) cleans showcase/trade references under enforcement.
"""

from __future__ import annotations

import itertools

from app.inventory_service import bulk_delete_inventory_rows
from app.models import (
    Card,
    InventoryRow,
    Playgroup,
    Share,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    Trade,
    TradeItem,
    User,
)
from app.routes.collections import _delete_blast_radius, _is_unfiltered

_seq = itertools.count(1)


def _card(s):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=f"C{next(_seq)}",
        set_code="TST",
        collector_number=str(next(_seq)),
        price_usd="2.00",
    )
    s.add(c)
    s.flush()
    return c


def _loc(s, uid, name, typ="box"):
    loc = StorageLocation(user_id=uid, name=name, type=typ, mode="manual", sort_order=0)
    s.add(loc)
    s.flush()
    return loc


def _row(s, uid, loc_id, qty=1, finish="normal"):
    r = InventoryRow(
        card_id=_card(s).id,
        user_id=uid,
        quantity=qty,
        finish=finish,
        is_pending=False,
        is_proxy=False,
        storage_location_id=loc_id,
    )
    s.add(r)
    s.flush()
    return r


# ── blast radius ────────────────────────────────────────────────


def test_blast_radius_counts(db, user):
    box = _loc(db, user.id, "Box")
    deck = _loc(db, user.id, "MyDeck", typ="deck")
    r_plain = _row(db, user.id, box.id, qty=2)  # card_count contributes 2
    r_deck = _row(db, user.id, deck.id)  # in a deck
    r_sc = _row(db, user.id, box.id)  # in a showcase (not shared)
    r_shared = _row(db, user.id, box.id)  # in a SHARED showcase
    sc = Showcase(user_id=user.id, name="SC")
    sc2 = Showcase(user_id=user.id, name="SC2")
    db.add_all([sc, sc2])
    db.flush()
    db.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=r_sc.id, quantity_offered=1))
    db.add(ShowcaseItem(showcase_id=sc2.id, inventory_row_id=r_shared.id, quantity_offered=1))
    pg = Playgroup(name="PG", created_by=user.id, join_code="abc")
    db.add(pg)
    db.flush()
    db.add(Share(user_id=user.id, showcase_id=sc2.id, playgroup_id=pg.id))  # sc2 is shared
    db.commit()

    ids = [r_plain.id, r_deck.id, r_sc.id, r_shared.id]
    blast = _delete_blast_radius(db, user.id, ids)
    assert blast["card_count"] == 5  # 2 + 1 + 1 + 1
    assert blast["rows_in_decks"] == 1
    assert blast["rows_in_showcases"] == 2  # r_sc + r_shared
    assert blast["rows_in_shared_showcases"] == 1  # only r_shared


def test_is_unfiltered():
    assert _is_unfiltered(
        {
            "search": "",
            "colors": "",
            "types": "",
            "status": "",
            "finishes": "",
            "price_min": "",
            "price_max": "",
            "finish": "",
            "location_id": 0,
        }
    )
    assert not _is_unfiltered({"search": "sol ring", "location_id": 0})
    assert not _is_unfiltered({"finish": "foil", "location_id": 0})
    assert not _is_unfiltered({"location_id": 5})


# ── routes (HTTP) ───────────────────────────────────────────────


def test_unfiltered_delete_requires_typed_confirmation(client, db, user):
    box = _loc(db, user.id, "Box")
    _row(db, user.id, box.id)
    _row(db, user.id, box.id)
    db.commit()

    # No confirm_text on an unfiltered delete → rejected, nothing deleted.
    r = client.post("/collection/delete-matching", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert "bulk=error" in r.headers["location"] and "confirm_required" in r.headers["location"]
    assert db.query(InventoryRow).filter(InventoryRow.user_id == user.id).count() == 2

    # With the typed confirmation → whole collection deleted.
    r2 = client.post(
        "/collection/delete-matching", data={"confirm_text": "DELETE"}, follow_redirects=False
    )
    assert r2.status_code == 303
    assert "bulk=deleted" in r2.headers["location"]
    db.expire_all()
    assert db.query(InventoryRow).filter(InventoryRow.user_id == user.id).count() == 0


def test_filtered_delete_leaves_non_matching_rows(client, db, user):
    box = _loc(db, user.id, "Box")
    foil = _row(db, user.id, box.id, finish="foil")
    normal = _row(db, user.id, box.id, finish="normal")
    db.commit()
    foil_id, normal_id = foil.id, normal.id

    # Filtered delete (finish=foil) — NOT unfiltered, so no typed confirm needed.
    r = client.post("/collection/delete-matching", data={"finish": "foil"}, follow_redirects=False)
    assert r.status_code == 303
    assert "bulk=deleted" in r.headers["location"]
    db.expire_all()
    assert db.query(InventoryRow).filter(InventoryRow.id == foil_id).first() is None
    assert db.query(InventoryRow).filter(InventoryRow.id == normal_id).first() is not None


# ── FK-safe primitive under enforcement ─────────────────────────


def test_bulk_delete_cleans_references_fk_on(fk_db):
    s = fk_db
    u = User(username="o@example.com", password_hash="x")
    other = User(username="o2@example.com", password_hash="x")
    s.add_all([u, other])
    s.flush()
    box = _loc(s, u.id, "Box")
    row = _row(s, u.id, box.id)
    sc = Showcase(user_id=u.id, name="SC")
    s.add(sc)
    s.flush()
    si = ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1)
    s.add(si)
    t = Trade(proposer_user_id=u.id, recipient_user_id=other.id, status="proposed")
    s.add(t)
    s.flush()
    ti = TradeItem(
        trade_id=t.id,
        side="offered",
        inventory_row_id=row.id,
        card_id=row.card_id,
        finish="normal",
        quantity=1,
    )
    s.add(ti)
    s.commit()
    si_id, ti_id, row_id = si.id, ti.id, row.id

    # The Delete-matching primitive — must NOT FK-error under enforcement.
    deleted = bulk_delete_inventory_rows(s, row_ids=[row_id], user_id=u.id)
    s.expire_all()
    assert deleted == 1
    assert s.query(InventoryRow).filter(InventoryRow.id == row_id).first() is None
    assert s.query(ShowcaseItem).filter(ShowcaseItem.id == si_id).first() is None
    ti_after = s.query(TradeItem).filter(TradeItem.id == ti_id).first()
    assert ti_after is not None and ti_after.inventory_row_id is None
