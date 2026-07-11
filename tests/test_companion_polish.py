"""Companion polish: standalone fullscreen view, /companion lobby, commander art."""

from __future__ import annotations

import itertools

from app import game_service
from app.models import Card, Deck, Game, GameSeat, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _user(db, name=None) -> User:
    u = User(username=(name or f"cp{next(_seq)}@ex.com"), password_hash="x")
    db.add(u)
    db.flush()
    return u


def _game(db, owner_id, *, seats, status="in_progress", played_at=None):
    from app.timeutil import utc_now

    game = Game(
        user_id=owner_id,
        format="Commander",
        status=status,
        client_token="TOK",
        first_seat_number=1,
        played_at=played_at or utc_now(),
    )
    db.add(game)
    db.flush()
    objs = []
    for i, spec in enumerate(seats, start=1):
        s = GameSeat(
            game_id=game.id,
            seat_number=i,
            player_name=f"P{i}",
            user_id=spec.get("user_id"),
            deck_id=spec.get("deck_id"),
            commander_name_at_game=spec.get("commander_name"),
            starting_life=40,
        )
        db.add(s)
        objs.append(s)
    db.flush()
    return game, objs


def _deck_with_commander(db, user_id, scryfall_id="cmdr-sid-x") -> Deck:
    loc = StorageLocation(user_id=user_id, name="DeckLoc", type="deck", mode="managed")
    db.add(loc)
    db.flush()
    deck = Deck(user_id=user_id, name="My Deck", storage_location_id=loc.id)
    db.add(deck)
    db.flush()
    card = Card(
        scryfall_id=scryfall_id,
        name="Commander X",
        set_code="cmr",
        set_name="Cmd",
        collector_number="1",
        rarity="mythic",
        type_line="Legendary Creature",
        oracle_text="x",
        image_url="http://x/i.png",
        color_identity="",
        set_type="commander",
        price_usd="1",
        price_usd_foil=None,
    )
    db.add(card)
    db.flush()
    db.add(
        InventoryRow(
            user_id=user_id,
            card_id=card.id,
            finish="normal",
            quantity=1,
            storage_location_id=loc.id,
            is_pending=False,
            role="commander",
        )
    )
    db.flush()
    return deck


# ── D1: standalone fullscreen ────────────────────────────────────────────────


def test_companion_page_is_standalone_with_no_zoom_metas(db, client, user):
    game, seats = _game(db, user.id, seats=[{"user_id": user.id}, {}])
    db.commit()
    html = client.get(f"/games/{game.id}/companion").text
    # Standalone: no site nav chrome.
    assert 'class="nav-item' not in html
    assert "<!DOCTYPE html>" in html
    # No-zoom + web-app metas.
    assert "maximum-scale=1" in html and "user-scalable=no" in html
    assert 'name="apple-mobile-web-app-capable"' in html
    assert 'name="mobile-web-app-capable"' in html
    assert "Back to Cartarch" in html  # escape hatch


# ── D2: /companion lobby ─────────────────────────────────────────────────────


def test_lobby_lists_seated_games_in_progress_first(db, client, user):
    from app.timeutil import utc_now

    waiting, _ = _game(
        db, user.id, seats=[{"user_id": user.id}], status="created", played_at=utc_now()
    )
    live, _ = _game(
        db, user.id, seats=[{"user_id": user.id}], status="in_progress", played_at=utc_now()
    )
    db.commit()
    html = client.get("/companion").text
    assert "LIVE" in html and "Waiting" in html
    # in_progress game listed before the created game.
    assert html.index(f"/games/{live.id}/companion") < html.index(f"/games/{waiting.id}/companion")


def test_lobby_empty_state_for_user_with_no_seats(db, client, user):
    # A game exists but the client user isn't seated in it.
    other = _user(db)
    _game(db, other.id, seats=[{"user_id": other.id}], status="in_progress")
    db.commit()
    assert "No games yet" in client.get("/companion").text


def test_lobby_requires_login():
    from fastapi.testclient import TestClient

    from app import main

    # A raw client (no get_current_user override) with no session → redirect to login.
    r = TestClient(main.app).get("/companion", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")


# ── D3: commander art context ────────────────────────────────────────────────


def test_seat_commander_scryfall_id_resolves_and_degrades(db, user):
    deck = _deck_with_commander(db, user.id, scryfall_id="abc-123")
    game, seats = _game(db, user.id, seats=[{"user_id": user.id, "deck_id": deck.id}, {}])
    ids = game_service.get_seat_commander_scryfall_ids(db, game)
    assert ids[seats[0].id] == "abc-123"  # seat with a commander
    assert ids[seats[1].id] is None  # seat with no deck → None


def test_companion_renders_commander_background(db, client, user):
    deck = _deck_with_commander(db, user.id, scryfall_id="art-999")
    game, seats = _game(db, user.id, seats=[{"user_id": user.id, "deck_id": deck.id}, {}])
    db.commit()
    html = client.get(f"/games/{game.id}/companion").text
    assert "cmp-has-art" in html
    assert "art-999" in html  # mirror URL built from the commander scryfall_id


def test_companion_no_commander_renders_without_art(db, client, user):
    game, seats = _game(db, user.id, seats=[{"user_id": user.id}, {}])  # no deck
    db.commit()
    r = client.get(f"/games/{game.id}/companion")
    assert r.status_code == 200
    assert "cmp-has-art" not in r.text  # solid background, no broken image
