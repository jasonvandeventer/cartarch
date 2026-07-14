"""#116 — Archenemy overlay on companion mode (scheme deck).

Service-level: scheme pool, the enable / set-in-motion / abandon actions through
the real ``apply_live_action`` pipeline (auth included), and the ingest keeping
schemes.
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


def _scheme(db, name, *, ongoing=False) -> OracleCatalog:
    s = OracleCatalog(
        oracle_id=f"oid-{next(_seq)}",
        name=name,
        type_line="Ongoing Scheme" if ongoing else "Scheme",
        oracle_text=f"{name}: do a bad thing.",
        scryfall_id=f"sid-{next(_seq)}",
        is_momir_legal=False,
    )
    db.add(s)
    db.flush()
    return s


def _game(db, owner_id, seat_user_id):
    game = Game(user_id=owner_id, format="Commander", status="created", client_token=TABLE)
    db.add(game)
    db.flush()
    seat = GameSeat(
        game_id=game.id, seat_number=1, player_name="P1", user_id=seat_user_id, starting_life=40
    )
    db.add(seat)
    db.flush()
    return game, seat


def _state(live):
    return json.loads(live.state)


def test_enable_designates_seat_and_shuffles(db):
    owner = _user(db)
    for i in range(3):
        _scheme(db, f"Scheme {i}")
    db.commit()
    game, seat = _game(db, owner.id, owner.id)
    lgs.start_live_game(db, game.id, owner.id)
    assert "archenemy" not in _state(game.live_state)  # untouched until enabled

    live = lgs.apply_live_action(
        db, game.id, owner.id, {"type": "archenemy_enable", "seat_id": seat.id}, TABLE
    )
    st = _state(live)
    assert st["archenemy"] is True
    assert st["archenemySeatId"] == seat.id
    assert len(st["schemeDeck"]) == 3
    assert st["schemeCurrent"] is None  # not revealed until set-in-motion
    assert st["ongoingSchemes"] == []


def test_set_in_motion_ongoing_accumulates_nonongoing_discards(db):
    owner = _user(db)
    for i in range(3):
        _scheme(db, f"Ongoing {i}", ongoing=True)
    db.commit()
    game, seat = _game(db, owner.id, owner.id)
    lgs.start_live_game(db, game.id, owner.id)
    lgs.apply_live_action(
        db, game.id, owner.id, {"type": "archenemy_enable", "seat_id": seat.id}, TABLE
    )

    live = lgs.apply_live_action(db, game.id, owner.id, {"type": "scheme_set_in_motion"}, TABLE)
    st = _state(live)
    assert st["schemeCurrent"] is not None and st["schemeCurrent"]["ongoing"] is True
    assert len(st["ongoingSchemes"]) == 1  # ongoing parks in the persistent list
    first = st["schemeCurrent"]["id"]

    live = lgs.apply_live_action(db, game.id, owner.id, {"type": "scheme_set_in_motion"}, TABLE)
    st = _state(live)
    assert len(st["ongoingSchemes"]) == 2  # the ongoing one persisted, no discard
    assert st["schemeDiscard"] == []

    # Manually abandon the first ongoing scheme → to discard, off the board.
    live = lgs.apply_live_action(
        db, game.id, owner.id, {"type": "scheme_abandon", "scheme_id": first}, TABLE
    )
    st = _state(live)
    assert first not in [s["id"] for s in st["ongoingSchemes"]]
    assert first in st["schemeDiscard"]


def test_nonongoing_scheme_discards_on_next(db):
    owner = _user(db)
    _scheme(db, "Plain A")
    _scheme(db, "Plain B")
    db.commit()
    game, seat = _game(db, owner.id, owner.id)
    lgs.start_live_game(db, game.id, owner.id)
    lgs.apply_live_action(
        db, game.id, owner.id, {"type": "archenemy_enable", "seat_id": seat.id}, TABLE
    )
    live = lgs.apply_live_action(db, game.id, owner.id, {"type": "scheme_set_in_motion"}, TABLE)
    first = _state(live)["schemeCurrent"]["id"]
    live = lgs.apply_live_action(db, game.id, owner.id, {"type": "scheme_set_in_motion"}, TABLE)
    st = _state(live)
    assert st["ongoingSchemes"] == []
    assert first in st["schemeDiscard"]  # the previous non-ongoing was abandoned
    # Deck now empty; a third set-in-motion reshuffles the discard back in.
    live = lgs.apply_live_action(db, game.id, owner.id, {"type": "scheme_set_in_motion"}, TABLE)
    st = _state(live)
    assert st["schemeCurrent"] is not None  # reshuffled, revealed again


def test_set_in_motion_before_enable_rejected(db):
    owner = _user(db)
    _scheme(db, "A")
    db.commit()
    game, _seat = _game(db, owner.id, owner.id)
    lgs.start_live_game(db, game.id, owner.id)
    with pytest.raises(ValueError):
        lgs.apply_live_action(db, game.id, owner.id, {"type": "scheme_set_in_motion"}, TABLE)


def test_authorization_enable_table_only_setmotion_seated(db):
    owner = _user(db)
    player = _user(db)
    for i in range(2):
        _scheme(db, f"S{i}")
    db.commit()
    game, seat = _game(db, owner.id, player.id)
    lgs.start_live_game(db, game.id, owner.id)
    with pytest.raises(PermissionError):
        lgs.apply_live_action(
            db, game.id, player.id, {"type": "archenemy_enable", "seat_id": seat.id}, None
        )
    lgs.apply_live_action(
        db, game.id, owner.id, {"type": "archenemy_enable", "seat_id": seat.id}, TABLE
    )
    live = lgs.apply_live_action(db, game.id, player.id, {"type": "scheme_set_in_motion"}, None)
    assert _state(live)["schemeCurrent"] is not None


def test_ingest_keeps_schemes():
    def card(oid, name, tl):
        return {"oracle_id": oid, "name": name, "type_line": tl, "id": f"s-{oid}"}

    assert extract(card("s1", "All in Fun", "Scheme"))["is_momir_legal"] is False
    assert extract(card("s2", "Mind-Controlling", "Ongoing Scheme"))["is_momir_legal"] is False
