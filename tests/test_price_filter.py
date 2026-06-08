"""Proof-of-life route test on the new pytest fixtures (v3.37.0).

Exercises the search path end-to-end through the TestClient, pinning the
v3.36.5 NULLIF price-CAST behaviour: ``price:`` filters never crash on blank /
NULL prices, and blank/NULL rows are excluded (not coerced to 0.0).

invariant: architecture.md → request-path price filter (NULLIF guard, v3.36.5)
"""

from __future__ import annotations

from app.models import Card, InventoryRow, StorageLocation


def _placed_card(db, user, name, price):
    """Seed one Card at ``price`` (str / "" / None) with a placed InventoryRow."""
    card = Card(
        scryfall_id=f"sid-{name}",
        name=name,
        set_code="tst",
        collector_number=name[-1],
        price_usd=price,
    )
    db.add(card)
    db.flush()
    db.add(
        InventoryRow(
            card_id=card.id,
            user_id=user.id,
            quantity=1,
            finish="normal",
            is_pending=False,
            storage_location_id=None,
        )
    )
    db.commit()


def test_price_filter_handles_blank_and_null(client, db, user):
    loc = StorageLocation(user_id=user.id, name="Binder", type="binder", mode="managed")
    db.add(loc)
    db.commit()
    _placed_card(db, user, "Priced10", "10.00")
    _placed_card(db, user, "BlankPrice", "")  # NULLIF → NULL, excluded (not 0.0)
    _placed_card(db, user, "NullPrice", None)  # NULL, excluded

    # price:>=5 → only the $10 card; blank/NULL excluded, no crash.
    r = client.get("/collection?search=price:>=5")
    assert r.status_code == 200, r.status_code
    assert "Priced10" in r.text
    assert "BlankPrice" not in r.text
    assert "NullPrice" not in r.text

    # price:<5 → none (the $10 card is too expensive; blank/NULL excluded).
    r = client.get("/collection?search=price:<5")
    assert r.status_code == 200, r.status_code
    assert "Priced10" not in r.text

    # price:>=0 → the guard's sharp edge: must NOT crash and must NOT pull the
    # blank/NULL rows in as 0.0.
    r = client.get("/collection?search=price:>=0")
    assert r.status_code == 200, r.status_code
    assert "Priced10" in r.text
    assert "BlankPrice" not in r.text
    assert "NullPrice" not in r.text
