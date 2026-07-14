"""#115 — Planechase overlay on companion mode (plane deck + planar die).

Service-level: the plane pool query, the enable/roll/planeswalk actions through
the real ``apply_live_action`` pipeline (auth included), and the ingest keeping
planes/phenomena alongside creatures.
"""

from __future__ import annotations

import itertools
import json

import pytest

from app import live_game_service as lgs
from app.jobs.oracle_ingest import extract
from app.models import Game, GameSeat, OracleCatalog, User

TABLE = "TABLETOK"
_seq = itertools.count(1)


def _user(db) -> User:
    u = User(username=f"u{next(_seq)}@ex.com", password_hash="x")
    db.add(u)
    db.flush()
    return u


def _plane(db, name: str) -> OracleCatalog:
    p = OracleCatalog(
        oracle_id=f"oid-{next(_seq)}",
        name=name,
        type_line="Plane — Test",
        oracle_text=f"{name}: chaos happens.",
        scryfall_id=f"sid-{next(_seq)}",
        is_momir_legal=False,
    )
    db.add(p)
    db.flush()
    return p


def _game(db, owner_id, seat_user_id):
    game = Game(user_id=owner_id, format="Commander", status="created", client_token=TABLE)
    db.add(game)
    db.flush()
    s = GameSeat(
        game_id=game.id, seat_number=1, player_name="P1", user_id=seat_user_id, starting_life=40
    )
    db.add(s)
    db.flush()
    return game, s


def _state(live):
    return json.loads(live.state)


# ── plane pool + actions through the real pipeline ────────────────────────────


def test_enable_roll_walk_and_reshuffle(db):
    owner = _user(db)
    _plane(db, "Plane A")
    _plane(db, "Plane B")
    db.commit()
    game, _seat = _game(db, owner.id, owner.id)
    lgs.start_live_game(db, game.id, owner.id)

    # Commander blob stays byte-identical until enabled.
    assert "planechase" not in _state(game.live_state)

    # Enable (table token) → shuffles the pool, reveals the top plane.
    live = lgs.apply_live_action(db, game.id, owner.id, {"type": "planechase_enable"}, TABLE)
    st = _state(live)
    assert st["planechase"] is True
    assert st["currentPlane"] is not None
    assert len(st["planeDeck"]) == 1 and st["planeDiscard"] == []
    first_id = st["currentPlane"]["id"]

    # Planeswalk → current moves to discard, next revealed.
    live = lgs.apply_live_action(db, game.id, owner.id, {"type": "planeswalk"}, TABLE)
    st = _state(live)
    assert st["currentPlane"]["id"] != first_id
    assert st["planeDiscard"] == [first_id]
    assert st["planeDeck"] == []
    second_id = st["currentPlane"]["id"]

    # Deck empty → next planeswalk reshuffles the WHOLE discard back in, then draws.
    live = lgs.apply_live_action(db, game.id, owner.id, {"type": "planeswalk"}, TABLE)
    st = _state(live)
    all_ids = {first_id, second_id}
    assert st["currentPlane"]["id"] in all_ids
    assert st["planeDiscard"] == []  # all reshuffled back into the deck, one drawn
    assert len(st["planeDeck"]) == 1
    # No plane lost or duplicated across current + deck + discard.
    assert {st["currentPlane"]["id"], *st["planeDeck"], *st["planeDiscard"]} == all_ids

    # Planar die roll returns a face and stamps lastRoll.
    live = lgs.apply_live_action(db, game.id, owner.id, {"type": "planar_roll"}, TABLE)
    st = _state(live)
    assert st["lastRoll"] in {"blank", "chaos", "planeswalk"}


def test_roll_before_enable_is_rejected(db):
    owner = _user(db)
    _plane(db, "Plane A")
    db.commit()
    game, _seat = _game(db, owner.id, owner.id)
    lgs.start_live_game(db, game.id, owner.id)
    with pytest.raises(ValueError):
        lgs.apply_live_action(db, game.id, owner.id, {"type": "planar_roll"}, TABLE)


def test_authorization_enable_is_table_only_roll_is_seated(db):
    owner = _user(db)
    player = _user(db)
    _plane(db, "Plane A")
    _plane(db, "Plane B")
    db.commit()
    game, _seat = _game(db, owner.id, player.id)
    lgs.start_live_game(db, game.id, owner.id)

    # A phone (seated player, no table token) may NOT enable Planechase.
    with pytest.raises(PermissionError):
        lgs.apply_live_action(db, game.id, player.id, {"type": "planechase_enable"}, None)
    # The table enables it...
    lgs.apply_live_action(db, game.id, owner.id, {"type": "planechase_enable"}, TABLE)
    # ...then the seated player CAN roll from their phone (no table token).
    live = lgs.apply_live_action(db, game.id, player.id, {"type": "planar_roll"}, None)
    assert _state(live)["lastRoll"] in {"blank", "chaos", "planeswalk"}


# ── ingest keeps planes/phenomena alongside creatures ─────────────────────────


def _card(oracle_id, name, type_line, **extra):
    return {
        "oracle_id": oracle_id,
        "name": name,
        "type_line": type_line,
        "id": f"sid-{oracle_id}",
        **extra,
    }


def test_ingest_keeps_planes_and_phenomena_not_planeswalkers():
    plane = extract(_card("o1", "Academy at Tolaria West", "Plane — Dominaria"))
    assert plane is not None and plane["is_momir_legal"] is False
    phenom = extract(_card("o2", "Chaotic Aether", "Phenomenon"))
    assert phenom is not None and phenom["is_momir_legal"] is False
    creature = extract(_card("o3", "Grizzly Bears", "Creature — Bear", cmc=2))
    assert creature is not None and creature["is_momir_legal"] is True
    # Excluded: a planeswalker (leading "Planeswalker", not "Plane ") and a spell.
    assert extract(_card("o4", "Jace, the Mind Sculptor", "Legendary Planeswalker — Jace")) is None
    assert extract(_card("o5", "Lightning Bolt", "Instant")) is None
