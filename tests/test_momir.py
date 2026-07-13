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
from app.models import Game, GameEvent, GameLiveState, GameSeat, OracleCatalog, User

_seq = itertools.count(1)


def _user(db, name=None) -> User:
    u = User(username=(name or f"u{next(_seq)}@ex.com"), password_hash="x")
    db.add(u)
    db.flush()
    return u


def _creature(
    db,
    name,
    cmc,
    *,
    power="2",
    toughness="2",
    type_line="Creature — Bear",
    keywords="[]",
    is_momir_legal=True,
):
    """Insert one oracle_catalog row (Momir Sim #109 — the creature source moved
    off the collection-bounded ``cards`` table to a one-row-per-name catalog)."""
    live_game_service.invalidate_valid_mvs()  # keep the memo honest across seeds
    db.add(
        OracleCatalog(
            oracle_id=f"oid-{next(_seq)}",
            name=name,
            cmc=cmc,
            type_line=type_line,
            power=power,
            toughness=toughness,
            keywords=keywords,
            scryfall_id=f"sid-{next(_seq)}",
            is_momir_legal=is_momir_legal,
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


def _start(db, game, user_id, *, fund=True):
    """Start live mode and (by default) fund every seat with lands/untapped so the
    #110 activation cost (untapped >= cmc, hand >= 1) doesn't block pre-#110 tests.
    Lands are set too — untap resets untapped = lands each turn."""
    live = live_game_service.start_live_game(db, game.id, user_id)
    if fund:
        st = json.loads(live.state)
        for s in game.seats:
            st["lands"][str(s.id)] = 30
            st["untapped"][str(s.id)] = 30
        live.state = json.dumps(st)
        db.flush()
    return live


def _live_state(db, game_id):
    return json.loads(db.query(GameLiveState).filter_by(game_id=game_id).one().state)


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
        "keywords": [],  # #111 — carried onto the token at summon
        "oracle_text": None,  # #112 — carried onto the token (unset in this seed)
    }
    assert c["scryfall_id"].startswith("sid-")


def test_random_creature_none_when_no_creature_at_cmc(db):
    _creature(db, "Grizzly Bears", 2)
    assert random_creature_at_cmc(db, 99) is None
    assert random_creature_at_cmc(db, 7) is None  # nothing at 7 either


def test_random_creature_excludes_non_legal(db):
    # The pool query trusts the precomputed is_momir_legal flag (token/vintage/set
    # exclusions live in the ingest now, not the query). A non-legal row at a CMC
    # with no legal creature → whiff.
    _creature(db, "Un-Set Thing", 0, is_momir_legal=False)
    assert random_creature_at_cmc(db, 0) is None


def test_random_creature_randomizes_across_names(db):
    # One row per name (no printing dedup). Two names at CMC 2 → over many calls
    # both are reachable.
    _creature(db, "Grizzly Bears", 2)
    _creature(db, "Runeclaw Bear", 2)
    seen = {random_creature_at_cmc(db, 2)["name"] for _ in range(40)}
    assert seen == {"Grizzly Bears", "Runeclaw Bear"}


def test_valid_momir_mvs_reports_populated_cmcs(db):
    _creature(db, "One Drop", 1)
    _creature(db, "Three Drop", 3)
    _creature(db, "Banned Thing", 5, is_momir_legal=False)  # excluded from the set
    assert live_game_service.valid_momir_mvs(db) == {1, 3}


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
    _start(db, game, owner.id)
    # Haste so a creature summoned this turn can attack this turn — most combat
    # tests declare on the summon turn (#111 summoning sickness otherwise blocks).
    _creature(db, "Grizzly Bears", 3, power="2", toughness="2", keywords='["Haste"]')
    return {"db": db, "gid": game.id, "uid": owner.id, "seats": seats, "owner": owner}


def test_start_initializes_empty_tokens_for_momir(db):
    owner = _user(db)
    game, _ = _momir_game(db, owner.id, seats=[{}, {}])
    live = _start(db, game, owner.id)
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
    _start(db, game, owner.id)
    _creature(db, "Colossus", 3, power="5", toughness="5", keywords='["Haste"]')
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
    _start(db, game, owner.id)
    _creature(db, "Grizzly Bears", 3, keywords='["Haste"]')
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
    _start(db, game, owner.id)
    _creature(db, "Grizzly Bears", 3, power="2", toughness="2", keywords='["Haste"]')
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


def test_momir_resolve_cancel_and_fizzle_and_turn_clears(db):
    owner = _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": owner.id}, {}])
    _start(db, game, owner.id)
    # Haste (can attack the turn it's summoned) + Vigilance (declaring doesn't tap,
    # so the same creature can be re-declared after a cancel/revive).
    _creature(db, "Serra Angel", 3, power="4", toughness="4", keywords='["Haste", "Vigilance"]')
    s1, s2 = seats

    def act(a):
        return _act(db, game.id, owner.id, a, "TABLE")

    act({"type": "momir_activate", "seat_id": s1.id, "cmc": 3})
    # cancel a mis-declared attack → no life change, cleared.
    live = act({"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id})
    live = act({"type": "momir_resolve", "seat_id": s2.id, "seq": _seq_of(live), "cancel": True})
    assert _state(live)["lives"][str(s2.id)] == 24 and _state(live)["attacks"] == []
    # fizzle: attacker dies before the defender resolves → no damage.
    live = act({"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id})
    seq = _seq_of(live)
    act({"type": "momir_kill_token", "seat_id": s1.id, "index": 0})
    live = act({"type": "momir_resolve", "seat_id": s2.id, "seq": seq})
    assert _state(live)["lives"][str(s2.id)] == 24  # fizzled
    # revive the (mistakenly killed) attacker, re-declare, then advance the turn →
    # pending attacks clear (combat ends with the turn). Also exercises revive.
    act({"type": "momir_revive_token", "seat_id": s1.id, "index": 0})
    live = act({"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id})
    assert len(_state(live)["attacks"]) == 1
    assert _state(live)["tokens"][str(s1.id)][0]["alive"] is True  # revived
    live = act({"type": "turn"})
    assert _state(live)["attacks"] == []


def test_momir_activate_seat_scoped_auth(db):
    owner = _user(db)
    a, b = _user(db), _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": a.id}, {"user_id": b.id}])
    _start(db, game, owner.id)
    _creature(db, "Grizzly Bears", 1)
    s1, s2 = seats
    # player_a controls own seat (no table token).
    _act(db, game.id, a.id, {"type": "momir_activate", "seat_id": s1.id, "cmc": 1})
    # ...but not player_b's seat.
    with pytest.raises(PermissionError):
        _act(db, game.id, a.id, {"type": "momir_activate", "seat_id": s2.id, "cmc": 1})


# ── Phase 2 (#110): resource layer — mana / hand / library ───────────────────


def test_momir_start_draws_first_player_only(db):
    owner = _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": owner.id}, {}])
    live = _start(db, game, owner.id, fund=False)
    st = _state(live)
    s1, s2 = seats
    # Every player draws on their first turn (incl. the starter) — but only the
    # starter's first turn has begun at game start.
    assert st["hand"][str(s1.id)] == 8 and st["library"][str(s1.id)] == 59
    assert st["hand"][str(s2.id)] == 7 and st["library"][str(s2.id)] == 60
    assert st["lands"][str(s1.id)] == 0 and st["landPlayed"][str(s1.id)] is False


def test_momir_turn_advance_untaps_draws_and_resets_land(db):
    owner = _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": owner.id}, {}])
    _start(db, game, owner.id, fund=False)
    s1, s2 = seats
    sid = str(s2.id)
    # s2 mid-"tap" state: 5 lands but only 2 untapped, a small library, a tapped
    # token, and a used land drop — poke the blob directly for the tapped token.
    _act(
        db,
        game.id,
        owner.id,
        {"type": "momir_adjust", "seat_id": s2.id, "lands": 5, "untapped": 2, "library": 10},
        "TABLE",
    )
    live = db.query(GameLiveState).filter_by(game_id=game.id).one()
    st = json.loads(live.state)
    st["tokens"][sid] = [
        {"name": "x", "power": "1", "toughness": "1", "tapped": True, "alive": True}
    ]
    st["landPlayed"][sid] = True
    live.state = json.dumps(st)
    db.flush()

    live = _act(db, game.id, owner.id, {"type": "turn"}, "TABLE")  # passes to s2
    st = _state(live)
    assert st["untapped"][sid] == 5  # untapped = lands
    assert st["landPlayed"][sid] is False  # land drop reset
    assert st["tokens"][sid][0]["tapped"] is False  # creatures untapped
    assert st["hand"][sid] == 8 and st["library"][sid] == 9  # drew one


def test_momir_play_land(live_momir):
    L = live_momir
    db, gid, uid = L["db"], L["gid"], L["uid"]
    s = L["seats"][0]
    sid = str(s.id)
    st0 = _live_state(db, gid)
    lands0, un0, hand0 = st0["lands"][sid], st0["untapped"][sid], st0["hand"][sid]
    _act(db, gid, uid, {"type": "momir_play_land", "seat_id": s.id}, "TABLE")
    st = _live_state(db, gid)
    assert st["lands"][sid] == lands0 + 1 and st["untapped"][sid] == un0 + 1
    assert st["hand"][sid] == hand0 - 1 and st["landPlayed"][sid] is True
    with pytest.raises(ValueError):  # one land per turn
        _act(db, gid, uid, {"type": "momir_play_land", "seat_id": s.id}, "TABLE")
    # empty hand → no card to play (use s2, land drop still available)
    s2 = L["seats"][1]
    _act(db, gid, uid, {"type": "momir_adjust", "seat_id": s2.id, "hand": 0}, "TABLE")
    with pytest.raises(ValueError):
        _act(db, gid, uid, {"type": "momir_play_land", "seat_id": s2.id}, "TABLE")


def test_momir_activate_cost_math(live_momir):
    L = live_momir
    db, gid, uid = L["db"], L["gid"], L["uid"]
    s = L["seats"][0]
    sid = str(s.id)
    st0 = _live_state(db, gid)
    un0, hand0 = st0["untapped"][sid], st0["hand"][sid]
    _act(db, gid, uid, {"type": "momir_activate", "seat_id": s.id, "cmc": 3}, "TABLE")
    st = _live_state(db, gid)
    assert st["untapped"][sid] == un0 - 3  # tapped {3} mana
    assert st["hand"][sid] == hand0 - 1  # discarded a card


def test_momir_activate_requires_mana_and_a_card(live_momir):
    L = live_momir
    db, gid, uid = L["db"], L["gid"], L["uid"]
    s = L["seats"][0]
    _act(db, gid, uid, {"type": "momir_adjust", "seat_id": s.id, "untapped": 2}, "TABLE")
    with pytest.raises(ValueError):  # 2 untapped < cmc 3
        _act(db, gid, uid, {"type": "momir_activate", "seat_id": s.id, "cmc": 3}, "TABLE")
    _act(
        db, gid, uid, {"type": "momir_adjust", "seat_id": s.id, "untapped": 10, "hand": 0}, "TABLE"
    )
    with pytest.raises(ValueError):  # no card to discard
        _act(db, gid, uid, {"type": "momir_activate", "seat_id": s.id, "cmc": 3}, "TABLE")


def test_momir_deck_out_eliminates_and_stays_out(db):
    owner = _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": owner.id}, {}])
    _start(db, game, owner.id, fund=False)
    s1, s2 = seats
    sid = str(s2.id)
    _act(db, game.id, owner.id, {"type": "momir_adjust", "seat_id": s2.id, "library": 0}, "TABLE")
    live = _act(db, game.id, owner.id, {"type": "turn"}, "TABLE")  # s2 draws from empty
    st = _state(live)
    assert st["eliminated"][sid] is True and st["eliminationCause"][sid] == "deck"
    ev = (
        db.query(GameEvent)
        .filter_by(game_id=game.id, action_type="eliminate")
        .order_by(GameEvent.id.desc())
        .first()
    )
    assert json.loads(ev.payload) == {"auto": True, "cause": "deck", "eliminated": True}
    # deck-out is permanent — a later life gain must NOT auto-revive it.
    live = _act(db, game.id, owner.id, {"type": "life", "seat_id": s2.id, "delta": 5}, "TABLE")
    assert _state(live)["eliminated"][sid] is True


def test_momir_adjust_is_table_only(db):
    owner = _user(db)
    a = _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": a.id}, {}])
    _start(db, game, owner.id)
    s1, _ = seats
    sid = str(s1.id)
    with pytest.raises(PermissionError):  # a seat player cannot adjust, even own seat
        _act(db, game.id, a.id, {"type": "momir_adjust", "seat_id": s1.id, "library": 10})
    _act(db, game.id, a.id, {"type": "momir_adjust", "seat_id": s1.id, "library": 10}, "TABLE")
    assert _live_state(db, game.id)["library"][sid] == 10
    with pytest.raises(ValueError):  # negatives rejected
        _act(db, game.id, a.id, {"type": "momir_adjust", "seat_id": s1.id, "library": -1}, "TABLE")


# ── Phase 3 (#111): keyword combat engine ────────────────────────────────────


def _tok(power, toughness, keywords=(), **over):
    """A combat-ready token blob. turn_created=0 → never summoning sick."""
    t = {
        "name": "T",
        "power": str(power),
        "toughness": str(toughness),
        "type_line": "Creature",
        "scryfall_id": "sid-x",
        "cmc": 1,
        "turn_created": 0,
        "alive": True,
        "keywords": list(keywords),
        "tapped": False,
        "damage": 0,
        "counters": {"p1p1": 0, "m1m1": 0},
    }
    t.update(over)
    return t


def _combat(db, attacker, blockers, *, atk_life=24, def_life=24):
    """Set up a 2-seat Momir game with an exact attacker (s1[0]) and blockers
    (s2) by poking the blob — sidesteps the once-per-turn summon quota so we can
    place multiple blockers. Returns (act, s1, s2)."""
    owner = _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": owner.id}, {}])
    _start(db, game, owner.id, fund=False)
    s1, s2 = seats
    live = db.query(GameLiveState).filter_by(game_id=game.id).one()
    st = json.loads(live.state)
    st["tokens"][str(s1.id)] = [attacker]
    st["tokens"][str(s2.id)] = list(blockers)
    st["lives"][str(s1.id)] = atk_life
    st["lives"][str(s2.id)] = def_life
    live.state = json.dumps(st)
    db.flush()

    def act(a):
        return _act(db, game.id, owner.id, a, "TABLE")

    return act, s1, s2


def _fight(act, s1, s2, block_indexes=None):
    """Declare s1[0] → s2, then resolve with the given blocks; return final state."""
    live = act({"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id})
    seq = _seq_of(live)
    a = {"type": "momir_resolve", "seat_id": s2.id, "seq": seq}
    if block_indexes is not None:
        a["block_indexes"] = block_indexes
    return _state(act(a))


def test_combat_vanilla_block_trade(db):
    act, s1, s2 = _combat(db, _tok(2, 2), [_tok(2, 2)])
    st = _fight(act, s1, s2, [0])
    assert st["tokens"][str(s1.id)][0]["alive"] is False  # both 2/2 trade
    assert st["tokens"][str(s2.id)][0]["alive"] is False
    assert st["lives"][str(s2.id)] == 24  # blocked → no life loss


def test_combat_unblocked_hits_player(db):
    act, s1, s2 = _combat(db, _tok(3, 3), [_tok(2, 2)])
    st = _fight(act, s1, s2, [])  # choose not to block
    assert st["lives"][str(s2.id)] == 21
    assert st["tokens"][str(s2.id)][0]["alive"] is True


def test_combat_first_strike_kills_before_retaliation(db):
    act, s1, s2 = _combat(db, _tok(2, 2, ["First strike"]), [_tok(2, 2)])
    st = _fight(act, s1, s2, [0])
    assert st["tokens"][str(s1.id)][0]["alive"] is True  # struck first, took none
    assert st["tokens"][str(s2.id)][0]["alive"] is False


def test_combat_first_strike_plus_deathtouch(db):
    act, s1, s2 = _combat(db, _tok(1, 1, ["First strike", "Deathtouch"]), [_tok(3, 3)])
    st = _fight(act, s1, s2, [0])
    assert st["tokens"][str(s1.id)][0]["alive"] is True  # 1 deathtouch damage, first
    assert st["tokens"][str(s2.id)][0]["alive"] is False  # dies to deathtouch


def test_combat_deathtouch_trades_up(db):
    act, s1, s2 = _combat(db, _tok(1, 1, ["Deathtouch"]), [_tok(5, 5)])
    st = _fight(act, s1, s2, [0])
    assert st["tokens"][str(s1.id)][0]["alive"] is False  # takes 5
    assert st["tokens"][str(s2.id)][0]["alive"] is False  # deathtouch


def test_combat_trample_plus_deathtouch_spills(db):
    act, s1, s2 = _combat(db, _tok(5, 5, ["Trample", "Deathtouch"]), [_tok(3, 3)])
    st = _fight(act, s1, s2, [0])
    assert st["tokens"][str(s2.id)][0]["alive"] is False
    assert st["tokens"][str(s1.id)][0]["alive"] is True  # takes 3 < 5
    assert st["lives"][str(s2.id)] == 24 - 4  # 1 lethal (deathtouch), 4 tramples


def test_combat_trample_over_toughness(db):
    act, s1, s2 = _combat(db, _tok(5, 5, ["Trample"]), [_tok(2, 2)])
    st = _fight(act, s1, s2, [0])
    assert st["lives"][str(s2.id)] == 24 - 3  # 2 lethal to blocker, 3 tramples


def test_combat_double_strike_plus_lifelink(db):
    # blocker 1/4 survives the first-strike step, so both steps deal + gain life.
    act, s1, s2 = _combat(db, _tok(2, 2, ["Double strike", "Lifelink"]), [_tok(1, 4)])
    st = _fight(act, s1, s2, [0])
    assert st["tokens"][str(s2.id)][0]["alive"] is False  # 2+2 = 4 >= 4
    assert st["lives"][str(s1.id)] == 24 + 4  # lifelink both steps
    assert st["tokens"][str(s1.id)][0]["alive"] is True  # took 1 < 2


def test_combat_lifelink_unblocked(db):
    act, s1, s2 = _combat(db, _tok(3, 3, ["Lifelink"]), [_tok(2, 2)])
    st = _fight(act, s1, s2, [])
    assert st["lives"][str(s2.id)] == 21 and st["lives"][str(s1.id)] == 27


def test_combat_indestructible_survives_lethal_and_deathtouch(db):
    act, s1, s2 = _combat(db, _tok(2, 2, ["Deathtouch"]), [_tok(2, 2, ["Indestructible"])])
    st = _fight(act, s1, s2, [0])
    assert st["tokens"][str(s2.id)][0]["alive"] is True  # indestructible shrugs deathtouch
    assert st["tokens"][str(s1.id)][0]["alive"] is False  # attacker takes 2, dies


def test_combat_flying_only_blockable_by_flying_or_reach(db):
    act, s1, s2 = _combat(db, _tok(2, 2, ["Flying"]), [_tok(2, 2)])
    with pytest.raises(ValueError):  # ground creature can't block a flyer
        _fight(act, s1, s2, [0])
    act, s1, s2 = _combat(db, _tok(2, 2, ["Flying"]), [_tok(2, 2, ["Reach"])])
    st = _fight(act, s1, s2, [0])
    assert st["tokens"][str(s1.id)][0]["alive"] is False  # reach blocks fine


def test_combat_menace_needs_two_blockers(db):
    act, s1, s2 = _combat(db, _tok(3, 3, ["Menace"]), [_tok(2, 2), _tok(2, 2)])
    with pytest.raises(ValueError):  # single block illegal
        _fight(act, s1, s2, [0])
    act, s1, s2 = _combat(db, _tok(3, 3, ["Menace"]), [_tok(2, 2), _tok(2, 2)])
    st = _fight(act, s1, s2, [0, 1])  # two blockers legal
    assert st["tokens"][str(s1.id)][0]["alive"] is False  # takes 2+2 = 4 >= 3


def test_combat_vigilance_does_not_tap(db):
    act, s1, s2 = _combat(db, _tok(2, 2, ["Vigilance"]), [_tok(2, 2)])
    live = act({"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id})
    assert _state(live)["tokens"][str(s1.id)][0]["tapped"] is False  # vigilance: no tap
    # a non-vigilance attacker taps on declare
    act, s1, s2 = _combat(db, _tok(2, 2), [_tok(2, 2)])
    live = act({"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id})
    assert _state(live)["tokens"][str(s1.id)][0]["tapped"] is True


def test_combat_summoning_sickness_blocks_attack(db):
    sick = _tok(2, 2, turn_created=1)  # summoned this round (turn == 1)
    act, s1, s2 = _combat(db, sick, [_tok(2, 2)])
    with pytest.raises(ValueError):  # sick, no haste
        act({"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id})
    hasty = _tok(2, 2, ["Haste"], turn_created=1)
    act, s1, s2 = _combat(db, hasty, [_tok(2, 2)])
    live = act({"type": "momir_attack", "seat_id": s1.id, "index": 0, "target_seat_id": s2.id})
    assert len(_state(live)["attacks"]) == 1  # haste lets it attack


def test_combat_tapped_creature_cannot_block(db):
    act, s1, s2 = _combat(db, _tok(2, 2), [_tok(2, 2, tapped=True)])
    with pytest.raises(ValueError):
        _fight(act, s1, s2, [0])


# ── Phase 4 (#112): ability primitives ───────────────────────────────────────


def test_momir_damage_token_marks_and_kills(db):
    act, s1, s2 = _combat(db, _tok(2, 2), [_tok(2, 2)])
    sid = str(s1.id)
    st = _state(act({"type": "momir_damage", "seat_id": s1.id, "index": 0, "amount": 1}))
    assert st["tokens"][sid][0]["damage"] == 1 and st["tokens"][sid][0]["alive"] is True
    st = _state(act({"type": "momir_damage", "seat_id": s1.id, "index": 0, "amount": 1}))
    assert st["tokens"][sid][0]["alive"] is False  # 2 >= toughness 2


def test_momir_damage_seat_loses_life_and_auto_elims(db):
    act, s1, s2 = _combat(db, _tok(2, 2), [_tok(2, 2)], def_life=2)
    st = _state(act({"type": "momir_damage", "seat_id": s2.id, "amount": 5}))
    assert st["lives"][str(s2.id)] == 2 - 5
    assert st["eliminated"][str(s2.id)] is True
    assert st["eliminationCause"][str(s2.id)] == "life"


def test_momir_damage_rejects_bad_amount(db):
    act, s1, s2 = _combat(db, _tok(2, 2), [_tok(2, 2)])
    with pytest.raises(ValueError):
        act({"type": "momir_damage", "seat_id": s1.id, "index": 0, "amount": 0})


def test_momir_counter_p1p1_and_m1m1_raw_and_death(db):
    act, s1, s2 = _combat(db, _tok(2, 2), [_tok(2, 2)])
    sid = str(s1.id)
    st = _state(
        act(
            {
                "type": "momir_counter_token",
                "seat_id": s1.id,
                "index": 0,
                "counter": "p1p1",
                "delta": 2,
            }
        )
    )
    assert st["tokens"][sid][0]["counters"] == {"p1p1": 2, "m1m1": 0}
    # m1m1 to 4: eff toughness = 2 + 2 - 4 = 0 → dies. Both maps stay raw (no
    # annihilation of opposing counters).
    st = _state(
        act(
            {
                "type": "momir_counter_token",
                "seat_id": s1.id,
                "index": 0,
                "counter": "m1m1",
                "delta": 4,
            }
        )
    )
    tok = st["tokens"][sid][0]
    assert tok["counters"] == {"p1p1": 2, "m1m1": 4}
    assert tok["alive"] is False


def test_momir_counter_cannot_go_negative(db):
    act, s1, s2 = _combat(db, _tok(2, 2), [_tok(2, 2)])
    st = _state(
        act(
            {
                "type": "momir_counter_token",
                "seat_id": s1.id,
                "index": 0,
                "counter": "p1p1",
                "delta": -5,
            }
        )
    )
    assert st["tokens"][str(s1.id)][0]["counters"]["p1p1"] == 0


def test_momir_tap_token_toggles(db):
    act, s1, s2 = _combat(db, _tok(2, 2), [_tok(2, 2)])
    sid = str(s1.id)
    st = _state(act({"type": "momir_tap_token", "seat_id": s1.id, "index": 0}))
    assert st["tokens"][sid][0]["tapped"] is True
    st = _state(act({"type": "momir_tap_token", "seat_id": s1.id, "index": 0}))
    assert st["tokens"][sid][0]["tapped"] is False


def test_momir_sacrifice_kills_and_logs_distinct_type(db):
    act, s1, s2 = _combat(db, _tok(2, 2), [_tok(2, 2)])
    st = _state(act({"type": "momir_sacrifice", "seat_id": s1.id, "index": 0}))
    assert st["tokens"][str(s1.id)][0]["alive"] is False
    ev = (
        db.query(GameEvent)
        .filter_by(action_type="momir_sacrifice")
        .order_by(GameEvent.id.desc())
        .first()
    )
    assert ev is not None and json.loads(ev.payload)["index"] == 0


def test_momir_primitive_auth_matrix(db):
    # owner is NOT seated (a viewer); a and b hold the two seats.
    owner = _user(db)
    a, b = _user(db), _user(db)
    game, seats = _momir_game(db, owner.id, seats=[{"user_id": a.id}, {"user_id": b.id}])
    _start(db, game, owner.id)
    s1, s2 = seats
    live = db.query(GameLiveState).filter_by(game_id=game.id).one()
    stt = json.loads(live.state)
    stt["tokens"][str(s1.id)] = [_tok(2, 2)]
    stt["tokens"][str(s2.id)] = [_tok(2, 2)]
    live.state = json.dumps(stt)
    db.flush()

    # own board (a → s1): OK, no table token needed.
    _act(db, game.id, a.id, {"type": "momir_tap_token", "seat_id": s1.id, "index": 0})
    # cross board (a → s2): OK, and the acting user is recorded on the event.
    _act(db, game.id, a.id, {"type": "momir_tap_token", "seat_id": s2.id, "index": 0})
    ev = (
        db.query(GameEvent)
        .filter_by(action_type="momir_tap_token", seat_id=s2.id)
        .order_by(GameEvent.id.desc())
        .first()
    )
    assert json.loads(ev.payload)["actor_user_id"] == a.id
    # non-seated viewer (owner): 403.
    with pytest.raises(PermissionError):
        _act(
            db,
            game.id,
            owner.id,
            {"type": "momir_damage", "seat_id": s1.id, "index": 0, "amount": 1},
        )


def test_momir_token_carries_oracle_text(live_momir):
    L = live_momir
    db, gid, uid = L["db"], L["gid"], L["uid"]
    s = L["seats"][0]
    live = _act(db, gid, uid, {"type": "momir_activate", "seat_id": s.id, "cmc": 3}, "TABLE")
    tok = _state(live)["tokens"][str(s.id)][0]
    assert "oracle_text" in tok  # carried from the catalog (None in this seed)


def test_commander_rejects_momir_primitive(db):
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
            db,
            game.id,
            owner.id,
            {"type": "momir_tap_token", "seat_id": seat.id, "index": 0},
            "TABLE",
        )


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


def test_commander_has_no_resource_keys(db):
    owner = _user(db)
    game = Game(user_id=owner.id, format="Commander", status="created", client_token="TABLE")
    db.add(game)
    db.flush()
    db.add_all(
        [
            GameSeat(game_id=game.id, seat_number=i, player_name=f"P{i}", user_id=owner.id)
            for i in (1, 2)
        ]
    )
    db.flush()
    live = live_game_service.start_live_game(db, game.id, owner.id)
    st = _state(live)
    assert not ({"library", "hand", "lands", "untapped", "landPlayed"} & st.keys())
    # advancing the turn must not add any Momir resource keys either
    live = _act(db, game.id, owner.id, {"type": "turn"}, "TABLE")
    st = _state(live)
    assert not ({"library", "hand", "lands", "untapped", "landPlayed"} & st.keys())


def test_momir_pages_render(db, client, user):
    # Owner is `user` (client fixture pins get_current_user → user). Seat the
    # owner so the companion page shows the seat-scoped view, and start live.
    game, seats = _momir_game(db, user.id, seats=[{"user_id": user.id}, {}])
    _start(db, game, user.id)
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
