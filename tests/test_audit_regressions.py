import asyncio
from types import SimpleNamespace

import pytest

from app.models import Card, ImportBatch, InventoryRow, TransactionLog


@pytest.mark.parametrize("batch", [False, True])
def test_repeated_undo_is_idempotent(client, db, user, batch):
    c = Card(scryfall_id="audit-replay", name="Audit Card", set_code="tst", collector_number="1")
    db.add(c)
    db.flush()
    r = InventoryRow(user_id=user.id, card_id=c.id, quantity=5, finish="normal", is_pending=True)
    b = ImportBatch(user_id=user.id, filename="audit", row_count=1)
    db.add_all([r, b])
    db.flush()
    db.add(
        TransactionLog(
            user_id=user.id,
            event_type="import",
            card_id=c.id,
            finish="normal",
            quantity_delta=2,
            inventory_row_id=r.id,
            batch_id=b.id,
        )
    )
    db.commit()
    url = "/imports/undo-batch" if batch else "/imports/undo-last"
    data = {"batch_id": b.id} if batch else {}
    first = client.post(url, data=data, follow_redirects=False)
    db.refresh(r)
    q1 = r.quantity
    second = client.post(url, data=data, follow_redirects=False)
    db.refresh(r)
    q2 = r.quantity
    assert (first.status_code, second.status_code, q1, q2) == (303, 303, 3, 3)


def test_password_change_revokes_reset_token(client, db, user):
    from app.auth import hash_password, verify_password
    from app.password_reset_service import create_reset_token, find_valid_token

    user.password_hash = hash_password("old-password-audit")
    db.commit()
    token = create_reset_token(db, user)
    db.commit()
    response = client.post(
        "/account/change-password",
        data={
            "current_password": "old-password-audit",
            "new_password": "new-password-audit",
            "confirm_password": "new-password-audit",
        },
        follow_redirects=False,
    )
    db.refresh(user)
    assert response.headers["location"] == "/account?success=password_changed"
    assert verify_password("new-password-audit", user.password_hash)
    assert find_valid_token(db, token) is None


