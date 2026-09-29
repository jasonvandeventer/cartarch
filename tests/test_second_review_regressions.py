from datetime import UTC, datetime

import pytest

from app.dashboard_service import get_dashboard_data
from app.drawer_service import list_drawer_groups, list_rows_for_drawer
from app.inventory_service import get_inventory_row_stats
from app.location_service import get_location_summary
from app.models import Card, ImportBatch, InventoryRow, StorageLocation, TransactionLog
from app.presentation_service import build_pending_batch_groups


def card(db):
    c = Card(
        scryfall_id="second-review",
        name="Review Visibility Card",
        set_code="tst",
        collector_number="1",
        price_usd="10.00",
    )
    db.add(c)
    db.commit()
    return c


def test_real_import_keeps_batch_label(client, db, user):
    c = card(db)
    r = client.post(
        "/import/commit",
        data=dict(
            line_number="1",
            name=c.name,
            scryfall_id=c.scryfall_id,
            set_code="tst",
            collector_number="1",
            quantity="1",
            finish="normal",
            location="",
            filename="review.csv",
        ),
    )
    assert r.status_code == 200
    row = db.query(InventoryRow).filter_by(user_id=user.id).one()
    log = db.query(TransactionLog).filter_by(inventory_row_id=row.id, event_type="import").one()
    assert log.batch_id is not None
    groups = build_pending_batch_groups(db, user.id, [{"id": row.id}])
    assert groups[0]["source"] == "CSV — review.csv"
    page = client.get("/pending")
    assert page.status_code == 200 and "CSV — review.csv" in page.text


def test_explicit_drawer_import_is_visible(client, db, user):
    c = card(db)
    loc = StorageLocation(user_id=user.id, name="Drawer 2", type="drawer", mode="managed")
    db.add(loc)
    db.commit()
    r = client.post(
        "/import/manual/commit",
        data=dict(scryfall_id=c.scryfall_id, quantity=1, target_location_id=loc.id),
    )
    assert r.status_code == 200
    row = db.query(InventoryRow).filter_by(user_id=user.id).one()
    assert row.storage_location_id == loc.id and row.drawer is None
    assert len(list_drawer_groups(db, user.id)["2"]) == 1
    assert [r.id for r in list_rows_for_drawer(db, "2", user.id)] == [row.id]
    page = client.get("/drawers/2")
    assert page.status_code == 200 and c.name in page.text
    assert c.name in client.get(f"/locations/{loc.id}").text


@pytest.mark.parametrize("kind", ["binder", "drawer", "deck"])
def test_proxy_values_agree_across_surfaces(client, db, user, kind):
    c = card(db)
    loc = StorageLocation(
        user_id=user.id,
        name="Drawer 2" if kind == "drawer" else "Value location",
        type=kind,
        mode="manual",
    )
    db.add(loc)
    db.flush()
    urls = ["/collection", f"/locations/{loc.id}", f"/cards/{c.id}"]
    if kind == "drawer":
        urls.extend(["/drawers", "/drawers/2"])
    elif kind == "deck":
        from app.models import Deck

        deck = Deck(user_id=user.id, name="Valuation deck", storage_location_id=loc.id)
        db.add(deck)
        db.flush()
        urls.extend(["/decks", f"/decks/{deck.id}"])
    for proxy, quantity in [(False, 1), (True, 3)]:
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=c.id,
                storage_location_id=loc.id,
                is_proxy=proxy,
                quantity=quantity,
                finish="normal",
                is_pending=False,
            )
        )
    db.commit()
    assert get_dashboard_data(db, user.id)["holdings"]["placed_value"] == 10
    assert get_inventory_row_stats(db, user.id)["total_value"] == 10
    assert get_location_summary(db, user.id)[0]["total_value"] == 10

    for url in urls:
        page = client.get(url)
        assert page.status_code == 200
        assert "$40.00" not in page.text
        assert "$10.00" in page.text


def test_batch_uses_latest_timestamp(db, user):
    c = card(db)
    row = InventoryRow(user_id=user.id, card_id=c.id, quantity=1, is_pending=True)
    batches = [
        ImportBatch(user_id=user.id, filename=f"{name}.csv", row_count=1) for name in ("new", "old")
    ]
    db.add_all([row, *batches])
    db.flush()
    for batch, day in zip(batches, [20, 10], strict=True):
        db.add(
            TransactionLog(
                user_id=user.id,
                inventory_row_id=row.id,
                batch_id=batch.id,
                event_type="imported",
                created_at=datetime(2026, 9, day, tzinfo=UTC),
                quantity_delta=1,
            )
        )
        db.flush()
    db.commit()
    group = build_pending_batch_groups(db, user.id, [{"id": row.id}])[0]
    assert group["note"] == "new.csv"


def test_drawer_ignores_stale_legacy_field_and_other_owners(db, user):
    from app.models import User

    c = card(db)
    other = User(username="other-drawer@example.com", password_hash="x")
    db.add(other)
    db.flush()
    drawer = StorageLocation(user_id=user.id, name="Drawer 2", type="drawer", mode="managed")
    binder = StorageLocation(user_id=user.id, name="Binder", type="binder", mode="manual")
    foreign = StorageLocation(user_id=other.id, name="Drawer 2", type="drawer", mode="managed")
    db.add_all([drawer, binder, foreign])
    db.flush()
    for uid, loc in [(user.id, binder), (other.id, foreign)]:
        db.add(
            InventoryRow(
                user_id=uid,
                card_id=c.id,
                quantity=1,
                storage_location_id=loc.id,
                drawer="2",
                is_pending=False,
            )
        )
    db.commit()
    assert list_rows_for_drawer(db, "2", user.id) == []
    assert list_rows_for_drawer(db, "99", user.id) == []


def test_batch_timestamp_tie_uses_id_and_stays_owner_scoped(db, user):
    from app.models import User

    c = card(db)
    row = InventoryRow(user_id=user.id, card_id=c.id, quantity=1, is_pending=True)
    other = User(username="other-batch@example.com", password_hash="x")
    db.add_all([row, other])
    db.flush()
    timestamp = datetime(2026, 9, 20, tzinfo=UTC)
    for owner, filename, kind in [
        (user.id, "older.csv", "imported"),
        (user.id, "newer.csv", "import"),
        (other.id, "private.csv", "import"),
    ]:
        batch = ImportBatch(user_id=owner, filename=filename, row_count=1)
        db.add(batch)
        db.flush()
        db.add(
            TransactionLog(
                user_id=owner,
                inventory_row_id=row.id,
                batch_id=batch.id,
                event_type=kind,
                created_at=timestamp,
                quantity_delta=1,
            )
        )
        db.flush()
    db.commit()
    assert build_pending_batch_groups(db, user.id, [{"id": row.id}])[0]["note"] == "newer.csv"
