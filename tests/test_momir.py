"""Momir Basic (format="momir") — creature pool query, format-scoped game
creation, and the momir_activate / momir_kill_token live actions.

Commander games must be completely untouched: the tokens state field and the
Momir actions only operate when game.format is Momir."""

from __future__ import annotations

import itertools
import json

import pytest

from app import live_game_service
from app.game_service import create_game
from app.live_game_service import random_creature_at_cmc
from app.models import Card, Game, GameEvent, GameSeat, User

_seq = itertools.count(1)


def _user(db, name=None) -> User:
    u = User(username=(name or f"u{next(_seq)}@ex.com"), password_hash="x")
    db.add(u)
    db.flush()
    return u


def _creature(db, name, cmc, *, power="2", toughness="2", type_line="Creature — Bear", printings=1):
    """Insert ``printings`` distinct printings (unique scryfall_id/collector) of
    one creature name, all sharing name/cmc/P-T — mirrors real reprints."""
    for _ in range(printings):
        db.add(
            Card(
                scryfall_id=f"sid-{next(_seq)}",
                name=name,
                set_code="tst",
                collector_number=str(next(_seq)),
                type_line=type_line,
                cmc=cmc,
                power=power,
                toughness=toughness,
            )
        )
    db.flush()


def _momir_game(db, owner_id, *, seats, status="created"):
    game = Game(user_id=owner_id, format="Momir", status=status, client_token="TABLE")
    db.add(game)
    db.flush()
    seat_objs = []
    for i, spec in enumerate(seats, start=1):
        s = GameSeat(
            game_id=game.id,
            seat_number=i,
            player_name=f"P{i}",
            user_id=spec.get("user_id"),
            starting_life=spec.get("starting_life", 24),
        )
        db.add(s)
        seat_objs.append(s)
    db.flush()
    return game, seat_objs


def _act(db, game_id, user_id, action, token=None):
    return live_game_service.apply_live_action(db, game_id, user_id, action, token)


def _state(live):
    return json.loads(live.state)


# ── Step 1: random_creature_at_cmc ───────────────────────────────────────────


def test_random_creature_returns_all_fields(db):
    _creature(db, "Grizzly Bears", 2, power="2", toughness="2", type_line="Creature — Bear")
    c = random_creature_at_cmc(db, 2)
    assert c == {
        "name": "Grizzly Bears",
        "power": "2",
        "toughness": "2",
        "type_line": "Creature — Bear",
        "scryfall_id": c["scryfall_id"],  # some printing's id
        "cmc": 2,
    }
    assert c["scryfall_id"].startswith("sid-")


def test_random_creature_none_when_no_creature_at_cmc(db):
    _creature(db, "Grizzly Bears", 2)
    assert random_creature_at_cmc(db, 99) is None
    assert random_creature_at_cmc(db, 7) is None  # nothing at 7 either


def test_random_creature_excludes_tokens_and_noncreatures(db):
    _creature(db, "Bear Token", 0, type_line="Token Creature — Bear")  # Token → excluded
    db.add(
        Card(
            scryfall_id="sid-noncre",
            name="Lightning Bolt",
            set_code="tst",
            collector_number="x1",
            type_line="Instant",
            cmc=0,
        )
    )
    db.flush()
    assert random_creature_at_cmc(db, 0) is None


def test_random_creature_dedups_by_name_and_randomizes(db):
    # Two names at CMC 2, one heavily reprinted. Dedup-by-name means both are
    # reachable regardless of printing count; over many calls both appear.
    _creature(db, "Grizzly Bears", 2, printings=6)
    _creature(db, "Runeclaw Bear", 2, printings=1)
    seen = {random_creature_at_cmc(db, 2)["name"] for _ in range(40)}
    assert seen == {"Grizzly Bears", "Runeclaw Bear"}


# ── Step 2: Momir game creation ──────────────────────────────────────────────


def test_create_momir_game_seats_have_no_decks(db):
    owner = _user(db)
    game = create_game(
        db,
        user_id=owner.id,
        format="Momir",
        seats=[
            {"player_name": "A", "starting_life": 24},
            {"player_name": "B", "starting_life": 24},
        ],
    )
    assert game.format == "Momir"
    assert all(s.deck_id is None for s in game.seats)
    assert all(s.starting_life == 24 for s in game.seats)


