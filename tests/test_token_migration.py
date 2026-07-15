"""#120 — token cards in the main collection: migration + import routing."""

import itertools

from app.import_service import persist_import_rows
from app.models import Card, InventoryRow, StorageLocation, TokenInventory, TransactionLog
from app.token_service import (
    find_migratable_token_rows,
    is_token_card,
    migrate_rows_to_token_inventory,
    upsert_token_from_card,
)

_seq = itertools.count(1)


def _card(
    s, name="Treasure", layout="token", set_type="token", type_line="Token Artifact — Treasure"
):
    c = Card(
        scryfall_id=f"tok-{next(_seq)}",
        name=name,
        set_code="ttst",
        set_name="Test Tokens",
        collector_number=str(next(_seq)),
        rarity="common",
        type_line=type_line,
        oracle_text="",
        image_url="http://x/tok.png",
        color_identity="",
        set_type=set_type,
        layout=layout,
    )
    s.add(c)
    s.flush()
    return c


def _row(s, user_id, card, qty=3, loc_id=None):
    r = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        finish="normal",
        quantity=qty,
        is_pending=loc_id is None,
        storage_location_id=loc_id,
    )
    s.add(r)
    s.flush()
    return r


def _loc(s, user_id, name="Drawer 3"):
    loc = StorageLocation(user_id=user_id, name=name, type="box", mode="managed")
    s.add(loc)
    s.flush()
    return loc


def test_is_token_card_predicate():
    assert is_token_card("token", None)
    assert is_token_card("double_faced_token", "expansion")
    assert is_token_card("emblem", None)
    assert is_token_card(None, "token")
    assert not is_token_card("normal", "expansion")
    assert not is_token_card(None, None)  # pre-backfill NULLs don't match


def test_upsert_creates_then_merges(db, user):
    card = _card(db)
    loc = _loc(db, user.id)

    tok = upsert_token_from_card(db, user_id=user.id, card=card, quantity=2)
    db.commit()
    assert tok.name == "Treasure"
    assert tok.subtype == "Treasure"  # derived from the type_line tail
    assert tok.quantity == 2
    assert tok.storage_location_id is None

    again = upsert_token_from_card(
        db, user_id=user.id, card=card, quantity=5, storage_location_id=loc.id
    )
    db.commit()
    assert again.id == tok.id  # merged, not duplicated
    assert again.quantity == 7
    assert again.storage_location_id == loc.id  # fills an unset location


def test_find_migratable_scoped_to_user_and_predicate(db, user):
    from app.models import User

    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.flush()

    token_card = _card(db)
    normal_card = _card(
        db, name="Sol Ring", layout="normal", set_type="expansion", type_line="Artifact"
    )
    _row(db, user.id, token_card)
    _row(db, user.id, normal_card)
    _row(db, other.id, token_card)
    db.commit()

    found = find_migratable_token_rows(db, user.id)
    assert len(found) == 1
    assert found[0].card_id == token_card.id
    assert found[0].user_id == user.id


def test_migrate_moves_selected_rows_only(db, user):
    card_a = _card(db, name="Treasure")
    card_b = _card(db, name="Pest", type_line="Token Creature — Pest")
    loc = _loc(db, user.id)
    row_a = _row(db, user.id, card_a, qty=4, loc_id=loc.id)
    row_b = _row(db, user.id, card_b, qty=2)
    db.commit()

    # per-row opt-out: only row_a approved; a foreign/stale id is skipped
    result = migrate_rows_to_token_inventory(db, user_id=user.id, row_ids=[row_a.id, 99999])
    assert result == {"moved_rows": 1, "moved_quantity": 4}

    tokens = db.query(TokenInventory).filter_by(user_id=user.id).all()
    assert len(tokens) == 1
    assert tokens[0].name == "Treasure"
    assert tokens[0].quantity == 4
    assert tokens[0].storage_location_id == loc.id

    remaining = db.query(InventoryRow).filter_by(user_id=user.id).all()
    assert [r.id for r in remaining] == [row_b.id]  # unapproved row untouched

    log = db.query(TransactionLog).filter_by(user_id=user.id, event_type="token_migration").one()
    assert log.quantity_delta == -4
    assert log.destination_location == "token_inventory"


def test_import_routes_tokens_to_token_inventory(db, user):
    token_card = _card(db)
    normal_card = _card(
        db, name="Sol Ring", layout="normal", set_type="expansion", type_line="Artifact"
    )

    rows = [
        {"line_number": 1, "scryfall_id": token_card.scryfall_id, "quantity": 6},
        {"line_number": 2, "scryfall_id": normal_card.scryfall_id, "quantity": 1},
    ]
    result = persist_import_rows(db, rows, user_id=user.id, filename="t.csv")

    assert result["token_routed_count"] == 1
    assert result["token_routed_quantity"] == 6
    assert result["imported_count"] == 1  # tokens reported separately
    assert result["failed_rows"] == []

    tok = db.query(TokenInventory).filter_by(user_id=user.id).one()
    assert tok.scryfall_id == token_card.scryfall_id
    assert tok.quantity == 6

    inv = db.query(InventoryRow).filter_by(user_id=user.id).all()
    assert len(inv) == 1  # only the non-token landed in inventory_rows
    assert inv[0].card_id == normal_card.id


def test_import_merges_into_existing_token(db, user):
    token_card = _card(db)
    upsert_token_from_card(db, user_id=user.id, card=token_card, quantity=2)
    db.commit()

    persist_import_rows(
        db,
        [{"line_number": 1, "scryfall_id": token_card.scryfall_id, "quantity": 3}],
        user_id=user.id,
    )
    tok = db.query(TokenInventory).filter_by(user_id=user.id).one()
    assert tok.quantity == 5


def test_migrate_routes(client, db, user):
    card = _card(db)
    row = _row(db, user.id, card, qty=2)
    db.commit()

    # preview lists the row, mutates nothing
    resp = client.get("/tokens/migrate")
    assert resp.status_code == 200
    assert "Treasure" in resp.text
    assert db.query(InventoryRow).filter_by(id=row.id).count() == 1

    # empty selection is a clean no-op
    resp = client.post("/tokens/migrate", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert "migrated=0" in resp.headers["location"]

    # approved selection moves the row
    resp = client.post("/tokens/migrate", data={"row_id": [str(row.id)]}, follow_redirects=False)
    assert resp.status_code == 303
    assert "migrated=1" in resp.headers["location"]
    assert db.query(InventoryRow).filter_by(id=row.id).count() == 0
    assert db.query(TokenInventory).filter_by(user_id=user.id).count() == 1

    # empty state after migration
    resp = client.get("/tokens/migrate")
    assert resp.status_code == 200
    assert "No token printings found" in resp.text
