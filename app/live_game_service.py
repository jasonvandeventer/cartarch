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

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import live_game_events
from app.game_service import get_game, get_viewable_game
from app.models import Card, Game, GameEvent, GameLiveState, GameSeat
from app.timeutil import utc_now

# Momir Basic (format="momir") layers two extra actions on top of the shared
# companion infra: momir_activate (summon a random creature at a CMC) and
# momir_kill_token (grey out a dead token). Both are seat-scoped and rejected in
# non-Momir games — Commander games never see the tokens field or these types.
_MOMIR_TYPES = {"momir_activate", "momir_kill_token", "momir_attack", "momir_resolve"}
_MUTATING_TYPES = {"life", "counter", "cmd", "eliminate", "turn"} | _MOMIR_TYPES

_MAX_MOMIR_CMC = 16  # no creatures exist above ~16 CMC in MTG


def _is_momir(game: Game) -> bool:
    return (game.format or "").casefold() == "momir"


def random_creature_at_cmc(session: Session, cmc: int) -> dict | None:
    """Pick a random creature printing at ``cmc`` for Momir Basic, deduplicated by
    card name (many printings exist; any one carries the same P/T and the image
    comes from the mirror regardless of printing). Returns ``None`` when no
    creature exists at that CMC (a legal Momir "whiff", common at very high CMC).

    Dialect-agnostic (``func.random()`` → SQLite ``random()`` / Postgres
    ``random()``). Dedup is a two-step to stay portable: pick a random distinct
    name (GROUP BY, uniform per-name rather than weighted by printing count),
    then a random printing of it — SELECT DISTINCT + ORDER BY random() is
    rejected by Postgres, GROUP BY is not."""
    filters = (
        Card.type_line.like("%Creature%"),
        Card.cmc == cmc,
        Card.type_line.notlike("%Token%"),
        Card.scryfall_id.isnot(None),
    )
    name_row = (
        session.query(Card.name)
        .filter(*filters)
        .group_by(Card.name)
        .order_by(func.random())
        .first()
    )
    if name_row is None:
        return None
    card = (
        session.query(Card)
        .filter(*filters, Card.name == name_row[0])
        .order_by(func.random())
        .first()
    )
    return {
        "name": card.name,
        "power": card.power,
        "toughness": card.toughness,
        "type_line": card.type_line,
        "scryfall_id": card.scryfall_id,
        "cmc": cmc,
    }


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
    state = {
        "lives": {str(s.id): s.starting_life for s in seats},
        "eliminated": {},
        "eliminatedAtTurn": {},
        "cmd": {},
        "extraCounters": {},
        "turn": 1,
        "currentTurnId": _first_seat_id(game, seats),
        "turnEvents": [],
    }
    # Momir-only: seat-id-keyed map of summoned creature tokens + the per-seat
    # once-per-turn activation ledger (seat_id -> round it last activated).
    # Both absent in Commander games so their blob is byte-identical to before.
    if _is_momir(game):
        state["tokens"] = {}
        state["momirTurnUsed"] = {}
        state["attacks"] = []  # pending declared attackers awaiting block decisions
        state["attackSeq"] = 0
    return state


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
    elif atype == "momir_attack":  # attacker's seat + the defending seat
        _require_seat(action.get("seat_id"), seats_by_id, "seat_id")
        _require_seat(action.get("target_seat_id"), seats_by_id, "target_seat_id")
    else:  # life, counter, eliminate, momir_activate, momir_kill_token
        _require_seat(action.get("seat_id"), seats_by_id, "seat_id")


def _apply_mutation(session: Session, atype: str, action: dict, state: dict, game: Game) -> dict:
    """Mutate ``state`` in place and return event-payload EXTRAS (empty except for
    cmd, which returns raw + post-floor ``actual`` deltas, and the Momir actions,
    which return the summoned creature / whiff / killed index). ``session`` is
    used only by momir_activate (its creature query); the other types ignore it."""
    if atype == "momir_activate":
        return _apply_momir_activate(session, action, state)
    if atype == "momir_kill_token":
        return _apply_momir_kill(action, state)
    if atype == "momir_attack":
        return _apply_momir_attack(action, state)
    if atype == "momir_resolve":
        return _apply_momir_resolve(action, state)

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
        causes = state.setdefault("eliminationCause", {})
        if eliminated:
            state["eliminatedAtTurn"][sid] = int(state.get("turn", 1))
            causes[sid] = "manual"  # explicit toggle → manual, never auto-revives
        else:
            state["eliminatedAtTurn"].pop(sid, None)  # clear on revive
            causes.pop(sid, None)

    elif atype == "turn":
        _advance_turn(state, game)

    return {}


