"""Companion mode — live game state service (issue: tracker-server integration).

The FIRST mid-game server write path. A game in ``created`` status still uses the
localStorage-only tracker exactly as before; live mode is opt-in via
:func:`start_live_game`, which flips the game to ``in_progress`` and creates a
``GameLiveState`` row holding the same JSON blob shape the localStorage tracker
uses (so Session 2 can reuse the client render logic).

Authorization is deliberately split from the rest of the app's owner-only model:

* THE TABLE — a request presenting the game's ``client_token`` (the "table
  token") may control ALL seats. This is the shared tablet running the tracker.
  It is NOT tied to the logged-in user: the creator's phone does not get table
  powers just because the same account is signed in on the tablet.
* PLAYER PHONES — seat-scoped. A user attributed to a seat
  (``GameSeat.user_id``) controls that seat only (cmd is scoped to the RECEIVING
  seat; turn advance is any seated player). The creator without the table token
  is seat-scoped like everyone else.

Read access (:func:`get_live_state`, the SSE stream) is viewer-scoped and NOT
token-gated: seat players and playgroup members may watch.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app import live_game_events
from app.game_service import get_game, get_viewable_game
from app.models import Game, GameEvent, GameLiveState, GameSeat
from app.timeutil import utc_now

_MUTATING_TYPES = {"life", "counter", "cmd", "eliminate", "turn"}

# Physical clockwise seating slots (mirrors game_detail.html's CLOCKWISE). Turn
# rotation follows PHYSICAL seating order, derived from each seat's grid_position
# — NOT seat_number, which is DB/join order and can differ from where players
# actually sit (e.g. the 4-player default layout is p1,p2,p6,p5, so seat_number
# 3/4 sit in clockwise slots 4/3). Seat_number rotation visited seats in the
# wrong physical order (bug: game rotated 1,2,4,3 by badge instead of 1,2,3,4).
_CLOCKWISE = ("p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8")


def _clockwise_index(pos: str | None) -> int:
    """Clockwise slot index for a grid_position; unknown/blank sorts last (stable
    → seat_number order among them)."""
    try:
        return _CLOCKWISE.index(pos)
    except ValueError:
        return len(_CLOCKWISE)


def _clockwise_seats(game: Game) -> list[GameSeat]:
    """Seats in physical clockwise (turn) order — the SAME order the tracker UI
    rotates and the seat badges show. Sort by grid_position's clockwise slot,
    with seat_number as a stable tiebreak so positionless seats keep join order."""
    by_number = sorted(game.seats, key=lambda s: s.seat_number)
    return sorted(by_number, key=lambda s: _clockwise_index(s.grid_position))


def state_payload(live: GameLiveState) -> dict:
    """The wire shape for both the action response and every SSE event:
    ``{"version": N, "state": {...}}``. Full state (not a delta) so a
    reconnecting client self-heals."""
    return {"version": live.version, "state": json.loads(live.state)}


def _publish(live: GameLiveState) -> None:
    live_game_events.publish(live.game_id, json.dumps(state_payload(live)))


# --- state initialization ----------------------------------------------------


def _first_seat_id(game: Game, ordered_seats: list[GameSeat]) -> int | None:
    """Seat id of the starting player (``first_seat_number``), else the first seat
    in ``ordered_seats`` (clockwise order). ``None`` only for a seatless game."""
    if game.first_seat_number is not None:
        match = next((s for s in ordered_seats if s.seat_number == game.first_seat_number), None)
        if match is not None:
            return match.id
    return ordered_seats[0].id if ordered_seats else None


def _initial_state(game: Game) -> dict:
    """The live blob at start — mirrors the localStorage tracker shape. Object
    keys are seat-id STRINGS (JSON coerces them anyway; matches JS render)."""
    seats = _clockwise_seats(game)
    return {
        "lives": {str(s.id): s.starting_life for s in seats},
        "eliminated": {},
        "eliminatedAtTurn": {},
        "cmd": {},
        "extraCounters": {},
        "turn": 1,
        "currentTurnId": _first_seat_id(game, seats),
        "turnEvents": [],
    }


# --- lifecycle ---------------------------------------------------------------


def start_live_game(session: Session, game_id: int, user_id: int) -> GameLiveState:
    """Owner-only: flip a ``created`` game to ``in_progress`` and create its live
    state. Idempotent: re-entry on an already-in_progress game returns the
    existing state. Starting live mode is a game-management action, so the
    creator (owner-as-user) does it — the one place owner-as-user matters."""
    game = get_game(session, game_id, user_id)  # strict owner-only
    if game is None:
        raise PermissionError("Only the game owner can start live mode")

    if game.live_state is not None and game.status == "in_progress":
        return game.live_state  # idempotent re-entry
    if game.status != "created":
        raise ValueError(f"Cannot start live mode for a game in status '{game.status}'")

    game.status = "in_progress"
    live = GameLiveState(
        state=json.dumps(_initial_state(game)),
        version=1,
        updated_at=utc_now(),
    )
    # Assign through the relationship (not just game_id) so game.live_state
    # reflects the new row immediately on this instance — the delete-orphan
    # cascade also sets game_id.
    game.live_state = live
    # live_started bookend (create path only — idempotent re-entry returned above,
    # so no duplicate). payload is the initial state blob; shares this transaction.
    session.add(
        GameEvent(
            game_id=game.id,
            seat_id=None,
            action_type="live_started",
            payload=live.state,
            turn=1,
            actor_kind="table",
            created_at=utc_now(),
        )
    )
    session.commit()
    _publish(live)
    return live


def get_live_state(session: Session, game_id: int, user_id: int) -> GameLiveState:
    """Viewer-scoped fetch of the live state (owner / seat player / playgroup
    member). Raises ``LookupError`` if not viewable or no live state exists."""
    game = get_viewable_game(session, game_id, user_id)
    if game is None:
        raise LookupError("Game not found or not viewable")
    if game.live_state is None:
        raise LookupError("No live state for this game")
    return game.live_state


# --- action application ------------------------------------------------------


def _coerce_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _require_delta(action: dict) -> int:
    delta = _coerce_int(action.get("delta"))
    if delta is None:
        raise ValueError("delta must be an integer")
    return delta


def _require_seat(seat_id, seats_by_id: dict[int, GameSeat], field: str) -> int:
    sid = _coerce_int(seat_id)
    if sid is None or sid not in seats_by_id:
        raise ValueError(f"{field} {seat_id!r} does not belong to this game")
    return sid


def _authorize_seat_scoped(
    atype: str, action: dict, game: Game, user_id: int, seats_by_id: dict[int, GameSeat]
) -> None:
    """Seat-scoped authorization (no table token). Raises ``PermissionError``
    (→ 403). Seat existence is validated separately (→ 400)."""
    if atype == "turn":
        if not any(s.user_id == user_id for s in game.seats):
            raise PermissionError("Only a seated player may advance the turn")
        return

    seat_field = "receiver_seat_id" if atype == "cmd" else "seat_id"
    seat = seats_by_id.get(_coerce_int(action.get(seat_field)))
    # seat is guaranteed to exist here (validated before auth); attribution decides.
    if seat is None or seat.user_id != user_id:
        raise PermissionError("You may only control your own seat")


def _validate_action_seats(atype: str, action: dict, seats_by_id: dict[int, GameSeat]) -> None:
    """Validate that referenced seats belong to this game (→ 400). Runs BEFORE
    authorization so a bad seat is a 400 even on the table-token path."""
    if atype == "turn":
        return
    if atype == "cmd":
        _require_seat(action.get("receiver_seat_id"), seats_by_id, "receiver_seat_id")
        _require_seat(action.get("attacker_seat_id"), seats_by_id, "attacker_seat_id")
    else:  # life, counter, eliminate
        _require_seat(action.get("seat_id"), seats_by_id, "seat_id")


def _apply_mutation(atype: str, action: dict, state: dict, game: Game) -> dict:
    """Mutate ``state`` in place and return event-payload EXTRAS (empty except for
    cmd, which returns the raw + post-floor ``actual`` delta)."""
    if atype == "life":
        sid = str(_coerce_int(action["seat_id"]))
        state["lives"][sid] = int(state["lives"].get(sid, 0)) + _require_delta(action)

    elif atype == "counter":
        sid = str(_coerce_int(action["seat_id"]))
        name = str(action.get("counter") or "").strip()
        if not name:
            raise ValueError("counter name is required")
        delta = _require_delta(action)
        arr = state["extraCounters"].setdefault(sid, [])
        entry = next((c for c in arr if c.get("type") == name), None)
        if entry is None:
            arr.append({"type": name, "value": delta})
        else:
            entry["value"] = int(entry.get("value", 0)) + delta

    elif atype == "cmd":
        recv = str(_coerce_int(action["receiver_seat_id"]))
        atk = str(_coerce_int(action["attacker_seat_id"]))
        delta = _require_delta(action)
        recv_map = state["cmd"].setdefault(recv, {})
        prev = int(recv_map.get(atk, 0))
        recv_map[atk] = max(0, prev + delta)  # cmd floored at 0
        # Commander damage is coupled to life, matching the localStorage tracker
        # (game_detail.html adjustCmd): the receiver loses the ACTUAL cmd increase
        # (post-floor), so a decrement symmetrically restores life — but only the
        # amount that was actually there (a -3 on a value of 2 restores 2, not 3).
        actual_delta = recv_map[atk] - prev
        state["lives"][recv] = int(state["lives"].get(recv, 0)) - actual_delta
        # Analytics must never re-derive the floor rule — record both deltas.
        return {"raw_delta": delta, "actual_delta": actual_delta}

    elif atype == "eliminate":
        sid = str(_coerce_int(action["seat_id"]))
        eliminated = bool(action.get("eliminated"))
        state["eliminated"][sid] = eliminated
        if eliminated:
            state["eliminatedAtTurn"][sid] = int(state.get("turn", 1))
        else:
            state["eliminatedAtTurn"].pop(sid, None)  # clear on revive

    elif atype == "turn":
        _advance_turn(state, game)

    return {}


def _advance_turn(state: dict, game: Game) -> None:
    """Advance ``currentTurnId`` to the next non-eliminated seat in physical
    clockwise order; increment ``turn`` when the rotation wraps past the first
    seat."""
    seats = _clockwise_seats(game)
    if not seats:
        return
    order = [s.id for s in seats]
    first_id = _first_seat_id(game, seats)
    start = order.index(first_id) if first_id in order else 0
    rot = order[start:] + order[:start]  # rotation beginning at the first seat

    current = state.get("currentTurnId")
    i = rot.index(current) if current in rot else 0
    elim = state.get("eliminated", {})
    for step in range(1, len(rot) + 1):
        j = (i + step) % len(rot)
        cand = rot[j]
        if not elim.get(str(cand), False):
            state["currentTurnId"] = cand
            if j <= i:  # wrapped back to/through the first seat → new round
                state["turn"] = int(state.get("turn", 1)) + 1
            return
    # everyone eliminated → leave currentTurnId as-is.


_SENSITIVE_KEYS = ("table_token", "csrf_token")


def _event_seat_id(atype: str, action: dict) -> int | None:
    """The acted-on seat for an event: RECEIVING seat for cmd, None for turn."""
    if atype == "turn":
        return None
    field = "receiver_seat_id" if atype == "cmd" else "seat_id"
    return _coerce_int(action.get(field))


def _append_event(
    session: Session,
    game_id: int,
    atype: str,
    action: dict,
    state: dict,
    has_table: bool,
    extras: dict,
) -> None:
    """Append one GameEvent for a live action — inside the action's transaction
    (the caller commits). The table/csrf tokens are stripped from the payload."""
    payload = {k: v for k, v in action.items() if k not in _SENSITIVE_KEYS}
    payload.update(extras)  # cmd's raw_delta / actual_delta
    session.add(
        GameEvent(
            game_id=game_id,
            seat_id=_event_seat_id(atype, action),
            action_type=atype,
            payload=json.dumps(payload),
            turn=int(state.get("turn", 1)),  # NEW turn (post turn-advance)
            actor_kind="table" if has_table else "seat",
            created_at=utc_now(),
        )
    )


def apply_live_action(
    session: Session,
    game_id: int,
    user_id: int,
    action: dict,
    table_token: str | None,
) -> GameLiveState:
    """Authorize and apply one live action, then broadcast the full new state.

    Raises ``LookupError`` (→ 404) if the game/live-state is not viewable/absent,
    ``PermissionError`` (→ 403) on authorization failure, ``ValueError`` (→ 400)
    on an invalid action. ponytail: last-write-wins on the blob — no optimistic
    locking. Acceptable at a single physical table's write rate; the ``version``
    bump is for SSE ordering + client staleness display, not conflict rejection.
    """
    game = get_viewable_game(session, game_id, user_id)
    if game is None:
        raise LookupError("Game not found or not viewable")
    live = game.live_state
    if live is None:
        raise LookupError("No live state for this game")

    atype = (action or {}).get("type")
    if atype not in _MUTATING_TYPES:
        raise ValueError(f"Unknown action type: {atype!r}")

    seats_by_id = {s.id: s for s in game.seats}
    _validate_action_seats(atype, action, seats_by_id)  # 400 on bad seat, table path included

    has_table = bool(table_token) and table_token == game.client_token
    if not has_table:
        _authorize_seat_scoped(atype, action, game, user_id, seats_by_id)

    state = json.loads(live.state)
    extras = _apply_mutation(atype, action, state, game)

    live.state = json.dumps(state)
    live.version += 1
    live.updated_at = utc_now()
    # Event append shares this transaction — no event without its mutation.
    _append_event(session, game_id, atype, action, state, has_table, extras)
    session.commit()
    _publish(live)
    return live