def test_route_momir_defaults_life_to_24(db, client, user):
    # CSRF is overridden off in the client fixture; leave starting_life unset so
    # the route's Commander default (40) triggers the Momir → 24 swap.
    r = client.post(
        "/games",
        data={
            "player_count": "2",
            "format": "Momir",
            "player_names": ["A", "B"],
            "deck_ids": ["", ""],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    game = db.query(Game).order_by(Game.id.desc()).first()
    assert game.format == "Momir"
    assert all(s.starting_life == 24 for s in game.seats)


# ── Step 3/4: momir live actions ─────────────────────────────────────────────


@pytest.fixture
def live_momir(db):
    owner = _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": owner.id}, {}])
    live_game_service.start_live_game(db, game.id, owner.id)
    _creature(db, "Grizzly Bears", 3, power="2", toughness="2")
    return {"db": db, "gid": game.id, "uid": owner.id, "seats": seats, "owner": owner}


def test_start_initializes_empty_tokens_for_momir(db):
    owner = _user(db)
    game, _ = _momir_game(db, owner.id, seats=[{}, {}])
    live = live_game_service.start_live_game(db, game.id, owner.id)
    st = _state(live)
    assert st["tokens"] == {} and st["momirTurnUsed"] == {}


def test_momir_activate_adds_token_and_records_event(live_momir):
    L = live_momir
    s = L["seats"][0]
    live = _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s.id, "cmc": 3}, "TABLE"
    )
    toks = _state(live)["tokens"][str(s.id)]
    assert len(toks) == 1
    assert toks[0]["name"] == "Grizzly Bears"
    assert toks[0]["power"] == "2" and toks[0]["toughness"] == "2"
    assert toks[0]["cmc"] == 3 and toks[0]["turn_created"] == 1 and toks[0]["alive"] is True
    ev = (
        L["db"]
        .query(GameEvent)
        .filter_by(game_id=L["gid"], action_type="momir_activate")
        .order_by(GameEvent.id.desc())
        .first()
    )
    payload = json.loads(ev.payload)
    assert payload["cmc"] == 3 and payload["whiff"] is False
    assert payload["creature"]["name"] == "Grizzly Bears"
    assert ev.seat_id == s.id


def test_momir_activate_whiff_records_event_no_token(live_momir):
    L = live_momir
    s = L["seats"][0]
    # CMC 12 has no creatures seeded → whiff (valid action).
    live = _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s.id, "cmc": 12}, "TABLE"
    )
    assert _state(live)["tokens"].get(str(s.id), []) == []
    ev = (
        L["db"]
        .query(GameEvent)
        .filter_by(game_id=L["gid"], action_type="momir_activate")
        .order_by(GameEvent.id.desc())
        .first()
    )
    payload = json.loads(ev.payload)
    assert payload["whiff"] is True and payload["creature"] is None


def test_momir_activate_rejects_out_of_range_cmc(live_momir):
    L = live_momir
    s = L["seats"][0]
    with pytest.raises(ValueError):
        _act(
            L["db"],
            L["gid"],
            L["uid"],
            {"type": "momir_activate", "seat_id": s.id, "cmc": 17},
            "TABLE",
        )
    with pytest.raises(ValueError):
        _act(
            L["db"],
            L["gid"],
            L["uid"],
            {"type": "momir_activate", "seat_id": s.id, "cmc": -1},
            "TABLE",
        )


def test_momir_kill_token_marks_dead(live_momir):
    L = live_momir
    s = L["seats"][0]
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s.id, "cmc": 3}, "TABLE"
    )
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_kill_token", "seat_id": s.id, "index": 0},
        "TABLE",
    )
    assert _state(live)["tokens"][str(s.id)][0]["alive"] is False


def test_momir_kill_token_out_of_range_raises(live_momir):
    L = live_momir
    s = L["seats"][0]
    with pytest.raises(ValueError):
        _act(
            L["db"],
            L["gid"],
            L["uid"],
            {"type": "momir_kill_token", "seat_id": s.id, "index": 0},
            "TABLE",
        )


def test_momir_once_per_turn_rejects_second_activation(live_momir):
    L = live_momir
    s = L["seats"][0]
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s.id, "cmc": 3}, "TABLE"
    )
    with pytest.raises(ValueError):  # second activation same turn -> 400
        _act(
            L["db"],
            L["gid"],
            L["uid"],
            {"type": "momir_activate", "seat_id": s.id, "cmc": 3},
            "TABLE",
        )


