"""Assembly is saved inventory movement, never a second ledger of checked boxes."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import deck_service as ds
from app.models import Card, Deck, InventoryRow, StorageLocation, TransactionLog, User


@pytest.fixture
def assembly(db, user):
    box = StorageLocation(user_id=user.id, name="Binder with alternate printings", type="binder")
    loc = StorageLocation(user_id=user.id, name="Assembly brew", type="deck")
    db.add_all([box, loc])
    db.flush()
    deck = Deck(user_id=user.id, name="Assembly brew", storage_location_id=loc.id, is_brew=True)
    cards = [
        Card(
            scryfall_id=f"assembly-{i}",
            name="Gaea's Cradle",
            set_code=code,
            collector_number=str(i),
            type_line="Land",
        )
        for i, code in enumerate(["usg", "ema"])
    ]
    db.add_all([deck, *cards])
    db.flush()
    proxy = InventoryRow(
        user_id=user.id,
        card_id=cards[0].id,
        storage_location_id=loc.id,
        quantity=4,
        is_proxy=True,
        is_pending=False,
        role="commander",
    )
    source = InventoryRow(
        user_id=user.id,
        card_id=cards[1].id,
        storage_location_id=box.id,
        quantity=3,
        is_pending=False,
        language="ja",
        finish="foil",
        slot="10",
        notes="Signed",
        tags="Favorite",
    )
    db.add_all([proxy, source])
    db.commit()
    return SimpleNamespace(deck=deck, proxy=proxy, source=source, box=box, cards=cards)


def payload(a, quantity=1):
    return {
        "proxy_id": a.proxy.id,
        "source": f"{a.source.id}:{ds.assembly_row_version(a.source)}",
        "quantity": quantity,
        "proxy_version": ds.assembly_row_version(a.proxy),
    }


def test_page_is_read_only_and_resume_rejects_replayed_partial_pull(client, db, assembly):
    a = assembly
    url = f"/decks/{a.deck.id}/assemble"
    page = client.get(url)
    assert page.status_code == 200
    assert "3 ready to pull" in page.text and "1 unavailable" in page.text
    assert "Slot 10" in page.text and "Copy to pull" in page.text
    assert payload(a)["proxy_version"] in page.text
    assert db.query(TransactionLog).count() == 0
    data = payload(a)
    response = client.post(url, data=data)
    assert response.status_code == 200 and "Pull confirmed" in response.text
    assert "1 in deck" in response.text and "2 ready to pull" in response.text
    replay = client.post(url, data=data)
    assert "That pull could not be confirmed" in replay.text
    db.expire_all()
    assert a.proxy.quantity == 3 and a.source.quantity == 2
    real = (
        db.query(InventoryRow)
        .filter_by(storage_location_id=a.deck.storage_location_id, is_proxy=False)
        .one()
    )
    assert (real.card_id, real.finish, real.language, real.quantity) == (
        a.cards[1].id,
        "foil",
        "ja",
        1,
    )
    assert (real.role, real.notes, real.tags) == ("commander", "Signed", "Favorite")
    assert db.query(TransactionLog).one().source_location == a.box.name
    # Resume using fresh state; the remaining missing copy stays a placeholder.
    assert "Pull confirmed" in client.post(url, data=payload(a, 2)).text
    db.expire_all()
    assert a.proxy.quantity == 1 and a.deck.is_brew
    assert db.query(InventoryRow).filter_by(is_proxy=False).one().quantity == 3
    if directory := os.getenv("ASSEMBLY_BROWSER_FIXTURES"):
        Path(directory, "assembly.html").write_text(page.text)
        Path(directory, "assembly-resumed.html").write_text(client.get(url).text)


@pytest.mark.parametrize(
    "change",
    ["location", "quantity", "proxy", "name", "owner", "considering", "deck", "zero", "too_many"],
)
def test_stale_or_invalid_pull_does_not_move_anything(client, db, user, assembly, change):
    a = assembly
    data = payload(a)
    if change in {"location", "considering", "deck"}:
        loc = StorageLocation(
            user_id=user.id, name="Moved elsewhere", type=change if change != "location" else "box"
        )
        db.add(loc)
        db.flush()
        a.source.storage_location_id = loc.id
    elif change == "quantity":
        a.source.quantity = 2
    elif change == "proxy":
        a.source.is_proxy = True
    elif change == "name":
        a.cards[1].name = "Forest"
    elif change == "owner":
        other = User(username="other-assembly", password_hash="x")
        db.add(other)
        db.flush()
        a.source.user_id = other.id
    else:
        data["quantity"] = 0 if change == "zero" else 9
    db.commit()
    # Refresh tokens for prohibited source types: prove the lifecycle guard,
    # rather than only proving that a stale token was rejected.
    if change in {"considering", "deck", "proxy", "owner"}:
        data = payload(a)
    before = [
        (r.id, r.quantity, r.storage_location_id)
        for r in db.query(InventoryRow).order_by(InventoryRow.id)
    ]
    result = client.post(f"/decks/{a.deck.id}/assemble", data=data)
    assert "That pull could not be confirmed" in result.text
    db.expire_all()
    assert before == [
        (r.id, r.quantity, r.storage_location_id)
        for r in db.query(InventoryRow).order_by(InventoryRow.id)
    ]
    assert db.query(TransactionLog).count() == 0


def test_plan_reserves_copies_once_and_sorts_numeric_slots(db, user, assembly):
    a = assembly
    second = InventoryRow(
        user_id=user.id,
        card_id=a.cards[0].id,
        storage_location_id=a.deck.storage_location_id,
        quantity=2,
        is_proxy=True,
        is_pending=False,
    )
    early = InventoryRow(
        user_id=user.id,
        card_id=a.cards[1].id,
        storage_location_id=a.box.id,
        quantity=1,
        is_pending=False,
        slot="2",
    )
    other_loc = StorageLocation(user_id=user.id, name="Another deck", type="deck")
    db.add(other_loc)
    db.flush()
    committed = InventoryRow(
        user_id=user.id, card_id=a.cards[0].id, storage_location_id=other_loc.id, quantity=5
    )
    db.add_all([second, early, committed])
    db.commit()
    plan = ds.build_deck_assembly(db, a.deck)
    assert (plan["ready"], plan["remaining"]) == (4, 6)
    entries = plan["groups"][0][1]
    assert [e["source"]["row"].slot for e in entries] == ["2", "10"]
    assert sum(e["quantity"] for e in entries) == 4
    assert plan["unavailable"] == [
        {"name": "Gaea's Cradle", "quantity": 2, "committed": ["Deck · Another deck"]}
    ]


@pytest.mark.parametrize("bulk", [False, True])
def test_completion_preserves_language_and_never_takes_considering(
    db, user, client, assembly, bulk
):
    a = assembly
    a.proxy.quantity = 3
    english = InventoryRow(
        user_id=user.id,
        card_id=a.cards[1].id,
        storage_location_id=a.deck.storage_location_id,
        quantity=1,
        finish="foil",
        language="en",
        is_pending=False,
    )
    considering = StorageLocation(user_id=user.id, name="Considering", type="considering")
    db.add(considering)
    db.flush()
    reserved = InventoryRow(
        user_id=user.id, card_id=a.cards[0].id, storage_location_id=considering.id, quantity=10
    )
    db.add_all([english, reserved])
    db.commit()
    if bulk:
        assert ds.materialize_brew(db, user.id, a.deck.id) == {"claimed": 3, "remaining_proxies": 0}
    else:
        result = client.post(f"/decks/{a.deck.id}/assemble", data=payload(a, 3))
        assert "No placeholders left to fill" in result.text
    db.expire_all()
    assert not a.deck.is_brew and reserved.quantity == 10
    rows = db.query(InventoryRow).filter_by(storage_location_id=a.deck.storage_location_id).all()
    assert sorted((r.language, r.quantity) for r in rows) == [("en", 1), ("ja", 3)]
    assert next(r for r in rows if r.language == "ja").role == "commander"


def test_owner_retirement_and_physical_proxy_boundaries(client, db, user, assembly):
    a = assembly
    url = f"/decks/{a.deck.id}/assemble"
    a.deck.is_brew = False
    db.commit()
    page = client.get(url)
    assert "physical proxy" in page.text and "Confirm pull" not in page.text
    assert "That pull could not be confirmed" in client.post(url, data=payload(a)).text
    other = User(username="assembly-private", password_hash="x")
    db.add(other)
    db.flush()
    a.deck.user_id = other.id
    db.commit()
    assert client.get(url).status_code == 404
    assert client.post(url, data=payload(a)).status_code == 404
    a.deck.user_id = user.id
    from app.timeutil import utc_now

    a.deck.retired_at = utc_now()
    db.commit()
    assert client.get(url).status_code == 404
    assert client.post(url, data=payload(a)).status_code == 404


def test_import_planning_leaves_inventory_in_place_and_links_checklist(client, db, assembly):
    a = assembly
    form = {
        "target_location_id": a.deck.storage_location_id,
        "filename": "assembly-list",
        "line_number": "1",
        "scryfall_id": a.cards[1].scryfall_id,
        "name": a.cards[1].name,
        "set_code": "ema",
        "collector_number": "1",
        "finish": "foil",
        "quantity": "2",
        "location": "",
        "role": "commander",
        "reconcile_action": "plan_assembly",
        "reconcile_move_qty": "2",
        "reconcile_new_qty": "0",
    }
    preview = client.post("/import/reconcile-preview", data=form)
    assert preview.status_code == 200
    assert "Plan all for assembly" in preview.text and 'value="plan_assembly"' in preview.text
    result = client.post("/import/commit", data=form)
    assert result.status_code == 200 and "Open assembly checklist" in result.text
    assert f"/decks/{a.deck.id}/assemble" in result.text
    db.expire_all()
    assert a.source.quantity == 3 and a.source.storage_location_id == a.box.id
    planned = (
        db.query(InventoryRow)
        .filter_by(storage_location_id=a.deck.storage_location_id, card_id=a.cards[1].id)
        .one()
    )
    assert planned.is_proxy and planned.quantity == 2 and planned.role == "commander"
    a.deck.is_brew = False
    db.commit()
    assert client.post("/import/commit", data=form).status_code == 400
    db.expire_all()
    assert planned.quantity == 2 and a.source.quantity == 3


def test_concurrent_decks_cannot_claim_the_same_copy(db, user, assembly):
    if db.bind.dialect.name != "postgresql":
        pytest.skip("Row-lock race requires PostgreSQL")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy.orm import Session

    a = assembly
    a.source.quantity = 1
    loc = StorageLocation(user_id=user.id, name="Competing brew", type="deck")
    db.add(loc)
    db.flush()
    deck = Deck(user_id=user.id, name="Competing brew", storage_location_id=loc.id, is_brew=True)
    proxy = InventoryRow(
        user_id=user.id,
        card_id=a.cards[0].id,
        storage_location_id=loc.id,
        quantity=1,
        is_proxy=True,
        is_pending=False,
    )
    db.add_all([deck, proxy])
    db.commit()
    barrier = Barrier(2)
    source_id, source_version = a.source.id, ds.assembly_row_version(a.source)
    user_id = user.id
    requests = [
        (a.deck.id, a.proxy.id, ds.assembly_row_version(a.proxy)),
        (deck.id, proxy.id, ds.assembly_row_version(proxy)),
    ]

    def claim(request):
        with Session(db.bind) as session:
            barrier.wait(timeout=10)
            deck_id, proxy_id, version = request
            return ds.confirm_deck_assembly(
                session, user_id, deck_id, proxy_id, source_id, 1, version, source_version
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, requests))
    assert sorted(outcomes) == [False, True]
    db.expire_all()
    real = db.query(InventoryRow).filter_by(is_proxy=False).all()
    assert len(real) == 1 and real[0].quantity == 1
    assert db.query(TransactionLog).count() == 1
