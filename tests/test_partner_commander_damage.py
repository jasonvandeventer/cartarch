"""A shared commander-damage counter must not eliminate anyone (#partner-cmd).

`state.cmd[receiver][attacker]` keys on the attacking SEAT. The rules track
commander damage per COMMANDER — 21 from each, separately — so a seat playing
Partners has two commanders on one counter, and its total crossing 21 proves
nothing.

**It has already produced a suspect result.** Game 45 in prod: seat 207 played
*Haldan, Avid Arcanist + Pako, Arcane Retriever* and dealt 24 to seat 206 on one
counter; that seat was auto-eliminated with cause `cmd` on 11 life. Whether that
was right is unknowable — the app never recorded which commander dealt it.

This is the SMALL fix: stop auto-eliminating on those counters and flag them.
Keying per commander is the full fix and needs a state-shape change plus a
table-side attribution step.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

import app.legacy_tables  # noqa
from app.live_game_service import _loss_cause

_TEMPLATE = (
    pathlib.Path(__file__).resolve().parents[1] / "app" / "templates" / "game_detail.html"
).read_text()


def _state(cmd, partners=None, life=40):
    s = {"lives": {"1": life}, "cmd": {"1": cmd}, "extraCounters": {}}
    if partners is not None:
        s["partnerSeats"] = partners
    return s


# --- the server, which is the authority ------------------------------------------


def test_a_single_commander_still_kills_at_21():
    assert _loss_cause(_state({"2": 21}, partners=[]), "1") == "cmd"


def test_a_partner_seats_shared_counter_does_not_kill():
    """THE fix. 24 from a two-commander seat is not 21 from one commander."""
    assert _loss_cause(_state({"2": 24}, partners=["2"]), "1") is None


def test_a_partner_seat_does_not_shield_a_second_attacker():
    """The partner seat is skipped; a normal attacker at 21 still kills."""
    assert _loss_cause(_state({"2": 24, "3": 21}, partners=["2"]), "1") == "cmd"


def test_a_blob_without_the_key_behaves_exactly_as_before():
    """An in-flight game started before this change has no `partnerSeats`. It
    must keep the old behaviour rather than silently stop eliminating."""
    assert _loss_cause(_state({"2": 21}), "1") == "cmd"


def test_life_still_wins_the_race():
    """Order is life → poison → cmd; the cmd change must not reorder it."""
    assert _loss_cause(_state({"2": 24}, partners=["2"], life=0), "1") == "life"


# --- multi_commander_seat_ids ----------------------------------------------------


def test_multi_commander_seats_are_detected_through_the_anchor(db, user):
    """Counts via deck_commander_cards, so a deck whose commanders live on the
    #163 anchor rather than tagged rows still counts (the v4.12.40 lesson)."""
    from types import SimpleNamespace

    from app import deck_service
    from app.game_service import multi_commander_seat_ids
    from app.models import Card, DeckCommander

    solo = deck_service.create_deck(db, user.id, "Solo")
    duo = deck_service.create_deck(db, user.id, "Partners")
    for i, deck in ((1, solo), (2, duo), (3, duo)):
        card = Card(
            name=f"Cmdr {i}",
            scryfall_id=f"sf-partner-{i}",
            set_code="tst",
            set_name="T",
            collector_number=str(i),
            rarity="rare",
        )
        db.add(card)
        db.flush()
        db.add(DeckCommander(deck_id=deck.id, card_id=card.id))
    db.commit()

    game = SimpleNamespace(
        seats=[
            SimpleNamespace(id=10, deck=solo),
            SimpleNamespace(id=11, deck=duo),
            SimpleNamespace(id=12, deck=None),  # guest seat — no deck, no crash
        ]
    )
    assert multi_commander_seat_ids(db, game) == {11}


# --- the two client mirrors, executed --------------------------------------------