def _apply_momir_activate(session: Session, action: dict, state: dict) -> dict:
    """Summon a random creature at ``cmc`` onto the acting seat — the Momir Vig
    ability: ``{X}, discard a card: create a token copy of a random creature with
    mana value X``. The mana cost + discard are paper-side (no hand/land tracking
    — the format is basic lands + creatures only), so the ONE rule the tracker
    enforces is **once per turn** per seat: a seat may activate Momir once per
    round, the quota resetting when the turn counter advances.

    Sorcery-speed / your-own-turn is a UI-guided soft rule (the clients disable
    the control off-turn) rather than a hard server reject, so imperfect turn
    tracking never wedges a casual game.

    A whiff (no creature at that CMC) does NOT consume the once-per-turn quota —
    a mis-picked impossible CMC shouldn't waste your turn. Extras carry the
    creature (or the whiff) for the event + the SSE-driven UI reveal."""
    sid = str(_coerce_int(action["seat_id"]))
    cmc = _coerce_int(action.get("cmc"))
    if cmc is None or not (0 <= cmc <= _MAX_MOMIR_CMC):
        raise ValueError(f"cmc must be an integer between 0 and {_MAX_MOMIR_CMC}")

    # Once per turn (per round). Each seat takes exactly one turn per round, so
    # the round counter (state["turn"]) uniquely identifies "this seat's turn".
    round_no = int(state.get("turn", 1))
    used = state.setdefault("momirTurnUsed", {})
    if used.get(sid) == round_no:
        raise ValueError("Momir can only be activated once per turn")

    creature = random_creature_at_cmc(session, cmc)
    tokens = state.setdefault("tokens", {})
    if creature is None:
        return {"cmc": cmc, "creature": None, "whiff": True}  # whiff: quota not spent
    token = {
        "name": creature["name"],
        "power": creature["power"],
        "toughness": creature["toughness"],
        "type_line": creature["type_line"],
        "scryfall_id": creature["scryfall_id"],
        "cmc": cmc,
        "turn_created": round_no,
        "alive": True,
    }
    tokens.setdefault(sid, []).append(token)
    used[sid] = round_no  # spend the once-per-turn quota
    return {"cmc": cmc, "creature": token, "whiff": False}


def _apply_momir_kill(action: dict, state: dict) -> dict:
    """Mark one of a seat's tokens dead (greys out; no life/damage coupling)."""
    sid = str(_coerce_int(action["seat_id"]))
    idx = _coerce_int(action.get("index"))
    tokens = state.get("tokens", {}).get(sid, [])
    if idx is None or not (0 <= idx < len(tokens)):
        raise ValueError("token index out of range")
    tokens[idx]["alive"] = False
    return {"index": idx}