def test_momir_quota_resets_when_round_advances(live_momir):
    L = live_momir
    s1, s2 = L["seats"]
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s1.id, "cmc": 3}, "TABLE"
    )
    # Two turn advances in a 2-seat game wrap the round (turn 1 -> 2), back to s1.
    _act(L["db"], L["gid"], L["uid"], {"type": "turn"}, "TABLE")
    _act(L["db"], L["gid"], L["uid"], {"type": "turn"}, "TABLE")
    live = _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s1.id, "cmc": 3}, "TABLE"
    )
    st = _state(live)
    assert st["turn"] == 2
    assert len(st["tokens"][str(s1.id)]) == 2  # allowed again next round


def test_momir_whiff_does_not_consume_the_turn(live_momir):
    L = live_momir
    s = L["seats"][0]
    # CMC 12 has no seeded creature -> whiff, must NOT spend the once-per-turn quota.
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s.id, "cmc": 12}, "TABLE"
    )
    live = _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s.id, "cmc": 3}, "TABLE"
    )
    st = _state(live)
    assert len(st["tokens"][str(s.id)]) == 1  # the real summon still went through
    assert st["momirTurnUsed"][str(s.id)] == st["turn"]


def test_momir_once_per_turn_is_per_seat(live_momir):
    # Each seat gets its own activation; s1 using its turn doesn't block s2.
    L = live_momir
    s1, s2 = L["seats"]
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s1.id, "cmc": 3}, "TABLE"
    )
    live = _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s2.id, "cmc": 3}, "TABLE"
    )
    st = _state(live)
    assert len(st["tokens"][str(s1.id)]) == 1 and len(st["tokens"][str(s2.id)]) == 1


def _seq_of(live):  # seq of the most recently declared pending attack
    return _state(live)["attacks"][-1]["seq"]


def test_momir_attack_declares_pending_then_defender_takes(live_momir):
    L = live_momir
    s1, s2 = L["seats"]  # both start at 24
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s1.id, "cmc": 3}, "TABLE"
    )
    # Attacker DECLARES against the player — no damage yet, just a pending attack.
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id},
        "TABLE",
    )
    st = _state(live)
    assert st["lives"][str(s2.id)] == 24 and len(st["attacks"]) == 1
    seq = st["attacks"][0]["seq"]
    # DEFENDER chooses to take it → loses the attacker's power.
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_resolve", "seat_id": s2.id, "seq": seq},
        "TABLE",
    )
    st = _state(live)
    assert st["lives"][str(s2.id)] == 24 - 2 and st["attacks"] == []


def test_momir_defender_blocks_and_creatures_fight(live_momir):
    L = live_momir
    s1, s2 = L["seats"]
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s1.id, "cmc": 3}, "TABLE"
    )  # 2/2
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s2.id, "cmc": 3}, "TABLE"
    )  # 2/2 blocker
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id},
        "TABLE",
    )
    seq = _seq_of(live)
    # Defender blocks with their own creature → the two fight; no life loss.
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_resolve", "seat_id": s2.id, "seq": seq, "block_index": 0},
        "TABLE",
    )
    st = _state(live)
    assert st["tokens"][str(s1.id)][0]["alive"] is False  # 2 >= 2
    assert st["tokens"][str(s2.id)][0]["alive"] is False
    assert st["lives"][str(s2.id)] == 24 and st["attacks"] == []


def test_momir_taking_lethal_attack_auto_eliminates(db):
    owner = _user(db)
    game, seats = _momir_game(
        db, owner.id, seats=[{"user_id": owner.id, "starting_life": 24}, {"starting_life": 2}]
    )
    live_game_service.start_live_game(db, game.id, owner.id)
    _creature(db, "Colossus", 3, power="5", toughness="5")
    s1, s2 = seats
    _act(db, game.id, owner.id, {"type": "momir_activate", "seat_id": s1.id, "cmc": 3}, "TABLE")
    live = _act(
        db,
        game.id,
        owner.id,
        {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id},
        "TABLE",
    )
    live = _act(
        db,
        game.id,
        owner.id,
        {"type": "momir_resolve", "seat_id": s2.id, "seq": _seq_of(live)},
        "TABLE",
    )
    st = _state(live)
    assert st["lives"][str(s2.id)] == 2 - 5
    assert st["eliminated"][str(s2.id)] is True and st["eliminationCause"][str(s2.id)] == "life"


