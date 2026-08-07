"""The local (non-live) game tracker's turn rotation — executed, not grepped.

Two long-standing Known Problems, both real and both mis-described:

* *"`nextTurn()` ignores `firstSeatId`"* — it honours it for the STARTING seat
  (set at init) but not for the ROUND BOUNDARY. It incremented `state.turn` on
  wrapping to `clockwiseSeats[0]`, so a host who picked a first seat that is not
  the clockwise-first (v4.12.26 made that reachable) got the round ticking over
  at the wrong player. The round count is submitted at finalize.
* *"`undoLast` desyncs"* — it looked the seat up in the UNFILTERED
  `clockwiseSeats` while `nextTurn` had indexed the eliminated-FILTERED list, so
  after any elimination the two disagreed.

Root cause of both: turn rotation was re-derived in THREE places on the client
and only the live one was right — the exact thing v4.12.4 forbids server-side.

The functions are inline in a Jinja template, so this extracts them by brace
matching and runs them under Node with stubs. Grepping the source would pin the
shape and prove nothing about the arithmetic, which is where the bugs were.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

_TEMPLATE = (
    pathlib.Path(__file__).resolve().parents[1] / "app" / "templates" / "game_detail.html"
).read_text()

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract(name: str) -> str:
    """The full source of `function <name>(...) { ... }` by brace matching."""
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


_HARNESS = """
// --- stubs for everything the extracted functions touch ---
let clockwiseSeats = SEATS;
let firstSeatId = FIRST;
let state = STATE;
const LIVE = false;
function pushHistory(e) { (state.history = state.history || []).push(e); }
function save() {}
function render() {}
function postAction() {}
function getCounters() { return []; }
function liveAdvanceTurnOptimistic() {}

FUNCS

const out = [];
for (const op of OPS) {
  if (op === 'next') nextTurn(); else undoLast();
  out.push({turn: state.turn, current: state.currentTurnId});
}
console.log(JSON.stringify(out));
"""


def _run(seats, first, eliminated, ops, start_seat=None):
    funcs = "\n".join(
        _extract(n) for n in ("rotationIds", "advanceInRotation", "nextTurn", "undoLast")
    )
    state = {
        "turn": 1,
        "currentTurnId": start_seat if start_seat is not None else first,
        "eliminated": eliminated,
        "history": [],
        "lives": {},
        "cmd": {},
    }
    js = (
        _HARNESS.replace("SEATS", json.dumps([{"id": s} for s in seats]))
        .replace("FIRST", json.dumps(first))
        .replace("STATE", json.dumps(state))
        .replace("FUNCS", funcs)
        .replace("OPS", json.dumps(ops))
    )
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_round_ticks_over_at_the_first_seat_not_the_clockwise_first():
    """Seats 1..4 clockwise, host chose seat 3 to start. The round must increment
    returning to 3 — NOT passing 1, which is what the old index arithmetic did."""
    out = _run([1, 2, 3, 4], first=3, eliminated={}, ops=["next"] * 4)
    assert [o["current"] for o in out] == [4, 1, 2, 3]
    # turn stays 1 across 4→1→2, then becomes 2 on returning to the first seat.
    assert [o["turn"] for o in out] == [1, 1, 1, 2]


def test_the_clockwise_first_case_still_behaves():
    """Control: when the first seat IS clockwiseSeats[0], old and new agree."""
    out = _run([1, 2, 3, 4], first=1, eliminated={}, ops=["next"] * 4)
    assert [o["current"] for o in out] == [2, 3, 4, 1]
    assert [o["turn"] for o in out] == [1, 1, 1, 2]


def test_eliminated_seats_are_skipped():
    out = _run([1, 2, 3, 4], first=1, eliminated={"2": True}, ops=["next"] * 3)
    assert [o["current"] for o in out] == [3, 4, 1]
    assert [o["turn"] for o in out] == [1, 1, 2]


def test_undo_after_an_elimination_restores_the_round():
    """THE desync. With seat 2 out, advancing 4→1 wraps (round 2); undo must put
    it back to 1. The old code indexed the unfiltered list and got this wrong."""
    out = _run([1, 2, 3, 4], first=1, eliminated={"2": True}, ops=["next", "next", "next", "undo"])
    assert out[2] == {"turn": 2, "current": 1}, "advance to the first seat should open round 2"
    assert out[3] == {"turn": 1, "current": 4}, "undo should give the round back"


def test_undo_of_a_non_wrapping_advance_leaves_the_round_alone():
    out = _run([1, 2, 3, 4], first=1, eliminated={}, ops=["next", "undo"])
    assert out[1] == {"turn": 1, "current": 1}


def test_undo_does_not_give_back_a_round_the_advance_never_took():
    """THE undo desync, isolated.

    First seat 3, so the rotation is 3,4,1,2. Play a full lap into round 2, then
    advance 4→1 — which does NOT wrap. The old undo asked
    `clockwiseSeats.findIndex(seat 1) == 0` and decremented on that alone, so it
    handed back a round the advance never took. The earlier elimination case
    could not catch this: there the advanced-to seat really was both index 0 and
    a genuine wrap, so right and wrong answers coincided (and `Math.max(1, …)`
    hid it at round 1).
    """
    ops = ["next"] * 6 + ["undo"]  # 3→4→1→2→3(round 2)→4→1, then undo
    out = _run([1, 2, 3, 4], first=3, eliminated={}, ops=ops)
    assert out[3] == {"turn": 2, "current": 3}, "returning to the first seat opens round 2"
    assert out[5] == {"turn": 2, "current": 1}, "4→1 is mid-lap, not a wrap"
    assert out[6] == {"turn": 2, "current": 4}, "undo must not decrement a non-wrapping advance"


def test_everyone_eliminated_is_a_no_op_not_a_crash():
    out = _run([1, 2], first=1, eliminated={"1": True, "2": True}, ops=["next"])
    assert out[0]["current"] == 1
