"""Companion pre-live deck self-selection (player picks own deck from the phone)."""

from __future__ import annotations

import itertools

import pytest

from app import game_service
from app.game_service import GameLockedError, set_own_seat_deck
from app.models import (
    Card,
    Deck,
    Game,
    GameSeat,
    InventoryRow,
    Playgroup,
    PlaygroupMember,
    StorageLocation,
    User,
)
from app.timeutil import utc_now

_seq = itertools.count(1)


def _user(db, name=None) -> User:
    u = User(username=(name or f"cd{next(_seq)}@ex.com"), password_hash="x")
    db.add(u)
    db.flush()
    return u


def _game(db, owner_id, *, seats, status="created", playgroup_id=None):
    game = Game(
        user_id=owner_id,
        format="Commander",
        status=status,
        client_token="TOK",
        first_seat_number=1,
        playgroup_id=playgroup_id,
        played_at=utc_now(),
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
            starting_life=40,
        )
        db.add(s)
        objs.append(s)
    db.flush()
    return game, objs


def _deck(db, user_id, *, name="My Deck", commander="Commander X", scryfall_id=None) -> Deck:
    loc = StorageLocation(user_id=user_id, name=f"L{next(_seq)}", type="deck", mode="managed")
    db.add(loc)
    db.flush()
    deck = Deck(user_id=user_id, name=name, storage_location_id=loc.id)
    db.add(deck)
    db.flush()
    if commander:
        card = Card(
            scryfall_id=(scryfall_id or f"sid-{next(_seq)}"),
            name=commander,
            set_code="cmr",
            set_name="Cmd",
            collector_number=str(next(_seq)),
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


# ── service: set_own_seat_deck ───────────────────────────────────────────────


def test_set_own_deck_rederives_snapshot_fields(db, user):
    deck = _deck(db, user.id, name="Krenko Goblins", commander="Krenko", scryfall_id="krk-1")
    game, seats = _game(db, user.id, seats=[{"user_id": user.id}, {}])
    seat = set_own_seat_deck(db, game.id, user.id, deck.id)
    assert seat.id == seats[0].id  # the caller's own seat
    assert seat.deck_id == deck.id
    assert seat.deck_name_at_game == "Krenko Goblins"  # re-derived like create_game
    assert seat.commander_name_at_game == "Krenko"


def test_set_own_deck_mutates_only_callers_seat(db, user):
    other = _user(db)
    deck = _deck(db, user.id)
    game, seats = _game(db, user.id, seats=[{"user_id": other.id}, {"user_id": user.id}])
    seat = set_own_seat_deck(db, game.id, user.id, deck.id)
    assert seat.id == seats[1].id  # user's seat, not seat 0 (other's)
    assert db.get(GameSeat, seats[0].id).deck_id is None


def test_set_deck_not_owned_raises_permission(db, user):
    other = _user(db)
    foreign_deck = _deck(db, other.id)
    game, _ = _game(db, user.id, seats=[{"user_id": user.id}, {}])
    with pytest.raises(PermissionError):
        set_own_seat_deck(db, game.id, user.id, foreign_deck.id)


def test_set_deck_unknown_id_raises_value(db, user):
    game, _ = _game(db, user.id, seats=[{"user_id": user.id}, {}])
    with pytest.raises(ValueError):
        set_own_seat_deck(db, game.id, user.id, 999999)


def test_set_deck_in_progress_locks(db, user):
    deck = _deck(db, user.id)
    game, seats = _game(db, user.id, seats=[{"user_id": user.id}, {}], status="in_progress")
    with pytest.raises(GameLockedError):
        set_own_seat_deck(db, game.id, user.id, deck.id)
    assert db.get(GameSeat, seats[0].id).deck_id is None  # unchanged


def test_set_deck_unseated_raises_permission(db, user):
    owner = _user(db)
    pg = Playgroup(name="PG", created_by=owner.id)
    db.add(pg)
    db.flush()
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=user.id))  # viewer, not seated
    game, _ = _game(db, owner.id, seats=[{"user_id": owner.id}, {}], playgroup_id=pg.id)
    deck = _deck(db, user.id)
    with pytest.raises(PermissionError):
        set_own_seat_deck(db, game.id, user.id, deck.id)