def test_momir_attacker_declares_defender_resolves_auth(db):
    owner = _user(db)
    a, b = _user(db), _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": a.id}, {"user_id": b.id}])
    live_game_service.start_live_game(db, game.id, owner.id)
    _creature(db, "Grizzly Bears", 3)
    s1, s2 = seats
    _act(db, game.id, a.id, {"type": "momir_activate", "seat_id": s1.id, "cmc": 3})
    # b cannot declare an attack with a's creature (a's seat).
    with pytest.raises(PermissionError):
        _act(
            db,
            game.id,
            b.id,
            {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id},
        )
    # a declares with a's own creature.
    live = _act(
        db,
        game.id,
        a.id,
        {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id},
    )
    seq = _seq_of(live)
    # The ATTACKER cannot make the block decision (that's the defender's seat)...
    with pytest.raises(PermissionError):
        _act(db, game.id, a.id, {"type": "momir_resolve", "seat_id": s2.id, "seq": seq})
    # ...only the defender (b, owns s2) resolves it.
    live = _act(db, game.id, b.id, {"type": "momir_resolve", "seat_id": s2.id, "seq": seq})
    assert _state(live)["attacks"] == []


def test_momir_multiplayer_each_defender_resolves_their_own(db):
    # 4 players; two independent attacks at different targets. Each defender —
    # and ONLY that defender — resolves the attack aimed at them.
    owner = _user(db)
    p1, p2, p3, p4 = _user(db), _user(db), _user(db), _user(db)
    game, seats = _momir_game(
        db,
        owner.id,
        seats=[{"user_id": p1.id}, {"user_id": p2.id}, {"user_id": p3.id}, {"user_id": p4.id}],
    )
    live_game_service.start_live_game(db, game.id, owner.id)
    _creature(db, "Grizzly Bears", 3, power="2", toughness="2")
    s1, s2, s3, s4 = seats
    # p1 activates + attacks p3; p2 activates + attacks p4 (own-seat, no table token).
    _act(db, game.id, p1.id, {"type": "momir_activate", "seat_id": s1.id, "cmc": 3})
    _act(
        db,
        game.id,
        p1.id,
        {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s3.id},
    )
    _act(db, game.id, p2.id, {"type": "momir_activate", "seat_id": s2.id, "cmc": 3})
    live = _act(
        db,
        game.id,
        p2.id,
        {"type": "momir_attack", "seat_id": s2.id, "index": 0, "target_seat_id": s4.id},
    )
    st = _state(live)
    by_target = {a["target_seat"]: a for a in st["attacks"]}
    assert set(by_target) == {str(s3.id), str(s4.id)}  # two independent pending attacks
    seq3 = by_target[str(s3.id)]["seq"]

    # A bystander (p4) cannot resolve the attack aimed at p3...
    with pytest.raises((PermissionError, ValueError)):
        _act(db, game.id, p4.id, {"type": "momir_resolve", "seat_id": s3.id, "seq": seq3})
    # ...only p3 resolves their own (takes 2).
    live = _act(db, game.id, p3.id, {"type": "momir_resolve", "seat_id": s3.id, "seq": seq3})
    st = _state(live)
    assert st["lives"][str(s3.id)] == 24 - 2
    assert st["lives"][str(s4.id)] == 24  # p4's pending attack is untouched
    assert len(st["attacks"]) == 1 and st["attacks"][0]["target_seat"] == str(s4.id)

    # p4 resolves theirs independently.
    live = _act(
        db,
        game.id,
        p4.id,
        {"type": "momir_resolve", "seat_id": s4.id, "seq": st["attacks"][0]["seq"]},
    )
    assert _state(live)["lives"][str(s4.id)] == 24 - 2 and _state(live)["attacks"] == []


def test_momir_attack_self_dead_and_double_declare_rejected(live_momir):
    L = live_momir
    s1, s2 = L["seats"]
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s1.id, "cmc": 3}, "TABLE"
    )
    with pytest.raises(ValueError):  # can't attack yourself
        _act(
            L["db"],
            L["gid"],
            L["uid"],
            {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s1.id},
            "TABLE",
        )
    _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id},
        "TABLE",
    )
    with pytest.raises(ValueError):  # that creature is already attacking
        _act(
            L["db"],
            L["gid"],
            L["uid"],
            {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id},
            "TABLE",
        )