def test_stream_subscribes_before_initial_event(monkeypatch):
    from app import live_game_events
    from app.routes import live_games

    monkeypatch.setattr(live_games, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(live_games, "get_live_state", lambda *args: object())
    monkeypatch.setattr(live_games, "state_payload", lambda _: {"version": 1, "state": {}})
    monkeypatch.setattr(live_games, "_SSE_HEARTBEAT_SECONDS", 0.01)

    class Request:
        session = {"user_id": 1}

        async def is_disconnected(self):
            return False

    async def run():
        response = await live_games.live_stream(Request(), 12345)
        stream = response.body_iterator
        first = await anext(stream)
        assert '"version": 1' in first
        assert live_game_events.subscriber_count(12345) == 1
        live_game_events.publish(12345, '{"version":2,"state":{}}')
        second = await anext(stream)
        await stream.aclose()
        assert '"version":2' in second
        assert live_game_events.subscriber_count(12345) == 0

    asyncio.run(run())


def test_invalid_import_destination_is_rejected_before_persistence(client, db, user, monkeypatch):
    from app.routes import imports

    def unexpected_persistence(*args, **kwargs):
        pytest.fail("Invalid destination must be rejected before persistence")

    monkeypatch.setattr(imports, "persist_import_rows", unexpected_persistence)
    c = Card(scryfall_id="audit-import", name="Audit import", set_code="tst", collector_number="1")
    db.add(c)
    db.commit()
    response = client.post(
        "/import/manual/commit",
        data={"scryfall_id": c.scryfall_id, "quantity": 2, "target_location_id": 987654321},
        follow_redirects=False,
    )
    rows = db.query(InventoryRow).filter_by(user_id=user.id, card_id=c.id).all()
    assert response.status_code == 400, response.status_code
    assert sum(r.quantity for r in rows) == 0


def test_trade_terminal_transition_refreshes_stale_session(db, user, db_engine):
    from sqlalchemy.orm import Session

    from app.models import Trade, TradeRevision, User
    from app.trade_service import transition_trade

    other = User(username="audit-recipient@example.com", password_hash="x")
    db.add(other)
    db.flush()
    t = Trade(proposer_user_id=user.id, recipient_user_id=other.id, status="proposed")
    db.add(t)
    db.flush()
    db.add(TradeRevision(trade_id=t.id, author_user_id=user.id))
    db.commit()
    with Session(db_engine) as a, Session(db_engine) as b:
        stale = b.get(Trade, t.id)
        assert stale.status == "proposed"
        accepted = transition_trade(a, t.id, other.id, "accepted")
        assert accepted.status == "accepted"
        with pytest.raises(ValueError):
            transition_trade(b, t.id, user.id, "cancelled")
    db.expire_all()
    assert db.get(Trade, t.id).status == "accepted"


def test_import_undo_follows_destination_merge(client, db, user):
    from app.models import StorageLocation

    c = Card(scryfall_id="audit-merge", name="Audit merge", set_code="tst", collector_number="1")
    loc = StorageLocation(user_id=user.id, name="Audit binder", type="binder", mode="manual")
    db.add_all([c, loc])
    db.flush()
    r = InventoryRow(
        user_id=user.id,
        card_id=c.id,
        quantity=3,
        finish="normal",
        storage_location_id=loc.id,
        is_pending=False,
    )
    db.add(r)
    db.commit()
    response = client.post(
        "/import/manual/commit",
        data={"scryfall_id": c.scryfall_id, "quantity": 2, "target_location_id": loc.id},
        follow_redirects=False,
    )
    assert response.status_code == 200
    db.refresh(r)
    assert r.quantity == 5
    batch = db.query(ImportBatch).filter_by(user_id=user.id).one()
    response = client.post(
        "/imports/undo-batch", data={"batch_id": batch.id}, follow_redirects=False
    )
    assert response.status_code == 303
    db.refresh(r)
    assert r.quantity == 3


def test_password_reset_throttle_does_not_allocate_after_ip_block(monkeypatch):
    from collections import OrderedDict

    from app import password_reset_service as resets

    monkeypatch.setattr(resets, "_rate_log", OrderedDict())
    for i in range(1000):
        resets.check_rate_limits(f"unknown-{i}@example.invalid", "192.0.2.1")
    assert len(resets._rate_log) == 6


def test_undo_batch_validates_all_targets_before_writing(db, user):
    from app.inventory_service import undo_last_batch, undo_last_import

    card = Card(scryfall_id="undo-validation", name="Forest", set_code="tst", collector_number="2")
    db.add(card)
    db.flush()
    row = InventoryRow(user_id=user.id, card_id=card.id, quantity=4, finish="normal")
    batch = ImportBatch(user_id=user.id, filename="validation", row_count=2)
    db.add_all([row, batch])
    db.flush()
    logs = [
        TransactionLog(
            user_id=user.id,
            event_type="import",
            card_id=card.id,
            finish="normal",
            quantity_delta=3,
            inventory_row_id=row.id,
            batch_id=batch.id,
        )
        for _ in range(2)
    ]
    db.add_all(logs)
    db.commit()
    with pytest.raises(ValueError, match="copies are no longer present"):
        undo_last_batch(db, batch.id, user.id)
    db.rollback()
    assert db.get(InventoryRow, row.id).quantity == 4
    assert all(log.reversed_at is None for log in logs)
    # Single-event and batch undo share the same durable marker.
    row.quantity = 10
    db.commit()
    assert undo_last_import(db, user.id)
    assert undo_last_batch(db, batch.id, user.id) == 1
    assert not undo_last_import(db, user.id)
    assert row.quantity == 4


@pytest.mark.parametrize("path", ["/import/manual/commit", "/import/commit"])
def test_import_rollback_after_placement_commit(client, db, user, db_engine, monkeypatch, path):
    from sqlalchemy.orm import Session

    from app.models import StorageLocation
    from app.routes import imports as import_routes

    card = Card(scryfall_id="rollback-card", name="Forest", set_code="tst", collector_number="3")
    loc = StorageLocation(user_id=user.id, name="Rollback binder", type="binder", mode="manual")
    db.add_all([card, loc])
    db.commit()
    place = import_routes.place_imported_rows

    def fail_after_placement(*args, **kwargs):
        place(*args, **kwargs)
        args[0].commit()  # Even a deep service commit must remain rollback-able.
        raise ValueError("Injected failure after placement")

    monkeypatch.setattr(import_routes, "place_imported_rows", fail_after_placement)
    response = client.post(
        path,
        data={
            "scryfall_id": card.scryfall_id,
            "name": card.name,
            "quantity": 2,
            "line_number": 1,
            "set_code": "tst",
            "collector_number": "3",
            "location": "",
            "finish": "normal",
            "target_location_id": loc.id,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Injected failure" in response.text
    db.rollback()
    with Session(db_engine) as check:
        assert check.query(InventoryRow).filter_by(user_id=user.id).count() == 0
        assert check.query(ImportBatch).filter_by(user_id=user.id).count() == 0
        assert check.query(TransactionLog).filter_by(user_id=user.id).count() == 0


def test_admin_password_change_revokes_only_target_tokens(client, db, user):
    from app.auth import verify_password
    from app.models import User
    from app.password_reset_service import create_reset_token, find_valid_token

    user.is_admin = True
    target = User(username="reset-target@example.com", password_hash="x")
    db.add(target)
    db.commit()
    own_token = create_reset_token(db, user)
    token = create_reset_token(db, target)
    db.commit()
    response = client.post(
        f"/admin/users/{target.id}/reset-password",
        data={"new_password": "a-new-password"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/admin?success=password_reset"
    db.refresh(target)
    assert verify_password("a-new-password", target.password_hash)
    assert find_valid_token(db, token) is None
    assert find_valid_token(db, own_token) is not None


def test_reset_throttle_is_bounded_and_expires(monkeypatch):
    from collections import OrderedDict
    from datetime import timedelta

    from app import password_reset_service as resets
    from app.timeutil import utc_now

    monkeypatch.setattr(resets, "_rate_log", OrderedDict())
    monkeypatch.setattr(resets, "RESET_RATE_LIMIT_MAX_KEYS", 20)
    now = utc_now()
    monkeypatch.setattr(resets, "utc_now", lambda: now)
    for i in range(40):
        assert resets.check_rate_limits(f"{i}@example.com", str(i))
    assert len(resets._rate_log) == 20
    for _ in range(4):
        assert resets.check_rate_limits("39@example.com", "39")
    assert not resets.check_rate_limits("39@example.com", "other")
    assert "ip:other" not in resets._rate_log
    now += timedelta(hours=2)
    assert resets.check_rate_limits("39@example.com", "39")


def test_stream_refreshes_after_subscription_and_discards_old_events(monkeypatch):
    from app import live_game_events
    from app.routes import live_games

    current = {"version": 1, "state": {}}
    closed = []
    monkeypatch.setattr(
        live_games, "SessionLocal", lambda: SimpleNamespace(close=lambda: closed.append(True))
    )
    monkeypatch.setattr(live_games, "get_live_state", lambda *args: current.copy())
    monkeypatch.setattr(live_games, "state_payload", lambda live: live)
    monkeypatch.setattr(live_games, "_SSE_HEARTBEAT_SECONDS", 0.01)

    class Request:
        session = {"user_id": 1}

        async def is_disconnected(self):
            return False

    async def run():
        response = await live_games.live_stream(Request(), 54321)
        current["version"] = 3  # Update after authorization, before body starts.
        stream = response.body_iterator
        assert '"version": 3' in await anext(stream)
        assert len(closed) == 2  # No connection retained by the open stream.
        live_game_events.publish(54321, '{"version":2,"state":{}}')
        live_game_events.publish(54321, '{"version":3,"state":{}}')
        live_game_events.publish(54321, '{"version":4,"state":{}}')
        assert '"version":4' in await anext(stream)
        assert await anext(stream) == ": keepalive\n\n"
        await stream.aclose()
        assert live_game_events.subscriber_count(54321) == 0

    asyncio.run(run())


def test_import_reversal_migration_backfills_legacy_undo(db, user, db_engine):
    if db_engine.dialect.name != "postgresql":
        pytest.skip("Alembic is PostgreSQL-only")
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    original = TransactionLog(user_id=user.id, event_type="import", quantity_delta=2)
    untouched = TransactionLog(user_id=user.id, event_type="import", quantity_delta=1)
    db.add_all([original, untouched])
    db.flush()
    db.add(
        TransactionLog(
            user_id=user.id,
            event_type="undo_batch_import",
            quantity_delta=-2,
            note=f"Undid import log {original.id} from batch 12",
        )
    )
    db.commit()
    ids = original.id, untouched.id
    db.close()
    spec = importlib.util.spec_from_file_location(
        "reversal_migration",
        Path(__file__).parents[1] / "alembic/versions/c49ef51a823d_import_reversal_marker.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with db_engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        migration.upgrade()
        result = dict(
            conn.execute(
                text("SELECT id, reversed_at FROM transaction_logs WHERE event_type='import'")
            ).all()
        )
        assert result[ids[0]] is not None
        assert result[ids[1]] is None


def test_concurrent_trade_transitions_serialize_on_postgres(db, user, db_engine, monkeypatch):
    if db_engine.dialect.name != "postgresql":
        pytest.skip("Row-lock concurrency requires PostgreSQL")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy.orm import Session

    from app import trade_service
    from app.models import Trade, TradeRevision, User

    recipient = User(username="concurrent-recipient@example.com", password_hash="x")
    db.add(recipient)
    db.flush()
    trade = Trade(proposer_user_id=user.id, recipient_user_id=recipient.id, status="proposed")
    db.add(trade)
    db.flush()
    db.add(TradeRevision(trade_id=trade.id, author_user_id=user.id))
    db.commit()
    tid, proposer_id, recipient_id = trade.id, user.id, recipient.id
    locked, release, competing = Event(), Event(), Event()
    snapshot = trade_service.write_trade_terminal_snapshot

    def hold_first(session, trade):
        if trade.status == "accepted":
            locked.set()
            assert release.wait(5), "test did not release first transaction"
        return snapshot(session, trade)

    monkeypatch.setattr(trade_service, "write_trade_terminal_snapshot", hold_first)

    def transition(actor, status):
        with Session(db_engine) as session:
            if status == "cancelled":
                competing.set()
            try:
                return trade_service.transition_trade(session, tid, actor, status).status
            except ValueError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(transition, recipient_id, "accepted")
        try:
            assert locked.wait(5)
            second = pool.submit(transition, proposer_id, "cancelled")
            assert competing.wait(5)
            # Give the second connection time to reach the contested row. It
            # must wait, not approve a contradictory terminal transition.
            from concurrent.futures import TimeoutError

            with pytest.raises(TimeoutError):
                second.result(timeout=0.2)
        finally:
            release.set()
        assert first.result(timeout=5) == "accepted"
        assert second.result(timeout=5) == "rejected"
    db.expire_all()
    assert db.get(Trade, tid).status == "accepted"


def test_printing_modal_renders_hover_fallback(client, db, user, monkeypatch):
    import os
    from html.parser import HTMLParser
    from pathlib import Path

    from app.models import Deck, StorageLocation
    from app.routes import decks

    card = Card(
        scryfall_id="b5c9649e-9ae5-4926-bf08-71ba23aa37f1",
        name="Aberrant Researcher // Perfected Form",
        set_code="soi",
        collector_number="52",
        image_url="https://example.invalid/card.jpg",
        layout="transform",
    )
    loc = StorageLocation(user_id=user.id, name="Image tests", type="deck", mode="manual")
    db.add_all([card, loc])
    db.flush()
    deck = Deck(user_id=user.id, name="Image tests", storage_location_id=loc.id)
    row = InventoryRow(
        user_id=user.id, card_id=card.id, quantity=1, finish="normal", storage_location_id=loc.id
    )
    db.add_all([deck, row])
    db.commit()
    monkeypatch.setattr(decks, "fetch_card_printings", lambda _: [])
    response = client.get(f"/decks/{deck.id}/rows/{row.id}/printings-modal")
    assert response.status_code == 200

    class Rows(HTMLParser):
        images = []

        def handle_starttag(self, tag, attrs):
            data = dict(attrs)
            if "data-card-image" in data:
                self.images.append(data)

    parsed = Rows()
    parsed.feed(response.text)
    assert parsed.images
    assert all(
        i.get("data-card-image-alt", "").startswith("https://api.scryfall.com/")
        for i in parsed.images
    )
    if directory := os.getenv("AUDIT_BROWSER_FIXTURES"):
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        (output / "printing.html").write_text(response.text)
        page = client.get(f"/decks/{deck.id}")
        assert page.status_code == 200
        (output / "deck.html").write_text(page.text)
