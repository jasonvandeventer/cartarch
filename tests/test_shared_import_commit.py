"""Both import forms retain placement and response semantics after consolidation."""

import pytest

from app.models import Card, ImportBatch, InventoryRow, StorageLocation
from app.routes import imports


@pytest.mark.parametrize("path", ["/import/commit", "/import/manual/commit"])
@pytest.mark.parametrize("reconcile", [False, True])
@pytest.mark.parametrize("explicit_destination", [False, True])
def test_import_destination_and_sort_response(
    client, db, user, monkeypatch, path, reconcile, explicit_destination
):
    card = Card(scryfall_id="shared-import", name="Forest", set_code="tst", collector_number="1")
    loc = StorageLocation(user_id=user.id, name="Keep here", type="binder", mode="manual")
    db.add_all([card, loc])
    db.commit()
    calls = []
    monkeypatch.setattr(imports, "has_sortable_setup", lambda *_: True)
    monkeypatch.setattr(imports, "route_intake_to_bulk", lambda *args: calls.append("bulk"))
    monkeypatch.setattr(imports, "resort_collection", lambda *args, **kwargs: calls.append("sort"))
    data = {
        "scryfall_id": card.scryfall_id,
        "name": card.name,
        "quantity": 2,
        "line_number": 1,
        "set_code": "tst",
        "collector_number": "1",
        "finish": "normal",
        "location": "",
        "filename": "shared.csv",
        "target_location_id": loc.id if explicit_destination else 0,
    }
    if reconcile:
        data.update(reconcile_action="import_new", reconcile_new_qty="2")
    response = client.post(path, data=data, follow_redirects=False)
    row = db.query(InventoryRow).filter_by(user_id=user.id, card_id=card.id).one()
    assert row.quantity == 2
    assert row.storage_location_id == (loc.id if explicit_destination else None)
    assert calls == ([] if explicit_destination else ["bulk", "sort"])
    batch = db.query(ImportBatch).filter_by(user_id=user.id).one()
    assert batch.filename == ("shared.csv" if path == "/import/commit" else "manual import")
    if not explicit_destination and path == "/import/commit":
        assert response.status_code == 303
        assert response.headers["location"] == "/pending"
    else:
        assert response.status_code == 200
        assert "Import Results" in response.text
        if explicit_destination:
            assert loc.name in response.text
