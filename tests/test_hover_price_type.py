"""Hover-attr prices must come from a FLOAT, never `Card.price_usd`.

`cards.price_usd*` are TEXT columns (String(32)), so `'%.2f'|format(...)` over
one raises TypeError and 500s the page. v4.13.4 wired that raw column into the
hover `data-card-info` on /pending and /drawers/{n}; reported live 2026-08-06
("Internal Server Error every time I click Review now"). Both surfaces already
carry a resolved float on the item dict (`price` / `effective_price`).
"""

import app.legacy_tables  # noqa
from app.models import Card, InventoryRow, StorageLocation


def _card(db, sid):
    card = Card(
        name="Priced",
        scryfall_id=sid,
        set_code="tst",
        set_name="T",
        collector_number="1",
        rarity="rare",
        image_url="http://x/i.png",
        price_usd="3.50",  # TEXT, exactly as MTGJSON ingest writes it
    )
    db.add(card)
    db.flush()
    return card


def test_pending_page_renders_a_priced_card(client, db, user):
    row = InventoryRow(
        user_id=user.id,
        card_id=_card(db, "sf-pending-price").id,
        quantity=1,
        finish="normal",
        is_pending=True,
    )
    db.add(row)
    db.commit()

    resp = client.get("/pending")
    assert resp.status_code == 200
    assert "$3.50" in resp.text


def test_drawer_page_renders_a_priced_card(client, db, user):
    db.add(StorageLocation(user_id=user.id, name="Drawer 1", type="drawer"))
    row = InventoryRow(
        user_id=user.id,
        card_id=_card(db, "sf-drawer-price").id,
        quantity=1,
        finish="normal",
        is_pending=False,
        drawer="1",
        slot="1",
    )
    db.add(row)
    db.commit()

    resp = client.get("/drawers/1")
    assert resp.status_code == 200
    assert "$3.50" in resp.text
