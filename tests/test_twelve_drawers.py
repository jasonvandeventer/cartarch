"""A two-catalog setup preserves ownership and uses native placement end to end."""

from app.inventory_service import (
    confirm_pending_row,
    get_inventory_row_stats,
    resort_collection,
    route_intake_to_bulk,
    summarize_intake_routing,
)
from app.location_service import numbered_drawers
from app.models import Card, InventoryRow, SorterRule, StorageLocation, User
from app.sorter_rule_service import configure_twelve_drawers


def seed_row(
    db,
    user,
    name,
    code,
    *,
    quantity=1,
    type_line="Creature — Elf",
    price="0.10",
    location=None,
    collector="1",
    language="en",
    set_type="expansion",
    proxy=False,
):
    card = Card(
        scryfall_id=name,
        name=name,
        set_code=code,
        collector_number=collector,
        type_line=type_line,
        price_usd=price,
        set_type=set_type,
    )
    db.add(card)
    db.flush()
    row = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        quantity=quantity,
        finish="normal",
        is_proxy=proxy,
        language=language,
        is_pending=location is None,
        storage_location_id=location.id if location else None,
        drawer="1" if location and location.type == "drawer" else None,
        slot="1" if location and location.type == "drawer" else None,
    )
    db.add(row)
    db.commit()
    return row


def test_setup_preserves_existing_inventory_and_is_account_scoped(db, user, client):
    original = StorageLocation(user_id=user.id, name="Drawer 1", type="drawer", mode="managed")
    other = User(username="another-owner", password_hash="x")
    db.add_all([original, other])
    db.commit()
    other_drawer = StorageLocation(user_id=other.id, name="Drawer 1", type="drawer", mode="managed")
    db.add(other_drawer)
    db.commit()
    row = seed_row(db, user, "Stored", "woe", quantity=4, location=original)
    before = (row.id, row.quantity, row.storage_location_id, row.drawer, row.slot, row.is_pending)
    assert client.post("/drawers/setup-twelve", data={}, follow_redirects=False).status_code == 400
    assert len(numbered_drawers(db, user.id)) == 1
    assert (
        client.post(
            "/drawers/setup-twelve", data={"acknowledge": "true"}, follow_redirects=False
        ).status_code
        == 303
    )
    db.expire_all()
    assert (
        row.id,
        row.quantity,
        row.storage_location_id,
        row.drawer,
        row.slot,
        row.is_pending,
    ) == before
    assert numbered_drawers(db, user.id)["1"].id == original.id
    assert len(numbered_drawers(db, user.id)) == 12
    assert len(numbered_drawers(db, other.id)) == 1
    assert other_drawer.note is None
    rules = db.query(SorterRule).filter_by(user_id=user.id).count()
    assert client.post("/drawers/setup-twelve", data={"acknowledge": "true"}).status_code == 400
    assert db.query(SorterRule).filter_by(user_id=user.id).count() == rules
    page = client.get("/drawers").text
    assert "A1 · Numbers + A–B" in page and "B6 · Tokens &amp; proxies" in page
    assert "Value ($5+)" not in page
    assert page.index('/drawers/9"') < page.index('/drawers/10"') < page.index('/drawers/12"')
    empty = client.get("/drawers/12").text
    assert "B6 · Tokens &amp; proxies" in empty
    assert "/audit/start?location_id=" in empty


def test_twelve_drawer_sort_confirm_counts_and_audit(db, user, client):
    configure_twelve_drawers(db, user.id)
    db.commit()
    examples = [
        ("Numeric", "40k", "1", {}),
        ("Alpha", "acr", "1", {}),
        ("C", "cmm", "2", {}),
        ("D", "dft", "3", {}),
        ("E", "ecc", "3", {}),
        ("F", "fdn", "4", {}),
        ("I", "iko", "5", {}),
        ("M", "m3c", "6", {}),
        ("N", "neo", "7", {}),
        ("S", "sld", "8", {"price": "99"}),
        ("T", "tla", "9", {}),
        ("W", "woe", "10", {"quantity": 4}),
        ("Snow", "khm", "11", {"type_line": "Basic Snow Land — Forest", "price": "50"}),
        ("Token", "twar", "12", {"type_line": "Token Creature — Elf"}),
        ("Substitute", "smid", "12", {"type_line": "Card", "set_type": "token"}),
        ("Proxy", "fdn", "12", {"proxy": True}),
        ("Walker", "woe", "10", {"type_line": "Legendary Planeswalker — Jace"}),
        ("Foreign", "woe", "10", {"language": "ja", "collector": "2"}),
        ("Unknown", "", "1", {}),
    ]
    rows = [
        (seed_row(db, user, name, code, **kwargs), drawer)
        for name, code, drawer, kwargs in examples
    ]
    oversized = seed_row(db, user, "Plane", "hop", type_line="Plane — Mirrodin")
    quantities = {r.id: r.quantity for r, _ in rows}
    woe = next(r for r, _ in rows if r.card.name == "W")
    assert route_intake_to_bulk(db, user.id, [woe.id]) == (4, 0)
    assert summarize_intake_routing(
        db,
        user.id,
        [{"card_id": woe.card_id, "recommended_action": "import_new", "recommended_new_qty": 4}],
    ) == (4, 0)
    resort_collection(db, user.id)
    db.expire_all()
    for row, drawer in rows:
        assert row.drawer == drawer, row.card.name
        assert row.slot and row.storage_location_id == numbered_drawers(db, user.id)[drawer].id
        assert row.is_pending and row.quantity == quantities[row.id]
    assert oversized.drawer is None and oversized.storage_location.name == "Oversized cards"
    pending = client.get("/pending").text
    assert "B4 · W–Z" in pending and "Value ($5+)" not in pending
    for row, _ in rows:
        confirm_pending_row(db, row.id, user.id)
    confirm_pending_row(db, oversized.id, user.id)
    counts = get_inventory_row_stats(db, user.id)["drawer_counts"]
    assert counts["10"] == 6 and counts["11"] == 1 and counts["12"] == 3
    assert sum(counts.values()) == sum(quantities.values())
    page = client.get("/drawers/10").text
    assert "Foreign" in page
    assert f"/card/woe/{woe.card.collector_number}" in page
    assert "B4 · W–Z" in page
    assert client.get("/collection?drawer=10").status_code == 200
    audit = client.get(f"/audit/start?location_id={woe.storage_location_id}").text
    assert "woe" in audit.lower()