def test_momir_resolve_cancel_and_fizzle_and_turn_clears(live_momir):
    L = live_momir
    s1, s2 = L["seats"]
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s1.id, "cmc": 3}, "TABLE"
    )
    # cancel a mis-declared attack → no life change, cleared.
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id},
        "TABLE",
    )
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_resolve", "seat_id": s2.id, "seq": _seq_of(live), "cancel": True},
        "TABLE",
    )
    assert _state(live)["lives"][str(s2.id)] == 24 and _state(live)["attacks"] == []
    # fizzle: attacker dies before the defender resolves → no damage.
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id},
        "TABLE",
    )
    seq = _seq_of(live)
    _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_kill_token", "seat_id": s1.id, "index": 0},
        "TABLE",
    )
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_resolve", "seat_id": s2.id, "seq": seq},
        "TABLE",
    )
    assert _state(live)["lives"][str(s2.id)] == 24  # fizzled
    # advancing the turn clears any pending attacks (combat ends with the turn).
    _act(
        L["db"], L["gid"], L["uid"], {"type": "momir_activate", "seat_id": s2.id, "cmc": 3}, "TABLE"
    )
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "momir_attack", "seat_id": s2.id, "index": 0, "target_seat_id": s1.id},
        "TABLE",
    )
    assert len(_state(live)["attacks"]) == 1
    live = _act(L["db"], L["gid"], L["uid"], {"type": "turn"}, "TABLE")
    assert _state(live)["attacks"] == []


def test_momir_activate_seat_scoped_auth(db):
    owner = _user(db)
    a, b = _user(db), _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": a.id}, {"user_id": b.id}])
    live_game_service.start_live_game(db, game.id, owner.id)
    _creature(db, "Grizzly Bears", 1)
    s1, s2 = seats
    # player_a controls own seat (no table token).
    _act(db, game.id, a.id, {"type": "momir_activate", "seat_id": s1.id, "cmc": 1})
    # ...but not player_b's seat.
    with pytest.raises(PermissionError):
        _act(db, game.id, a.id, {"type": "momir_activate", "seat_id": s2.id, "cmc": 1})


# ── Constraint: Commander games untouched ────────────────────────────────────


def test_commander_start_has_no_tokens_field(db):
    owner = _user(db)
    game = Game(user_id=owner.id, format="Commander", status="created", client_token="TABLE")
    db.add(game)
    db.flush()
    db.add_all([GameSeat(game_id=game.id, seat_number=i, player_name=f"P{i}") for i in (1, 2)])
    db.flush()
    live = live_game_service.start_live_game(db, game.id, owner.id)
    assert "tokens" not in _state(live)


def test_momir_pages_render(db, client, user):
    # Owner is `user` (client fixture pins get_current_user → user). Seat the
    # owner so the companion page shows the seat-scoped view, and start live.
    game, seats = _momir_game(db, user.id, seats=[{"user_id": user.id}, {}])
    live_game_service.start_live_game(db, game.id, user.id)
    db.commit()

    detail = client.get(f"/games/{game.id}")
    assert detail.status_code == 200
    assert "momir-bar" in detail.text  # activation bar rendered for Momir live
    assert "momir-combat" in detail.text  # tablet combat (declare/block) panel present

    companion = client.get(f"/games/{game.id}/companion")
    assert companion.status_code == 200
    assert "cmp-momir" in companion.text  # phone activation section rendered
    assert "cmp-incoming" in companion.text  # defender block-decision section present


def test_commander_rejects_momir_activate(db):
    owner = _user(db)
    game = Game(user_id=owner.id, format="Commander", status="created", client_token="TABLE")
    db.add(game)
    db.flush()
    seat = GameSeat(game_id=game.id, seat_number=1, player_name="P1", user_id=owner.id)
    db.add(seat)
    db.flush()
    live_game_service.start_live_game(db, game.id, owner.id)
    with pytest.raises(ValueError):
        _act(
            db, game.id, owner.id, {"type": "momir_activate", "seat_id": seat.id, "cmc": 2}, "TABLE"
        )