pytestmark_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract(name: str) -> str:
    start = _TEMPLATE.index(f"function {name}(")
    depth = 0
    for i in range(_TEMPLATE.index("{", start), len(_TEMPLATE)):
        if _TEMPLATE[i] == "{":
            depth += 1
        elif _TEMPLATE[i] == "}":
            depth -= 1
            if depth == 0:
                return _TEMPLATE[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _run_client(fn, cmd, partners, life=40):
    js = f"""
const PARTNER_SEAT_IDS = new Set({json.dumps(partners)}.map(String));
let state = {
        json.dumps(
            {
                "lives": {"1": life},
                "cmd": {"1": cmd},
                "extraCounters": {"1": []},
                "eliminated": {},
                "eliminatedAtTurn": {},
                "eliminationCause": {},
                "turn": 1,
            }
        )
    };
function getCounters(sid) {{ return state.extraCounters[sid] || []; }}
{_extract(fn)}
console.log(JSON.stringify({{
  cause: {
        "liveLossCause('1')"
        if fn == "liveLossCause"
        else "(checkElimination('1'), state.eliminated['1'] || false)"
    }
}}));
"""
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["cause"]


@pytestmark_node
def test_live_client_mirror_skips_partner_seats():
    assert _run_client("liveLossCause", {"2": 24}, partners=["2"]) is None
    assert _run_client("liveLossCause", {"2": 24}, partners=[]) == "cmd"


@pytestmark_node
def test_local_tracker_skips_partner_seats():
    assert _run_client("checkElimination", {"2": 24}, partners=["2"]) is False
    assert _run_client("checkElimination", {"2": 24}, partners=[]) is True


# --- the FULL fix: per-commander source keys (v4.13.21) ---------------------------
#
# A source key is "<seat>" (one commander, or none) or "<seat>:<card>" (one per
# commander of a partner seat). Partners land in SEPARATE map entries, so
# `max(values) >= 21` — unchanged arithmetic — stops summing two commanders.
# The rule needs no special case; the KEY carries the correctness.


def test_damage_sources_split_a_partner_seat_and_leave_the_rest_alone(db, user):
    from types import SimpleNamespace

    from app import deck_service
    from app.game_service import seat_damage_sources
    from app.models import Card, DeckCommander

    solo = deck_service.create_deck(db, user.id, "Solo2")
    duo = deck_service.create_deck(db, user.id, "Partners2")
    ids = {}
    for i, deck in ((1, solo), (2, duo), (3, duo)):
        card = Card(
            name=f"Cmdr{i}",
            scryfall_id=f"sf-src-{i}",
            set_code="tst",
            set_name="T",
            collector_number=str(i),
            rarity="rare",
        )
        db.add(card)
        db.flush()
        ids[i] = card.id
        db.add(DeckCommander(deck_id=deck.id, card_id=card.id))
    db.commit()

    game = SimpleNamespace(
        seats=[
            SimpleNamespace(id=10, deck=solo, player_name="Solo Sam", seat_number=1),
            SimpleNamespace(id=11, deck=duo, player_name="Duo Dana", seat_number=2),
            SimpleNamespace(id=12, deck=None, player_name="Guest", seat_number=3),
        ]
    )
    sources = seat_damage_sources(db, game)

    assert [s["key"] for s in sources["10"]] == ["10"], "one commander keeps the bare seat key"
    assert [s["key"] for s in sources["12"]] == ["12"], "no deck falls back, never blocks"
    assert sorted(s["key"] for s in sources["11"]) == sorted([f"11:{ids[2]}", f"11:{ids[3]}"]), (
        "a partner seat splits per commander"
    )
    assert sorted(s["label"] for s in sources["11"]) == ["Cmdr2", "Cmdr3"]


def test_two_partners_below_21_each_no_longer_kill():
    """THE point of the whole change: 13 + 11 is 24 on the table and lethal to
    nobody. Under the old seat keying this was a dead player."""
    assert _loss_cause(_state({"2:100": 13, "2:101": 11}), "1") is None


def test_one_partner_reaching_21_still_kills():
    assert _loss_cause(_state({"2:100": 21, "2:101": 3}), "1") == "cmd"


def test_an_explicit_source_key_is_honoured_and_an_unknown_one_is_not():
    from app.live_game_service import _resolve_cmd_source

    state = {"cmdSources": {"2": [{"key": "2:100"}, {"key": "2:101"}]}}
    assert _resolve_cmd_source(state, {"attacker_seat_id": 2, "attacker_source_key": "2:101"}) == (
        "2:101"
    )
    # Forged/stale key → the bare seat, never a private counter no UI shows.
    assert (
        _resolve_cmd_source(state, {"attacker_seat_id": 2, "attacker_source_key": "2:999"}) == "2"
    )
    assert _resolve_cmd_source(state, {"attacker_seat_id": 2}) == "2"


def test_the_matrix_totals_the_seat_but_flags_lethal_per_commander(db):
    """A matrix cell means "how much has this SEAT dealt me"; lethality is per
    COMMANDER. Driven through build_game_analytics with a real partner seat —
    an earlier version of this test re-implemented the sum/max in the test body
    and proved nothing about the code.
    """
    import json as _json

    from app import live_game_service
    from app.game_analytics_service import build_game_analytics
    from app.game_service import end_game
    from app.models import Game, GameSeat, User

    owner = User(username="matrix@example.com", password_hash="x")
    db.add(owner)
    db.flush()
    game = Game(user_id=owner.id, format="Commander", status="created", client_token="tok-matrix")
    db.add(game)
    db.flush()
    seats = []
    for i in range(1, 4):
        seat = GameSeat(game_id=game.id, seat_number=i, player_name=f"P{i}", starting_life=40)
        db.add(seat)
        seats.append(seat)
    db.flush()
    s1, s2, s3 = seats

    live_game_service.start_live_game(db, game.id, owner.id)

    # Make s3 a partner seat by hand: two sources on one seat.
    live = game.live_state
    st = _json.loads(live.state)
    st["cmdSources"][str(s3.id)] = [
        {"key": f"{s3.id}:100", "label": "Haldan", "card_id": 100},
        {"key": f"{s3.id}:101", "label": "Pako", "card_id": 101},
    ]
    live.state = _json.dumps(st)
    db.commit()

    def act(action):
        return live_game_service.apply_live_action(db, game.id, owner.id, action, "tok-matrix")

    # 13 from one commander, 11 from the other: 24 total, lethal to nobody.
    for key, dmg in ((f"{s3.id}:100", 13), (f"{s3.id}:101", 11)):
        act(
            {
                "type": "cmd",
                "receiver_seat_id": s1.id,
                "attacker_seat_id": s3.id,
                "attacker_source_key": key,
                "delta": dmg,
            }
        )

    assert not _json.loads(game.live_state.state)["eliminated"].get(str(s1.id)), (
        "24 split across two commanders must not eliminate anyone"
    )

    end_game(
        db,
        game.id,
        owner.id,
        placements={s2.id: 1, s1.id: 2, s3.id: 3},
        final_lives={s1.id: 16, s2.id: 40, s3.id: 40},
        turn_count=1,
        notes="",
    )

    matrix = build_game_analytics(db, game.id)["cmd_matrix"]
    row = next(r for r in matrix["rows"] if r["sid"] == str(s1.id))
    col = [c["sid"] for c in matrix["columns"]].index(str(s3.id))
    cell = row["cells"][col]
    assert cell["value"] == 24, "the cell shows the seat total"
    assert cell["lethal"] is False, "but 13 and 11 are each below 21"
    assert matrix["any"] is True


# --- the FOURTH elimination path: the phone (v4.13.20 missed it) ------------------


def _companion_src() -> str:
    text = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "templates" / "game_companion.html"
    ).read_text()
    start = text.index("function cmpLossCause(")
    depth = 0
    for i in range(text.index("{", start), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError("unbalanced braces extracting cmpLossCause")


def _run_companion(cmd, partner_seats, life=40):
    js = f"""
const st = {
        json.dumps(
            {
                "lives": {"1": life},
                "cmd": {"1": cmd},
                "extraCounters": {"1": []},
                "partnerSeats": partner_seats,
            }
        )
    };
function K(x) {{ return String(x); }}
{_companion_src()}
console.log(JSON.stringify({{cause: cmpLossCause(1)}}));
"""
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["cause"]


@pytestmark_node
def test_the_phone_stops_eliminating_on_a_shared_counter():
    """v4.13.20 fixed the server and two clients and MISSED this one, so a
    partner-seat player saw a false 'eliminated' until the SSE echo corrected it."""
    assert _run_companion({"2": 24}, partner_seats=["2"]) is None
    assert _run_companion({"2": 24}, partner_seats=[]) == "cmd"


@pytestmark_node
def test_the_phone_reads_per_commander_keys():
    """New-shape blob: two partners below 21 each kill nobody; one at 21 does."""
    assert _run_companion({"2:100": 13, "2:101": 11}, partner_seats=[]) is None
    assert _run_companion({"2:100": 21, "2:101": 3}, partner_seats=[]) == "cmd"