def _pt_int(value) -> int:
    """Best-effort integer for a raw P/T string. Numeric ("7") → 7; a leading-int
    variable ("1+*") → 1; a pure variable ("*", "X") → 0. Combat math only runs on
    the numeric part — a 0 from an unparseable value applies no auto-damage/lethal
    (the players resolve those creatures by hand)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        import re

        m = re.match(r"-?\d+", str(value or ""))
        return int(m.group()) if m else 0


def _live_token(state: dict, sid: str, idx, what: str) -> dict:
    tokens = state.get("tokens", {}).get(sid, [])
    if idx is None or not (0 <= idx < len(tokens)) or tokens[idx].get("alive") is False:
        raise ValueError(f"{what} creature not found or dead")
    return tokens[idx]


def _apply_momir_attack(action: dict, state: dict) -> dict:
    """DECLARE an attack: the attacking player sends one of their creatures at a
    PLAYER (you attack players, never creatures). This only records a *pending*
    attack in ``state["attacks"]`` — no damage yet. The DEFENDING player then
    decides whether to block, via :func:`_apply_momir_resolve`. Pending attacks
    are cleared when the turn advances (combat ends with the turn).

    A creature can be declared as an attacker only once at a time."""
    a_sid = str(_coerce_int(action["seat_id"]))
    t_sid = str(_coerce_int(action.get("target_seat_id")))
    if a_sid == t_sid:
        raise ValueError("a creature must attack another player, not its controller")
    a_idx = _coerce_int(action.get("index"))
    attacker = _live_token(state, a_sid, a_idx, "attacking")
    attacks = state.setdefault("attacks", [])
    if any(x["attacker_seat"] == a_sid and x["attacker_index"] == a_idx for x in attacks):
        raise ValueError("that creature is already attacking")
    seq = int(state.get("attackSeq", 0)) + 1
    state["attackSeq"] = seq
    attacks.append(
        {"seq": seq, "attacker_seat": a_sid, "attacker_index": a_idx, "target_seat": t_sid}
    )
    return {"seq": seq, "attacker_name": attacker.get("name"), "target_seat_id": int(t_sid)}


def _apply_momir_resolve(action: dict, state: dict) -> dict:
    """The DEFENDING player resolves a pending attack aimed at them — the block
    decision belongs to the defender, not the attacker:

    * **Take it** (no ``block_index``): lose the attacker's power in life.
    * **Block** (``block_index`` = one of your live creatures): your blocker and
      the attacker deal their power to each other; either dies if lethal
      (``power >= toughness``, numeric only). No life loss.
    * **Cancel** (``cancel`` truthy): dismiss a mis-declared attack, no effect.

    A blank/dead attacker (killed since declaration) simply fizzles."""
    d_sid = str(_coerce_int(action["seat_id"]))  # the defender resolving
    seq = _coerce_int(action.get("seq"))
    attacks = state.setdefault("attacks", [])
    entry = next((x for x in attacks if x["seq"] == seq), None)
    if entry is None:
        raise ValueError("no such pending attack")
    if entry["target_seat"] != d_sid:
        raise ValueError("only the defending player can resolve this attack")
    attacks.remove(entry)

    if action.get("cancel"):
        return {"seq": seq, "resolution": "cancel"}

    a_list = state.get("tokens", {}).get(entry["attacker_seat"], [])
    ai = entry["attacker_index"]
    attacker = a_list[ai] if isinstance(ai, int) and 0 <= ai < len(a_list) else None
    if attacker is None or attacker.get("alive") is False:
        return {"seq": seq, "resolution": "fizzle"}  # attacker died before blocks
    a_pow, a_tou = _pt_int(attacker.get("power")), _pt_int(attacker.get("toughness"))

    block_index = action.get("block_index")
    if block_index is None:  # take the hit
        state["lives"][d_sid] = int(state["lives"].get(d_sid, 0)) - a_pow
        return {
            "seq": seq,
            "resolution": "unblocked",
            "attacker_name": attacker.get("name"),
            "target_seat_id": int(d_sid),
            "damage": a_pow,
        }

    blocker = _live_token(state, d_sid, _coerce_int(block_index), "blocking")
    b_pow, b_tou = _pt_int(blocker.get("power")), _pt_int(blocker.get("toughness"))
    attacker_died = a_tou > 0 and b_pow >= a_tou
    blocker_died = b_tou > 0 and a_pow >= b_tou
    if attacker_died:
        attacker["alive"] = False
    if blocker_died:
        blocker["alive"] = False
    return {
        "seq": seq,
        "resolution": "blocked",
        "attacker_name": attacker.get("name"),
        "blocker_name": blocker.get("name"),
        "attacker_died": attacker_died,
        "blocker_died": blocker_died,
    }


def _advance_turn(state: dict, game: Game) -> None:
    """Advance ``currentTurnId`` to the next non-eliminated seat in physical
    clockwise order; increment ``turn`` when the rotation wraps past the first
    seat."""
    # Combat ends with the turn: drop any attackers still awaiting a block
    # decision (Momir-only key — absent in Commander state, left untouched).
    if "attacks" in state:
        state["attacks"] = []
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


# --- auto-elimination on loss conditions --------------------------------------
# Mirrors game_detail.html checkElimination(): a seat is eliminated when its life
# <= 0, any single attacker's commander damage >= 21, or its poison counter >= 10.
# The local tracker makes elimination PERMANENT (no auto-revive) and records no
# cause. Live mode adds two things the tracker is silent on (both additive):
#   * eliminationCause[seat] in {"life","cmd","poison","manual"}.
#   * AUTO eliminations auto-REVIVE when their own condition un-triggers (a live
#     scoreboard corrects mis-taps); MANUAL eliminations never auto-revive.
# Precedence follows the tracker's `||` order (life > poison > cmd), so a coupled
# cmd hit that both reaches 21 AND drops life to 0 is a single "life" elimination.


def _loss_cause(state: dict, sid: str) -> str | None:
    """First-triggered loss cause for a seat, or None. Order mirrors the local
    tracker's `life || poison || cmd` short-circuit."""
    life = state.get("lives", {}).get(sid)
    if life is not None and int(life) <= 0:
        return "life"
    poison = next(
        (
            int(c.get("value", 0))
            for c in state.get("extraCounters", {}).get(sid, [])
            if c.get("type") == "poison"
        ),
        0,
    )
    if poison >= 10:
        return "poison"
    cmd_map = state.get("cmd", {}).get(sid, {})
    if cmd_map and max(int(v) for v in cmd_map.values()) >= 21:
        return "cmd"
    return None


