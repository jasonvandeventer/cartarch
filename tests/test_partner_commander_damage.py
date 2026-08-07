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
