"""Authenticated route-render smoke (v3.32.x).

Pytest module (matches tests/test_share_service).
Invoke via:

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_route_smoke.py

Closes a recurring gap: two prod 500s (the v3.31.0 value-totals feature's
``list_locations`` import bug caught on Dev, and the ``shares_view`` missing
``total_value`` that reached prod) were route→template mismatches that the
pre-push smoke missed because it never GET'd every page. In particular the
old smoke hit ``/shares`` + ``/showcase/{id}`` but never ``/shares/{id}``.

This test seeds ONE populated user (card, storage location, placed row,
deck, game+seat, showcase+item, share→playgroup) and GETs every
authenticated page route, asserting it renders (200, or the documented 303
for the legacy ``/showcase`` redirect). It does NOT assert page content —
it's a wholesale guard that every route→template wiring is intact, the
class of bug that keeps reaching prod. Uses one shared in-memory DB
(StaticPool) + dependency overrides so routes read these fixtures, not the
dev DB.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import share_service
from app.db import Base
from app.models import (
    Card,
    Deck,
    Game,
    GameSeat,
    InventoryRow,
    Playgroup,
    Share,
    ShowcaseItem,
    StorageLocation,
    User,
)


def _seed(s):
    """Build one populated user and return (user, ids) for route params."""
    user = User(username="smoke", password_hash="x")
    s.add(user)
    s.flush()

    loc = StorageLocation(user_id=user.id, name="Binder", type="binder", mode="managed")
    s.add(loc)
    s.flush()

    card = Card(
        scryfall_id="smoke-1",
        name="Smoke Card",
        set_code="tst",
        collector_number="1",
        type_line="Creature — Test",
        color_identity="G",
        price_usd="1.00",
    )
    s.add(card)
    s.flush()

    s.add(
        InventoryRow(
            card_id=card.id,
            user_id=user.id,
            storage_location_id=loc.id,
            quantity=1,
            finish="normal",
            is_pending=False,
        )
    )

    deck_loc = StorageLocation(user_id=user.id, name="My Deck", type="deck", mode="managed")
    s.add(deck_loc)
    s.flush()
    deck = Deck(
        user_id=user.id, name="My Deck", format="Commander", storage_location_id=deck_loc.id
    )
    s.add(deck)
    s.flush()

    game = Game(user_id=user.id, format="Commander", status="created")
    s.add(game)
    s.flush()
    s.add(
        GameSeat(
            game_id=game.id,
            seat_number=1,
            player_name="Smoke",
            user_id=user.id,
            grid_position="C",
        )
    )

    sc = share_service.create_showcase(s, user.id, "Trade binder", None)
    s.flush()
    s.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=1, quantity_offered=1))

    pg = Playgroup(name="PG", created_by=user.id, join_code="SMOKE1")
    s.add(pg)
    s.flush()
    share = Share(user_id=user.id, showcase_id=sc.id, playgroup_id=pg.id)
    s.add(share)
    s.commit()

    return user, {
        "loc_id": loc.id,
        "deck_id": deck.id,
        "game_id": game.id,
        "set_code": card.set_code,
        "showcase_id": sc.id,
        "share_id": share.id,
    }


def test_authenticated_pages_render() -> int:
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    user, ids = _seed(sm())

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    main.app.dependency_overrides[get_current_user] = lambda: user

    # (path, expected_status). 303 for the documented legacy redirect.
    routes = [
        ("/", 200),
        ("/collection", 200),
        ("/collection?colors=G", 200),
        ("/pending", 200),
        ("/locations", 200),
        (f"/locations/{ids['loc_id']}", 200),
        ("/decks", 200),
        (f"/decks/{ids['deck_id']}", 200),
        ("/sets", 200),
        # NOTE: /sets/{set_code} is intentionally omitted — it has a documented
        # request-path Scryfall-fetch history (v3.27.13). Keeping this smoke
        # fully offline avoids flakiness; the route→template wiring class this
        # test guards is covered by the other set/collection pages.
        ("/games", 200),
        ("/games/new", 200),
        (f"/games/{ids['game_id']}", 200),
        ("/showcases", 200),
        ("/showcase", 303),  # legacy redirect → /showcases
        (f"/showcase/{ids['showcase_id']}", 200),
        ("/shares", 200),
        (f"/shares/{ids['share_id']}", 200),
    ]

    failed = 0
    try:
        c = TestClient(main.app, follow_redirects=False)
        for path, expected in routes:
            r = c.get(path)
            if r.status_code != expected:
                print(f"  [FAIL] GET {path} -> {r.status_code} (expected {expected})")
                failed += 1
            else:
                print(f"  [OK] GET {path} -> {r.status_code}")
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)
        main.app.dependency_overrides.pop(get_current_user, None)
    assert failed == 0