def _affected_seat(atype: str, action: dict) -> int | None:
    """Seat whose loss conditions a mutation can change: the target for
    life/counter, the RECEIVER for cmd. None for turn/eliminate (eliminate sets
    its own cause in _apply_mutation)."""
    if atype in ("life", "counter"):
        return _coerce_int(action.get("seat_id"))
    if atype == "cmd":
        return _coerce_int(action.get("receiver_seat_id"))
    # Resolving an attack (taking it) drains the defending seat's life → re-check
    # them. Declaring an attack changes no life, so it needs no re-check.
    if atype == "momir_resolve":
        return _coerce_int(action.get("seat_id"))
    return None


def _auto_eliminate(
    session: Session, game_id: int, seat_id: int, state: dict, has_table: bool
) -> None:
    """Re-evaluate one seat's loss conditions after a life/cmd/counter mutation,
    flipping auto-elimination on/off and appending an eliminate GameEvent on each
    transition (same transaction as the caller). Manual eliminations are left
    untouched (they never auto-revive)."""
    sid = str(seat_id)
    causes = state.setdefault("eliminationCause", {})
    currently = bool(state.get("eliminated", {}).get(sid))
    if currently and causes.get(sid) == "manual":
        return  # manual lock

    cause = _loss_cause(state, sid)
    if cause and not currently:  # alive → auto-eliminated
        state.setdefault("eliminated", {})[sid] = True
        state.setdefault("eliminatedAtTurn", {})[sid] = int(state.get("turn", 1))
        causes[sid] = cause
        _append_auto_elim_event(session, game_id, seat_id, cause, True, state, has_table)
    elif cause and currently:
        causes[sid] = cause  # still down (possibly a new cause) — refresh, no event
    elif not cause and currently:  # auto-eliminated → recovered (manual excluded above)
        prev = causes.get(sid)
        state["eliminated"][sid] = False
        state.get("eliminatedAtTurn", {}).pop(sid, None)
        causes.pop(sid, None)
        _append_auto_elim_event(session, game_id, seat_id, prev, False, state, has_table)


def _append_auto_elim_event(
    session: Session,
    game_id: int,
    seat_id: int,
    cause: str | None,
    eliminated: bool,
    state: dict,
    has_table: bool,
) -> None:
    """Append the eliminate event for an AUTO elimination/revive."""
    session.add(
        GameEvent(
            game_id=game_id,
            seat_id=seat_id,
            action_type="eliminate",
            payload=json.dumps({"auto": True, "cause": cause, "eliminated": eliminated}),
            turn=int(state.get("turn", 1)),
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
    # Momir actions (+ the tokens state field) exist ONLY in Momir games; a
    # Commander game rejects them so its state stays untouched.
    if atype in _MOMIR_TYPES and not _is_momir(game):
        raise ValueError("Momir actions are only valid in Momir games")

    seats_by_id = {s.id: s for s in game.seats}
    _validate_action_seats(atype, action, seats_by_id)  # 400 on bad seat, table path included

    has_table = bool(table_token) and table_token == game.client_token
    if not has_table:
        _authorize_seat_scoped(atype, action, game, user_id, seats_by_id)

    state = json.loads(live.state)
    extras = _apply_mutation(session, atype, action, state, game)

    # Event append shares this transaction — no event without its mutation. The
    # triggering event is recorded first, then any auto-elimination/revive it
    # caused appends its own eliminate event (state serialized AFTER both).
    _append_event(session, game_id, atype, action, state, has_table, extras)
    affected = _affected_seat(atype, action)
    if affected is not None:
        _auto_eliminate(session, game_id, affected, state, has_table)

    live.state = json.dumps(state)
    live.version += 1
    live.updated_at = utc_now()
    session.commit()
    _publish(live)
    return live
