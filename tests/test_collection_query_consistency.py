"""Collection queries must agree with canonical placement and displayed value."""

import pytest

from app import deck_service
from app.dashboard_service import get_dashboard_data
from app.inventory_service import build_collection_filter_query, get_collection_facet_counts
from app.models import Card, InventoryRow, StorageLocation


@pytest.mark.parametrize("check", ["drawer", "price_search", "price_facet", "counts"])
def test_collection_query_consistency(client, db, user, check):
    brew = deck_service.create_deck(db, user.id, "Query brew", is_brew=True)
    drawer = StorageLocation(user_id=user.id, name="Drawer 2", type="drawer", mode="manual")
    binder = StorageLocation(user_id=user.id, name="Binder", type="binder", mode="manual")
    db.add_all([drawer, binder])
    db.flush()
    rows = []
    for name, loc, proxy, legacy in [
        ("Filed card", drawer, False, None),
        ("Physical proxy", binder, True, "2"),
        ("Brew placeholder", db.get(StorageLocation, brew.storage_location_id), True, None),
    ]:
        card = Card(
            scryfall_id=name,
            name=name,
            set_code="tst",
            collector_number="1",
            price_usd="10",
            type_line="Creature",
            color_identity="G",
        )
        db.add(card)
        db.flush()
        row = InventoryRow(
            user_id=user.id,
            card_id=card.id,
            storage_location_id=loc.id,
            drawer=legacy,
            is_proxy=proxy,
            quantity=1,
            is_pending=False,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    if check == "counts":
        counts = get_collection_facet_counts(db, user.id)
        assert counts["total"] == 2
        assert counts["types"]["Creature"] == 2
        assert counts["status"]["in_deck"] == 0
    else:
        filters, expected = {
            "drawer": ({"search": "drawer:2"}, rows[0].id),
            "price_search": ({"search": "price:>=5"}, rows[0].id),
            "price_facet": ({"facet_price_max": 0}, rows[1].id),
        }[check]
        assert {r.id for r in build_collection_filter_query(db, user.id, **filters)} == {expected}
        if check == "drawer":
            page = client.get("/collection", params={"search": "drawer:2"})
            assert page.status_code == 200
            assert "Filed card" in page.text and "Physical proxy" not in page.text


@pytest.mark.parametrize("foil_price", ["", "0.00", None])
def test_empty_foil_price_uses_normal_price_in_dashboard(db, user, foil_price):
    card = Card(
        scryfall_id="empty-price",
        name="Empty foil",
        set_code="tst",
        collector_number="1",
        price_usd="10",
        price_usd_foil=foil_price,
    )
    db.add(card)
    db.flush()
    db.add(
        InventoryRow(user_id=user.id, card_id=card.id, quantity=2, finish="foil", is_pending=False)
    )
    db.commit()
    assert get_dashboard_data(db, user.id)["holdings"]["placed_value"] == 20