def test_clear_deck_allowed(db, user):
    deck = _deck(db, user.id)
    game, seats = _game(db, user.id, seats=[{"user_id": user.id, "deck_id": deck.id}, {}])
    seat = set_own_seat_deck(db, game.id, user.id, None)  # clear
    assert seat.deck_id is None
    assert seat.deck_name_at_game is None and seat.commander_name_at_game is None


# ── route ────────────────────────────────────────────────────────────────────


def test_route_set_deck_returns_seat_identity(db, client, user):
    deck = _deck(db, user.id, name="Atraxa", commander="Atraxa", scryfall_id="atx-9")
    game, seats = _game(db, user.id, seats=[{"user_id": user.id}, {}])
    db.commit()
    r = client.post(f"/games/{game.id}/companion/deck", data={"deck_id": deck.id})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["seat"]["deck_name"] == "Atraxa"
    assert body["seat"]["commander_name"] == "Atraxa"
    assert body["seat"]["commander_scryfall_id"] == "atx-9"  # art context updates


def test_route_set_deck_not_owned_403(db, client, user):
    other = _user(db)
    foreign = _deck(db, other.id)
    game, _ = _game(db, user.id, seats=[{"user_id": user.id}, {}])
    db.commit()
    assert (
        client.post(f"/games/{game.id}/companion/deck", data={"deck_id": foreign.id}).status_code
        == 403
    )


def test_route_set_deck_in_progress_409(db, client, user):
    deck = _deck(db, user.id)
    game, _ = _game(db, user.id, seats=[{"user_id": user.id}, {}], status="in_progress")
    db.commit()
    assert (
        client.post(f"/games/{game.id}/companion/deck", data={"deck_id": deck.id}).status_code
        == 409
    )


# ── GET companion pre-live picker context ────────────────────────────────────


def test_companion_get_created_seated_shows_picker(db, client, user):
    _deck(db, user.id, name="Edgar Vampires", commander="Edgar")
    game, _ = _game(db, user.id, seats=[{"user_id": user.id}, {}], status="created")
    db.commit()
    html = client.get(f"/games/{game.id}/companion").text
    assert "Change deck" in html
    assert "Edgar Vampires" in html  # the user's deck offered
    assert "Waiting for the game to start" in html


def test_companion_get_created_spectator_no_picker(db, client, user):
    owner = _user(db)
    pg = Playgroup(name="PG", created_by=owner.id)
    db.add(pg)
    db.flush()
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=user.id))
    game, _ = _game(
        db, owner.id, seats=[{"user_id": owner.id}, {}], status="created", playgroup_id=pg.id
    )
    db.commit()
    html = client.get(f"/games/{game.id}/companion").text
    assert "Change deck" not in html  # spectator gets no picker
    assert "Spectating" in html


def test_companion_get_in_progress_no_picker(db, client, user):
    game, _ = _game(db, user.id, seats=[{"user_id": user.id}, {}], status="in_progress")
    db.commit()
    html = client.get(f"/games/{game.id}/companion").text
    assert "Change deck" not in html  # live game: no picker anywhere
    assert "End turn" in html  # the live tracker instead


def test_list_user_decks_for_companion_sorted(db, user):
    _deck(db, user.id, name="Zzz Deck", commander=None)
    _deck(db, user.id, name="Aaa Deck", commander="Aaa Cmdr")
    _user(db)  # another user's deck must not leak — none created for them here
    out = game_service.list_user_decks_for_companion(db, user.id)
    assert [d["name"] for d in out] == ["Aaa Deck", "Zzz Deck"]  # sorted by name
    assert out[0]["commander_name"] == "Aaa Cmdr"
