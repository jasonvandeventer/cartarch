"""Companion mode — live game state service (issue: tracker-server integration).

The FIRST mid-game server write path. A game in ``created`` status still uses the
localStorage-only tracker exactly as before; live mode is opt-in via
:func:`start_live_game`, which flips the game to ``in_progress`` and creates a
``GameLiveState`` row holding the same JSON blob shape the localStorage tracker
uses (so Session 2 can reuse the client render logic).

Authorization is deliberately split from the rest of the app's owner-only model:

* THE TABLE — a request presenting the game's ``client_token`` (the "table
  token") may control ALL seats, and may advance the turn for anyone (including
  out of order). This is the shared tablet running the tracker. NOTE: the token
  is handed out by ``routes/games.py`` to the game OWNER on whatever device
  loads ``/games/{id}`` — it is owner-scoped, not device-scoped, so the creator
  can obtain table powers from their phone via the game detail page. Accepted:
  the creator is the person running the tablet in practice.
* PLAYER PHONES — seat-scoped. A user attributed to a seat
  (``GameSeat.user_id``) controls that seat only (cmd is scoped to the RECEIVING
  seat; turn advance is the ACTIVE seat only — see ``_authorize_seat_scoped``).
  The creator without the table token is seat-scoped like everyone else.

Read access (:func:`get_live_state`, the SSE stream) is viewer-scoped and NOT
token-gated: seat players and playgroup members may watch.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import live_game_events
from app.game_service import get_game, get_viewable_game, multi_commander_seat_ids
from app.models import Game, GameEvent, GameLiveState, GameSeat, OracleCatalog
from app.timeutil import utc_now

logger = logging.getLogger(__name__)

# ── #155 — live-action request-overlap instrumentation ────────────────────────
# DIAGNOSTIC ONLY. Nothing in this block locks a game, retries, or changes what is
# written; it observes. Remove it — or consciously keep it — once #153's
# lost-update hypothesis is settled either way.
#
# The race #153 hypothesises: two requests read version N, both write N+1, and the
# second silently discards the first's mutation (apply_live_action json.loads the
# WHOLE blob, mutates it, re-serialises, commits — no optimistic locking). The
# route runs the service via `run_in_threadpool`, so those really are concurrent
# threads, not interleaved coroutines.
#
# Detected with NO extra query: remember the highest version actually written per
# game. If a commit writes a version that was already written, this request just
# clobbered whoever wrote it. `_live_in_flight` separately counts concurrent
# requests per game so BENIGN overlap is visible too — overlap and data loss are
# different questions and the logs should answer both.
#
# LIMITATION: in-process. It sees races between threadpool workers in one pod,
# which is the realistic case on a single-replica deploy. A cross-pod race would
# escape the detector — but every line carries v_read / v_written, so the raw logs
# still support offline detection.
_LIVE_OVERLAP_MAX_GAMES = 512  # bounded like the login throttle; diagnostic must not leak
_last_written_version: OrderedDict[int, int] = OrderedDict()
_live_in_flight: dict[int, int] = {}
_overlap_lock = threading.Lock()


@contextmanager
def _track_in_flight(game_id: int):
    """Count this request as in-flight for ``game_id`` for its whole duration and
    yield how many OTHER requests were already running when it started.

    Entry, not commit, is the right moment to sample: any two overlapping requests
    have at least one that started while the other was in flight, so an entry
    reading catches every overlap. Sampling at commit would miss a pair whose
    earlier request had already finished.

    Decrements even when the action raises, so a rejected action cannot leak a
    phantom concurrent request into the next one's reading."""
    with _overlap_lock:
        running = _live_in_flight.get(game_id, 0) + 1
        _live_in_flight[game_id] = running
    try:
        yield running - 1  # excluding this request
    finally:
        with _overlap_lock:
            remaining = _live_in_flight.get(game_id, 1) - 1
            if remaining > 0:
                _live_in_flight[game_id] = remaining
            else:
                _live_in_flight.pop(game_id, None)


def _record_live_action(
    game_id: int,
    user_id: int,
    atype: str,
    v_read: int,
    v_written: int,
    started_iso: str,
    t0: float,
    overlap: int = 0,
) -> None:
    """Emit the #155 record for one applied action, flagging a detected lost update.

    Called AFTER commit, so ``v_written`` is what actually landed. Never raises —
    instrumentation must not be able to fail a live game action."""
    duration_ms = (time.perf_counter() - t0) * 1000.0
    try:
        with _overlap_lock:
            previous = _last_written_version.get(game_id)
            # Already written at or beyond this version → a concurrent writer got
            # there first and this commit overwrote their blob.
            clobbered = previous is not None and previous >= v_written
            _last_written_version[game_id] = max(previous or 0, v_written)
            _last_written_version.move_to_end(game_id)
            while len(_last_written_version) > _LIVE_OVERLAP_MAX_GAMES:
                _last_written_version.popitem(last=False)
        logger.info(
            "live_action game=%s actor=%s type=%s v_read=%s v_written=%s "
            "start=%s dur_ms=%.3f concurrent=%s lost_update=%s",
            game_id,
            user_id,
            atype,
            v_read,
            v_written,
            started_iso,
            duration_ms,
            overlap,
            clobbered,
        )
        if clobbered:
            logger.warning(
                "LOST UPDATE game=%s actor=%s type=%s v_read=%s v_written=%s "
                "already_written=%s dur_ms=%.3f concurrent=%s — this commit discarded a "
                "concurrent mutation (#153/#155)",
                game_id,
                user_id,
                atype,
                v_read,
                v_written,
                previous,
                duration_ms,
                overlap,
            )
            _persist_live_conflict(
                game_id, user_id, atype, v_read, v_written, previous, overlap, duration_ms
            )
    except Exception:  # pragma: no cover — never let telemetry break a game
        logger.debug("live_action instrumentation failed", exc_info=True)


def _persist_live_conflict(
    game_id: int,
    user_id: int | None,
    atype: str,
    v_read: int,
    v_written: int,
    already_written: int | None,
    overlap: int,
    duration_ms: float,
) -> None:
    """Write one detected lost update to ``live_action_conflicts``.

    **Its own short session, never the caller's.** This runs AFTER the game's
    commit; borrowing that session would put a diagnostic write inside the game's
    transaction boundary, and a failure there could take the action with it. Same
    posture the ingest jobs use.

    Swallows everything — the caller already wraps this, but the contract that
    instrumentation may never fail an action is worth stating where the DB write
    actually happens. A lost conflict row is a lost diagnostic; a failed live
    action is a broken game.
    """
    from app.db import SessionLocal
    from app.models import LiveActionConflict

    session = SessionLocal()
    try:
        session.add(
            LiveActionConflict(
                game_id=game_id,
                user_id=user_id,
                action_type=atype,
                version_read=v_read,
                version_written=v_written,
                already_written=already_written,
                concurrent=overlap,
                duration_ms=duration_ms,
            )
        )
        session.commit()
    except Exception:  # pragma: no cover — diagnostics never break a game
        session.rollback()
        logger.debug("live conflict persist failed", exc_info=True)
    finally:
        session.close()


# Momir Basic (format="momir") layers two extra actions on top of the shared
# companion infra: momir_activate (summon a random creature at a CMC) and
# momir_kill_token (grey out a dead token). Both are seat-scoped and rejected in
# non-Momir games — Commander games never see the tokens field or these types.
_MOMIR_TYPES = {
    "momir_activate",
    "momir_kill_token",
    "momir_revive_token",
    "momir_attack",
    "momir_resolve",
    "momir_play_land",
    "momir_adjust",
    # #112 ability primitives — humans execute the oracle text.
    "momir_damage",
    "momir_counter_token",
    "momir_tap_token",
    "momir_sacrifice",
}

# #112 — primitives that may target ANOTHER seat's board/life (an ability like
# "deals 2 to target creature" crosses seats). Any SEATED player may perform
# them; every use is event-logged with the acting user. Non-seated → 403. Table
# token retains full override. Hard-blocking cross-seat effects would wedge
# legitimate ability resolution, so the audit log + social contract govern.
_MOMIR_CROSS_SEAT = {"momir_damage", "momir_counter_token", "momir_tap_token", "momir_sacrifice"}

# #115 — Planechase overlay: a shared plane deck on ANY live game (not a format).
# Additive on the state blob (fields appear only once enabled), so Commander /
# Momir blobs stay byte-identical until a table turns Planechase on. Enable is
# table-only (game config); rolling the planar die + planeswalking are open to any
# seated player (the active player rolls, but enforcing "whose turn" is fussy for
# a casual tracker — the social contract governs, every action is event-logged).
_PLANECHASE_TYPES = {"planechase_enable", "planar_roll", "planeswalk"}

# #116 — Archenemy overlay: a shared scheme deck for a designated archenemy seat,
# on any live game. Same additive-state / any-game shape as Planechase. Enable
# (which designates the archenemy) is table-only; setting a scheme in motion +
# abandoning ongoing schemes are open to any seated player (the archenemy).
_ARCHENEMY_TYPES = {"archenemy_enable", "scheme_set_in_motion", "scheme_abandon"}
_MUTATING_TYPES = (
    {"life", "counter", "cmd", "eliminate", "turn"}
    | _MOMIR_TYPES
    | _PLANECHASE_TYPES
    | _ARCHENEMY_TYPES
)

_MAX_MOMIR_CMC = 16  # no creatures exist above ~16 CMC in MTG

# Momir Sim #110 — per-seat resource layer (mana / hand / library). Seeded at
# start and lazily back-filled (setdefault) so a game that began before this
# feature doesn't KeyError. Adjustable only via the table-token momir_adjust.
_MOMIR_RES_DEFAULTS = {
    "library": 60,
    "hand": 7,
    "lands": 0,
    "untapped": 0,
    "landPlayed": False,
}


def _is_momir(game: Game) -> bool:
    return (game.format or "").casefold() == "momir"


def random_creature_at_cmc(session: Session, cmc: int) -> dict | None:
    """Pick a random Momir-legal creature at ``cmc``. Returns ``None`` when none
    exists at that CMC (a legal Momir "whiff", common at very high CMC).

    Momir Sim #109 — sourced from ``oracle_catalog`` (one row per NAME already,
    so the old GROUP-BY-then-repick dedup dance is gone) instead of the
    collection-bounded ``cards`` table. The token/vintage/set exclusions are
    precomputed into ``is_momir_legal`` at ingest, so the query just trusts it.
    Return shape is unchanged so callers/clients don't change. Dialect-agnostic
    (``func.random()`` → SQLite/Postgres ``random()``)."""
    row = (
        session.query(OracleCatalog)
        .filter(OracleCatalog.is_momir_legal.is_(True), OracleCatalog.cmc == cmc)
        .order_by(func.random())
        .first()
    )
    if row is None:
        return None
    return {
        "name": row.name,
        "power": row.power,
        "toughness": row.toughness,
        "type_line": row.type_line,
        "scryfall_id": row.scryfall_id,
        "cmc": cmc,
        # #111 — keywords ride onto the token at summon so combat is a
        # self-contained blob read (no catalog join at damage time).
        "keywords": json.loads(row.keywords or "[]"),
        # #112 — oracle text rides along too; the app never interprets it, players
        # resolve abilities by hand via the primitives.
        "oracle_text": row.oracle_text,
    }


# ── Planechase (#115) ─────────────────────────────────────────────────────────
# Planes + phenomena live in oracle_catalog alongside creatures (the ingest keeps
# them, is_momir_legal=False so they never leak into the Momir pool). type_line
# "Plane — X" (a leading "Plane " excludes "Planeswalker") or "...Phenomenon".


def _plane_filter():
    return or_(
        OracleCatalog.type_line.like("Plane %"),
        OracleCatalog.type_line.like("%Phenomenon%"),
    )


def _shuffled_plane_ids(session: Session, only: list[int] | None = None) -> list[int]:
    """Plane/phenomenon ids in random order (DB ``random()`` — server-authoritative,
    dialect-agnostic). ``only`` reshuffles a specific subset (the discard pile)."""
    q = session.query(OracleCatalog.id).filter(_plane_filter())
    if only is not None:
        if not only:
            return []
        q = q.filter(OracleCatalog.id.in_(only))
    return [pid for (pid,) in q.order_by(func.random()).all()]


def _plane_dict(session: Session, plane_id: int | None) -> dict | None:
    if plane_id is None:
        return None
    row = session.get(OracleCatalog, plane_id)
    if row is None:
        return None
    return {
        "id": row.id,
        "name": row.name,
        "text": row.oracle_text or "",
        "scryfall_id": row.scryfall_id,
        "type": row.type_line or "",
    }


def _advance_plane(session: Session, state: dict) -> None:
    """Move the current plane to discard and reveal the next; reshuffle the discard
    back into the deck when it empties (paper rule)."""
    deck = state.get("planeDeck", [])
    discard = state.get("planeDiscard", [])
    current = state.get("currentPlane")
    if current:
        discard.append(current["id"])
    if not deck:
        deck = _shuffled_plane_ids(session, only=discard)
        discard = []
    nxt = deck.pop(0) if deck else None
    state["planeDeck"] = deck
    state["planeDiscard"] = discard
    state["currentPlane"] = _plane_dict(session, nxt)


def _apply_planechase_enable(session: Session, action: dict, state: dict) -> dict:
    """Enable/disable the Planechase overlay. Enable shuffles the whole plane pool
    and reveals the top plane; disable strips the fields (blob back to byte-identical)."""
    if not bool(action.get("enabled", True)):
        for key in ("planechase", "planeDeck", "planeDiscard", "currentPlane", "lastRoll"):
            state.pop(key, None)
        return {}
    ids = _shuffled_plane_ids(session)
    if not ids:
        raise ValueError("No planes in the catalog — run the plane ingest first")
    state["planechase"] = True
    state["planeDeck"] = ids
    state["planeDiscard"] = []
    state["currentPlane"] = None
    state["lastRoll"] = None
    _advance_plane(session, state)  # reveal the first plane
    return {}


def _apply_planeswalk(session: Session, action: dict, state: dict) -> dict:
    if not state.get("planechase"):
        raise ValueError("Planechase is not enabled for this game")
    _advance_plane(session, state)
    return {}


def _apply_planar_roll(session: Session, action: dict, state: dict) -> dict:
    """Roll the 6-face planar die: 4 blank / 1 chaos / 1 planeswalk. A planeswalk
    result advances the plane in the same action (one tap = roll + resolve). The
    chaos effect is on the plane card — players read + resolve it by hand."""
    if not state.get("planechase"):
        raise ValueError("Planechase is not enabled for this game")
    roll = secrets.randbelow(6)
    face = "chaos" if roll == 4 else "planeswalk" if roll == 5 else "blank"
    state["lastRoll"] = face
    if face == "planeswalk":
        _advance_plane(session, state)
    return {"face": face}


# ── Archenemy (#116) ──────────────────────────────────────────────────────────
# Schemes live in oracle_catalog alongside creatures/planes (is_momir_legal=False).
# type_line "Scheme" or "Ongoing Scheme" (the latter persists in play).


def _scheme_filter():
    return OracleCatalog.type_line.like("%Scheme%")


def _shuffled_scheme_ids(session: Session, only: list[int] | None = None) -> list[int]:
    q = session.query(OracleCatalog.id).filter(_scheme_filter())
    if only is not None:
        if not only:
            return []
        q = q.filter(OracleCatalog.id.in_(only))
    return [sid for (sid,) in q.order_by(func.random()).all()]


def _scheme_dict(session: Session, scheme_id: int | None) -> dict | None:
    if scheme_id is None:
        return None
    row = session.get(OracleCatalog, scheme_id)
    if row is None:
        return None
    type_line = row.type_line or ""
    return {
        "id": row.id,
        "name": row.name,
        "text": row.oracle_text or "",
        "scryfall_id": row.scryfall_id,
        "type": type_line,
        "ongoing": "Ongoing" in type_line,
    }


def _apply_archenemy_enable(session: Session, action: dict, state: dict) -> dict:
    """Enable/disable Archenemy. Enable designates the archenemy seat and shuffles
    the scheme deck; re-enabling while on just re-points the seat (no reshuffle).
    Disable strips the fields (blob back to byte-identical)."""
    if not bool(action.get("enabled", True)):
        for key in (
            "archenemy",
            "archenemySeatId",
            "schemeDeck",
            "schemeDiscard",
            "schemeCurrent",
            "ongoingSchemes",
        ):
            state.pop(key, None)
        return {}
    seat_id = _coerce_int(action.get("seat_id"))
    if state.get("archenemy"):
        state["archenemySeatId"] = seat_id  # already running — just reassign
        return {}
    ids = _shuffled_scheme_ids(session)
    if not ids:
        raise ValueError("No schemes in the catalog — run the scheme ingest first")
    state["archenemy"] = True
    state["archenemySeatId"] = seat_id
    state["schemeDeck"] = ids
    state["schemeDiscard"] = []
    state["schemeCurrent"] = None
    state["ongoingSchemes"] = []
    return {}


def _apply_scheme_set_in_motion(session: Session, action: dict, state: dict) -> dict:
    """Set the top scheme in motion: retire the previous non-ongoing current to the
    discard, reveal the next (reshuffle discard when the deck empties), and park an
    Ongoing scheme in the persistent list."""
    if not state.get("archenemy"):
        raise ValueError("Archenemy is not enabled for this game")
    cur = state.get("schemeCurrent")
    if cur and not cur.get("ongoing"):
        state.setdefault("schemeDiscard", []).append(cur["id"])
    deck = state.get("schemeDeck", [])
    discard = state.get("schemeDiscard", [])
    if not deck:
        deck = _shuffled_scheme_ids(session, only=discard)
        discard = []
    nxt = deck.pop(0) if deck else None
    state["schemeDeck"] = deck
    state["schemeDiscard"] = discard
    scheme = _scheme_dict(session, nxt)
    state["schemeCurrent"] = scheme
    if scheme and scheme.get("ongoing"):
        state.setdefault("ongoingSchemes", []).append(scheme)
    return {}


def _apply_scheme_abandon(session: Session, action: dict, state: dict) -> dict:
    """Manually abandon a scheme (default: the app can't know arbitrary leaves
    conditions). With ``scheme_id`` → drop that ongoing scheme; without → clear the
    current in-motion scheme. Abandoned schemes go to the discard."""
    if not state.get("archenemy"):
        raise ValueError("Archenemy is not enabled for this game")
    sid = _coerce_int(action.get("scheme_id"))
    if sid is not None:
        ongoing = state.get("ongoingSchemes", [])
        state["ongoingSchemes"] = [s for s in ongoing if s.get("id") != sid]
        if any(s.get("id") == sid for s in ongoing):
            state.setdefault("schemeDiscard", []).append(sid)
        # A cleared ongoing scheme that is also the current one drops from view.
        if (state.get("schemeCurrent") or {}).get("id") == sid:
            state["schemeCurrent"] = None
    else:
        cur = state.get("schemeCurrent")
        if cur:
            state.setdefault("schemeDiscard", []).append(cur["id"])
            state["schemeCurrent"] = None
    return {}


def valid_momir_mvs(session: Session) -> set[int]:
    """Set of integer MVs (0.._MAX_MOMIR_CMC) with at least one Momir-legal
    creature — feeds the Phase 5 picker (grey out empty MVs).

    ponytail: queried live, NOT memoized. The old per-process memo went stale
    across processes — an ingest in the job pod couldn't bust a web pod's cache,
    so a freshly-populated catalog left the picker greyed until a manual restart
    (this bit prod on the v4.6.0 rollout). A DISTINCT over the indexed is_momir_legal
    column is sub-millisecond and only runs on a Momir page render, so a cache
    bought nothing but a footgun."""
    rows = (
        session.query(OracleCatalog.cmc).filter(OracleCatalog.is_momir_legal.is_(True)).distinct()
    )
    return {int(c) for (c,) in rows if c is not None and 0 <= c <= _MAX_MOMIR_CMC}


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


def turn_rotation(game: Game) -> list[int]:
    """Seat ids in turn order, BEGINNING at the game's starting seat. Shared with
    ``game_analytics_service``, which replays the same rotation to attribute each
    pace segment to a player — the two must never derive rotation separately."""
    seats = _clockwise_seats(game)
    order = [s.id for s in seats]
    first_id = _first_seat_id(game, seats)
    start = order.index(first_id) if first_id in order else 0
    return order[start:] + order[:start]


def next_seat_in_rotation(
    rot: list[int], current: int | None, eliminated: dict
) -> tuple[int | None, bool]:
    """Next non-eliminated seat after ``current`` in ``rot`` (a
    :func:`turn_rotation` list), plus whether the rotation wrapped past the first
    seat (→ a new round). ``(None, False)`` when every seat is eliminated —
    callers leave the current seat in place. ``eliminated`` is the state blob's
    seat-id-STRING-keyed map."""
    if not rot:
        return None, False
    i = rot.index(current) if current in rot else 0
    for step in range(1, len(rot) + 1):
        j = (i + step) % len(rot)
        cand = rot[j]
        if not eliminated.get(str(cand), False):
            return cand, j <= i
    return None, False


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


def _initial_state(game: Game, partner_seat_ids: set[int] | None = None) -> dict:
    """The live blob at start — mirrors the localStorage tracker shape. Object
    keys are seat-id STRINGS (JSON coerces them anyway; matches JS render).

    ``partner_seat_ids`` (game_service.multi_commander_seat_ids) is recorded as
    ``partnerSeats`` so :func:`_loss_cause` can refuse to auto-eliminate on their
    SHARED commander-damage counter. Additive and optional: a blob written before
    this simply has no key, and the check then behaves exactly as it did.
    """
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
        # Seats whose deck has >1 commander. See _loss_cause.
        "partnerSeats": sorted(str(sid) for sid in (partner_seat_ids or set())),
    }
    # Momir-only: seat-id-keyed map of summoned creature tokens + the per-seat
    # once-per-turn activation ledger (seat_id -> round it last activated).
    # Both absent in Commander games so their blob is byte-identical to before.
    if _is_momir(game):
        state["tokens"] = {}
        state["momirTurnUsed"] = {}
        state["attacks"] = []  # pending declared attackers awaiting block decisions
        state["attackSeq"] = 0
        # #113 physical mode — real basic-land decks handle mana/hand/library, so
        # skip the digital resource layer entirely (no double bookkeeping).
        state["momirPhysical"] = bool(game.momir_physical)
        if not state["momirPhysical"]:
            # Per-seat resource layer (#110). One map per field, keyed like lives.
            for key, default in _MOMIR_RES_DEFAULTS.items():
                state[key] = {str(s.id): default for s in seats}
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
    init = _initial_state(game, multi_commander_seat_ids(session, game))
    # #110 — the starting player draws on their first turn too (Momir multiplayer
    # rule: EVERY player draws, unlike paper MTG where the starter skips). Run the
    # starting seat's begin-of-turn now so its untap/land-reset/draw is applied.
    if _is_momir(game) and init.get("currentTurnId") is not None:
        _begin_momir_turn(session, game.id, init["currentTurnId"], init, has_table=True)
    live = GameLiveState(
        state=json.dumps(init),
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
    atype: str,
    action: dict,
    game: Game,
    user_id: int,
    seats_by_id: dict[int, GameSeat],
    state: dict,
) -> None:
    """Seat-scoped authorization (no table token). Raises ``PermissionError``
    (→ 403). Seat existence is validated separately (→ 400)."""
    if atype == "turn":
        # Only the ACTIVE player ends the turn from their phone. An unattributed
        # active seat (guest, no user_id) has no phone owner, so that turn is
        # table-token-only — friction, not a wedge: the table surface exists in
        # every live game (live_start is owner-only and lands on the detail page,
        # which carries the token). Deliberately a hard reject, unlike the
        # sorcery-speed soft rule in _apply_momir_activate: the tablet is the
        # documented escape hatch if currentTurnId drifts from the real table.
        active = seats_by_id.get(_coerce_int(state.get("currentTurnId")))
        if active is None or active.user_id != user_id:
            raise PermissionError("Only the active player may advance the turn")
        return

    # #115 — enabling Planechase is a table-only game config; rolling the planar
    # die + planeswalking are open to any seated player (their phone).
    if atype == "planechase_enable":
        raise PermissionError("Only the table can enable Planechase")
    if atype in _PLANECHASE_TYPES:
        if not any(s.user_id == user_id for s in game.seats):
            raise PermissionError("Only a seated player may control Planechase")
        return

    if atype == "archenemy_enable":
        raise PermissionError("Only the table can enable Archenemy")
    if atype in _ARCHENEMY_TYPES:
        if not any(s.user_id == user_id for s in game.seats):
            raise PermissionError("Only a seated player may control Archenemy")
        return

    # #110 — resource correction is a table-only power (seats get hard validation;
    # only the tablet can fix mis-taps). This branch runs only when the table token
    # was absent, so any seat player reaching here is rejected.
    if atype == "momir_adjust":
        raise PermissionError("Only the table can adjust Momir resources")

    # #112 — ability primitives may cross seats (an ETB that damages an opponent's
    # creature). ANY seated player may resolve them; the event log records who.
    if atype in _MOMIR_CROSS_SEAT:
        if not any(s.user_id == user_id for s in game.seats):
            raise PermissionError("Only a seated player may resolve abilities")
        return

    seat_field = "receiver_seat_id" if atype == "cmd" else "seat_id"
    seat = seats_by_id.get(_coerce_int(action.get(seat_field)))
    # seat is guaranteed to exist here (validated before auth); attribution decides.
    if seat is None or seat.user_id != user_id:
        raise PermissionError("You may only control your own seat")


def _validate_action_seats(atype: str, action: dict, seats_by_id: dict[int, GameSeat]) -> None:
    """Validate that referenced seats belong to this game (→ 400). Runs BEFORE
    authorization so a bad seat is a 400 even on the table-token path."""
    if atype == "turn" or atype in _PLANECHASE_TYPES:
        return  # table/game-level actions — no seat reference
    if atype in _ARCHENEMY_TYPES:
        # Only archenemy_enable names a seat (the designated archenemy); validate it.
        if atype == "archenemy_enable" and action.get("seat_id") is not None:
            _require_seat(action.get("seat_id"), seats_by_id, "seat_id")
        return
    if atype == "cmd":
        _require_seat(action.get("receiver_seat_id"), seats_by_id, "receiver_seat_id")
        _require_seat(action.get("attacker_seat_id"), seats_by_id, "attacker_seat_id")
    elif atype == "momir_attack":  # attacker's seat + the defending seat
        _require_seat(action.get("seat_id"), seats_by_id, "seat_id")
        _require_seat(action.get("target_seat_id"), seats_by_id, "target_seat_id")
    else:  # life, counter, eliminate, momir_activate/kill/revive, #112 primitives
        _require_seat(action.get("seat_id"), seats_by_id, "seat_id")


def _apply_mutation(
    session: Session, atype: str, action: dict, state: dict, game: Game, has_table: bool
) -> dict:
    """Mutate ``state`` in place and return event-payload EXTRAS (empty except for
    cmd, which returns raw + post-floor ``actual`` deltas, and the Momir actions,
    which return the summoned creature / whiff / killed index). ``session`` is used
    by the Momir creature query and by the deck-out event on turn advance."""
    if atype == "momir_activate":
        return _apply_momir_activate(session, action, state)
    if atype == "momir_kill_token":
        return _apply_momir_kill(action, state)
    if atype == "momir_revive_token":
        return _apply_momir_revive(action, state)
    if atype == "momir_attack":
        return _apply_momir_attack(action, state)
    if atype == "momir_resolve":
        return _apply_momir_resolve(action, state)
    if atype == "momir_play_land":
        return _apply_momir_play_land(action, state)
    if atype == "momir_adjust":
        return _apply_momir_adjust(action, state)
    if atype == "momir_damage":
        return _apply_momir_damage(action, state)
    if atype == "momir_counter_token":
        return _apply_momir_counter(action, state)
    if atype == "momir_tap_token":
        return _apply_momir_tap(action, state)
    if atype == "momir_sacrifice":
        return _apply_momir_kill(action, state)  # same effect; type distinguishes the log

    if atype == "planechase_enable":
        return _apply_planechase_enable(session, action, state)
    if atype == "planar_roll":
        return _apply_planar_roll(session, action, state)
    if atype == "planeswalk":
        return _apply_planeswalk(session, action, state)

    if atype == "archenemy_enable":
        return _apply_archenemy_enable(session, action, state)
    if atype == "scheme_set_in_motion":
        return _apply_scheme_set_in_motion(session, action, state)
    if atype == "scheme_abandon":
        return _apply_scheme_abandon(session, action, state)

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
        # Stamp the seat STARTING the next turn into the event payload, so the
        # pace strip can attribute each segment to a player. The `seat_id` COLUMN
        # stays NULL for turn events (documented invariant, consumed elsewhere) —
        # this rides in the payload, which needs no migration. Reading it back:
        # segment 1 belongs to _first_seat_id, and segment i+1 to the
        # active_seat_id carried on turn event i.
        return {"active_seat_id": _advance_turn(session, state, game, has_table)}

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

    # #110 — activation costs {cmc} in untapped lands plus discarding a card. You
    # must be able to pay to activate. (The picker is constrained to affordable +
    # non-empty MVs, so these rejects are a hard-validation backstop.) In #113
    # physical mode the real cards pay the cost, so the app doesn't gate on it.
    physical = bool(state.get("momirPhysical"))
    if not physical:
        _ensure_res(state, sid)
        if int(state["untapped"][sid]) < cmc:
            raise ValueError("not enough untapped lands to activate Momir")
        if int(state["hand"][sid]) < 1:
            raise ValueError("no card in hand to discard")

    creature = random_creature_at_cmc(session, cmc)
    tokens = state.setdefault("tokens", {})
    if creature is None:
        # Safety-net whiff: no cost and no quota spent (should be unreachable once
        # the picker only offers non-empty MVs).
        return {"cmc": cmc, "creature": None, "whiff": True}
    token = {
        "name": creature["name"],
        "power": creature["power"],
        "toughness": creature["toughness"],
        "type_line": creature["type_line"],
        "scryfall_id": creature["scryfall_id"],
        "cmc": cmc,
        "turn_created": round_no,
        "alive": True,
        # #111 combat state — self-contained on the token.
        "keywords": creature.get("keywords", []),
        "tapped": False,
        "damage": 0,  # marked this turn; cleared on turn advance
        "counters": {"p1p1": 0, "m1m1": 0},  # #112 primitives write these
        "oracle_text": creature.get("oracle_text"),  # #112 — humans resolve it
    }
    tokens.setdefault(sid, []).append(token)
    if not physical:
        state["untapped"][sid] = int(state["untapped"][sid]) - cmc  # tap the mana
        state["hand"][sid] = int(state["hand"][sid]) - 1  # discard
    used[sid] = round_no  # spend the once-per-turn quota
    return {"cmc": cmc, "creature": token, "whiff": False}


def _ensure_res(state: dict, sid: str) -> None:
    """Lazy setdefault backfill of a seat's resource fields (#110) so a Momir game
    that started before the resource layer doesn't KeyError."""
    for key, default in _MOMIR_RES_DEFAULTS.items():
        state.setdefault(key, {}).setdefault(sid, default)


def _apply_momir_play_land(action: dict, state: dict) -> dict:
    """Play one card from hand as a land (#110): once per turn, requires a card in
    hand. The land enters untapped, so it's usable for mana this same turn."""
    sid = str(_coerce_int(action["seat_id"]))
    if state.get("momirPhysical"):
        raise ValueError("lands are physical in this game")
    _ensure_res(state, sid)
    if int(state["hand"][sid]) < 1:
        raise ValueError("no card in hand to play as a land")
    if state["landPlayed"][sid]:
        raise ValueError("a land has already been played this turn")
    state["hand"][sid] = int(state["hand"][sid]) - 1
    state["lands"][sid] = int(state["lands"][sid]) + 1
    state["untapped"][sid] = int(state["untapped"][sid]) + 1
    state["landPlayed"][sid] = True
    return {}


def _apply_momir_adjust(action: dict, state: dict) -> dict:
    """Table-only correction of a seat's resource counts (#110) — the tablet fixes
    mis-taps. Auth (table-token-only) is enforced upstream in
    :func:`_authorize_seat_scoped`; here we just apply. Only the four counts are
    adjustable (not landPlayed); each clamped to a non-negative integer."""
    sid = str(_coerce_int(action["seat_id"]))
    if state.get("momirPhysical"):
        raise ValueError("no digital resources to adjust in physical mode")
    _ensure_res(state, sid)
    changed = {}
    for key in ("library", "hand", "lands", "untapped"):
        if key not in action:
            continue
        val = _coerce_int(action.get(key))
        if val is None or val < 0:
            raise ValueError(f"{key} must be a non-negative integer")
        state[key][sid] = val
        changed[key] = val
    return {"adjusted": changed}


def _apply_momir_kill(action: dict, state: dict) -> dict:
    """Mark one of a seat's tokens dead (greys out; no life/damage coupling)."""
    sid = str(_coerce_int(action["seat_id"]))
    idx = _coerce_int(action.get("index"))
    tokens = state.get("tokens", {}).get(sid, [])
    if idx is None or not (0 <= idx < len(tokens)):
        raise ValueError("token index out of range")
    tokens[idx]["alive"] = False
    return {"index": idx}


def _apply_momir_revive(action: dict, state: dict) -> dict:
    """Un-kill a token (#111) — the inverse of momir_kill_token, for undoing a
    mis-tap or a combat call the math got wrong. Seat-scoped like kill (own seat
    or the table token); marked damage is left as-is (it clears on turn advance)."""
    sid = str(_coerce_int(action["seat_id"]))
    idx = _coerce_int(action.get("index"))
    tokens = state.get("tokens", {}).get(sid, [])
    if idx is None or not (0 <= idx < len(tokens)):
        raise ValueError("token index out of range")
    tokens[idx]["alive"] = True
    return {"index": idx}


# --- Momir combat keyword engine (#111, Tier 1) ------------------------------


def _has_kw(token: dict, kw: str) -> bool:
    """Case-insensitive keyword membership. ``kw`` is lowercase; Scryfall stores
    title-case ("First strike", "Double strike")."""
    return any((k or "").casefold() == kw for k in (token.get("keywords") or []))


def _counters(token: dict) -> tuple[int, int]:
    c = token.get("counters") or {}
    return int(c.get("p1p1", 0)), int(c.get("m1m1", 0))


def _eff_pow(token: dict) -> int:
    """Effective power: parsed base + p1p1 − m1m1 (variable P/T → 0 base)."""
    plus, minus = _counters(token)
    return _pt_int(token.get("power")) + plus - minus


def _eff_tou(token: dict) -> int:
    plus, minus = _counters(token)
    return _pt_int(token.get("toughness")) + plus - minus


def _numeric_tou(token: dict) -> bool:
    """Whether the base toughness is a plain integer. Variable P/T ("*", "1+*",
    "X") is NOT — those creatures are resolved manually and never auto-die."""
    try:
        int(token.get("toughness"))
        return True
    except (TypeError, ValueError):
        return False


def _token_dies(token: dict) -> bool:
    """State-based death for the #112 primitives: a numeric-toughness creature
    dies to lethal marked damage or to 0-or-less effective toughness (e.g. m1m1
    counters). Indestructible survives; variable P/T stays manual."""
    if _has_kw(token, "indestructible") or not _numeric_tou(token):
        return False
    tou = _eff_tou(token)
    return tou <= 0 or int(token.get("damage", 0)) >= tou


def _apply_momir_damage(action: dict, state: dict) -> dict:
    """#112 — deal N damage to a target token (marks damage; death check runs) OR
    to a target seat's life (auto-elim rechecked upstream). Token when ``index``
    is given, otherwise the seat named by ``seat_id``. Manual primitive: a human
    resolves an ability's damage; the app just does the bookkeeping."""
    sid = str(_coerce_int(action["seat_id"]))
    amount = _coerce_int(action.get("amount"))
    if amount is None or amount < 1:
        raise ValueError("amount must be a positive integer")
    if action.get("index") is None:  # seat/player damage
        state["lives"][sid] = int(state["lives"].get(sid, 0)) - amount
        return {"target": "seat", "amount": amount}
    token = _live_token(state, sid, _coerce_int(action.get("index")), "target")
    token["damage"] = int(token.get("damage", 0)) + amount
    died = _token_dies(token)
    if died:
        token["alive"] = False
    return {"target": "token", "amount": amount, "died": died}


def _apply_momir_counter(action: dict, state: dict) -> dict:
    """#112 — add/remove +1/+1 (``p1p1``) or -1/-1 (``m1m1``) counters on a token.
    Both maps are kept raw (opposing counters are NOT annihilated). A death recheck
    runs after (m1m1 to 0 toughness kills)."""
    sid = str(_coerce_int(action["seat_id"]))
    kind = str(action.get("counter") or "")
    if kind not in ("p1p1", "m1m1"):
        raise ValueError("counter must be 'p1p1' or 'm1m1'")
    delta = _require_delta(action)
    token = _live_token(state, sid, _coerce_int(action.get("index")), "target")
    counters = token.setdefault("counters", {"p1p1": 0, "m1m1": 0})
    counters[kind] = max(0, int(counters.get(kind, 0)) + delta)  # can't go negative
    died = _token_dies(token)
    if died:
        token["alive"] = False
    return {"counter": kind, "delta": delta, "value": counters[kind], "died": died}


def _apply_momir_tap(action: dict, state: dict) -> dict:
    """#112 — toggle a token's tapped state (manual tap/untap for abilities the
    engine doesn't drive)."""
    sid = str(_coerce_int(action["seat_id"]))
    token = _live_token(state, sid, _coerce_int(action.get("index")), "target")
    token["tapped"] = not token.get("tapped", False)
    return {"tapped": token["tapped"]}


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

    A creature can be declared as an attacker only once at a time. #111 — the
    attacker must be untapped, not summoning-sick (controlled since the start of
    the turn, i.e. ``turn_created < current round``, unless it has haste), and not
    have defender. Declaring TAPS the attacker unless it has vigilance."""
    a_sid = str(_coerce_int(action["seat_id"]))
    t_sid = str(_coerce_int(action.get("target_seat_id")))
    if a_sid == t_sid:
        raise ValueError("a creature must attack another player, not its controller")
    a_idx = _coerce_int(action.get("index"))
    attacker = _live_token(state, a_sid, a_idx, "attacking")
    if attacker.get("tapped"):
        raise ValueError("a tapped creature cannot attack")
    if _has_kw(attacker, "defender"):
        raise ValueError("a creature with defender cannot attack")
    sick = int(attacker.get("turn_created", 0)) >= int(state.get("turn", 1))
    if sick and not _has_kw(attacker, "haste"):
        raise ValueError("that creature is summoning sick")
    attacks = state.setdefault("attacks", [])
    if any(x["attacker_seat"] == a_sid and x["attacker_index"] == a_idx for x in attacks):
        raise ValueError("that creature is already attacking")
    if not _has_kw(attacker, "vigilance"):
        attacker["tapped"] = True  # attacking taps, unless vigilance
    seq = int(state.get("attackSeq", 0)) + 1
    state["attackSeq"] = seq
    attacks.append(
        {"seq": seq, "attacker_seat": a_sid, "attacker_index": a_idx, "target_seat": t_sid}
    )
    return {"seq": seq, "attacker_name": attacker.get("name"), "target_seat_id": int(t_sid)}


def _apply_momir_resolve(action: dict, state: dict) -> dict:
    """The DEFENDING player resolves a pending attack aimed at them (#111):

    * **Take it** (no blocks): lose the attacker's power in life (double for an
      unblocked double-striker).
    * **Block** (``block_indexes`` = your live untapped creatures; legacy single
      ``block_index`` still accepted): the keyword combat engine runs — first- /
      double-strike steps, deathtouch, trample, lifelink, indestructible.
    * **Cancel** (``cancel`` truthy): dismiss a mis-declared attack, no effect.

    Block legality: blockers must be untapped and alive; a flyer is blockable only
    by flying/reach; a menacing attacker needs 0 or ≥2 blockers. A dead/gone
    attacker fizzles."""
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

    # Resolve the declared blockers (block_indexes[], or the legacy single
    # block_index). Empty → the defender takes the hit.
    raw = action.get("block_indexes")
    if raw is None and action.get("block_index") is not None:
        raw = [action["block_index"]]
    block_idx = [i for i in (_coerce_int(x) for x in (raw or [])) if i is not None]
    if len(set(block_idx)) != len(block_idx):
        raise ValueError("a creature cannot block twice")
    d_tokens = state.get("tokens", {}).get(d_sid, [])
    blockers = []
    for i in block_idx:
        if not (0 <= i < len(d_tokens)) or d_tokens[i].get("alive") is False:
            raise ValueError("blocking creature not found or dead")
        if d_tokens[i].get("tapped"):
            raise ValueError("a tapped creature cannot block")
        blockers.append(d_tokens[i])

    if _has_kw(attacker, "flying") and any(
        not (_has_kw(b, "flying") or _has_kw(b, "reach")) for b in blockers
    ):
        raise ValueError("only creatures with flying or reach can block a flyer")
    if _has_kw(attacker, "menace") and 0 < len(blockers) < 2:
        raise ValueError("a menacing creature must be blocked by two or more creatures")

    return _run_combat(state, seq, entry["attacker_seat"], attacker, blockers, d_sid)


def _run_combat(
    state: dict, seq: int, a_sid: str, attacker: dict, blockers: list[dict], d_sid: str
) -> dict:
    """Deal combat damage between one attacker and its blockers (or the defending
    player when unblocked), in a first-strike step then a normal step. Marks
    damage on tokens, applies state-based deaths after each step, and accumulates
    trample / player damage and lifelink life gain. See #111 for the model."""
    trample = _has_kw(attacker, "trample")
    lifegain: dict[str, int] = {}
    dt_killed: set[int] = set()  # id() of tokens that took deathtouch damage
    player_dmg = 0

    def gain(sid: str, source: dict, amount: int) -> None:
        if amount > 0 and _has_kw(source, "lifelink"):
            lifegain[sid] = lifegain.get(sid, 0) + amount

    def deals_in(t: dict, step: str) -> bool:
        fs, ds = _has_kw(t, "first strike"), _has_kw(t, "double strike")
        return (fs or ds) if step == "first" else (ds or not fs)

    for step in ("first", "normal"):
        # Attacker assigns its power (lethal-then-next across blockers in order;
        # deathtouch makes 1 lethal; trample spills the rest to the player).
        if attacker.get("alive") is not False and deals_in(attacker, step):
            remaining = max(0, _eff_pow(attacker))
            live_blockers = [b for b in blockers if b.get("alive") is not False]
            if not blockers:  # unblocked → straight to the player
                player_dmg += remaining
                gain(a_sid, attacker, remaining)
            else:
                dt = _has_kw(attacker, "deathtouch")
                for b in live_blockers:
                    if remaining <= 0:
                        break
                    lethal = 1 if dt else max(1, _eff_tou(b) - int(b.get("damage", 0)))
                    assign = min(remaining, lethal)
                    b["damage"] = int(b.get("damage", 0)) + assign
                    if dt:
                        dt_killed.add(id(b))
                    gain(a_sid, attacker, assign)
                    remaining -= assign
                if trample and remaining > 0:
                    player_dmg += remaining
                    gain(a_sid, attacker, remaining)
        # Blockers deal to the attacker simultaneously (only while it's alive).
        if attacker.get("alive") is not False:
            for b in blockers:
                if b.get("alive") is False or not deals_in(b, step):
                    continue
                bp = max(0, _eff_pow(b))
                attacker["damage"] = int(attacker.get("damage", 0)) + bp
                if _has_kw(b, "deathtouch") and bp > 0:
                    dt_killed.add(id(attacker))
                gain(d_sid, b, bp)
        # State-based deaths after the step (before the next step deals).
        for t in [attacker, *blockers]:
            if t.get("alive") is False or _has_kw(t, "indestructible"):
                continue
            tou = _eff_tou(t)
            if id(t) in dt_killed or (tou > 0 and int(t.get("damage", 0)) >= tou):
                t["alive"] = False

    if player_dmg:
        state["lives"][d_sid] = int(state["lives"].get(d_sid, 0)) - player_dmg
    for sid, amount in lifegain.items():
        state["lives"][sid] = int(state["lives"].get(sid, 0)) + amount

    return {
        "seq": seq,
        "resolution": "blocked" if blockers else "unblocked",
        "attacker_name": attacker.get("name"),
        "blockers": [b.get("name") for b in blockers],
        "player_damage": player_dmg,
        "attacker_died": attacker.get("alive") is False,
        "blockers_died": [b.get("alive") is False for b in blockers],
        "life_gained": lifegain,
    }


def _advance_turn(session: Session, state: dict, game: Game, has_table: bool) -> int | None:
    """Advance ``currentTurnId`` to the next non-eliminated seat in physical
    clockwise order; increment ``turn`` when the rotation wraps past the first
    seat. In Momir, the seat the turn passes TO then begins its turn (untap +
    draw), which can deck it out. Returns the seat STARTING the next turn (the
    new ``currentTurnId``), which the caller stamps into the event payload."""
    # Combat ends with the turn: drop any attackers still awaiting a block
    # decision (Momir-only key — absent in Commander state, left untouched). Also
    # clear damage marked this turn (#111 tokens carry `damage`; setdefault-safe).
    if "attacks" in state:
        state["attacks"] = []
    for toks in state.get("tokens", {}).values():
        for tok in toks:
            if "damage" in tok:
                tok["damage"] = 0
    rot = turn_rotation(game)  # begins at the first seat
    if not rot:
        return None
    cand, wrapped = next_seat_in_rotation(
        rot, state.get("currentTurnId"), state.get("eliminated", {})
    )
    if cand is None:
        return state.get("currentTurnId")  # everyone eliminated → leave as-is
    state["currentTurnId"] = cand
    if wrapped:  # wrapped back to/through the first seat → new round
        state["turn"] = int(state.get("turn", 1)) + 1
    if _is_momir(game):
        _begin_momir_turn(session, game.id, cand, state, has_table)
    return cand


def _begin_momir_turn(
    session: Session, game_id: int, seat_id: int, state: dict, has_table: bool
) -> None:
    """Start-of-turn upkeep for a Momir seat (#110): untap all its lands, untap
    its creature tokens (#111 `tapped` flag), reset the land drop, then draw one —
    decking out (auto-elimination, cause ``deck``) if the library is empty."""
    sid = str(seat_id)
    # Untap this seat's creatures every turn — combat state, not a resource, so it
    # happens in physical mode too.
    for tok in state.get("tokens", {}).get(sid, []):
        tok["tapped"] = False
    if state.get("momirPhysical"):
        return  # real cards handle mana / draw / deck-out
    _ensure_res(state, sid)
    state["untapped"][sid] = int(state["lands"][sid])
    state["landPlayed"][sid] = False
    if int(state["library"][sid]) <= 0:  # forced draw on an empty library → decked
        _deck_out(session, game_id, seat_id, state, has_table)
        return
    state["library"][sid] = int(state["library"][sid]) - 1
    state["hand"][sid] = int(state["hand"][sid]) + 1


def _deck_out(session: Session, game_id: int, seat_id: int, state: dict, has_table: bool) -> None:
    """Auto-eliminate a seat that tried to draw from an empty library (#110). Rides
    the auto-elim infra but is triggered here (not via ``_loss_cause``, which is
    life/poison/cmd only — deck-out has no life-state signal). The ``deck`` cause
    is permanent, protected from auto-revive like ``manual``."""
    sid = str(seat_id)
    if state.get("eliminated", {}).get(sid):
        return  # already out
    state.setdefault("eliminated", {})[sid] = True
    state.setdefault("eliminatedAtTurn", {})[sid] = int(state.get("turn", 1))
    state.setdefault("eliminationCause", {})[sid] = "deck"
    _append_auto_elim_event(session, game_id, seat_id, "deck", True, state, has_table)


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
    user_id: int | None = None,
) -> None:
    """Append one GameEvent for a live action — inside the action's transaction
    (the caller commits). The table/csrf tokens are stripped from the payload; the
    acting user is stamped as ``actor_user_id`` (#112 — cross-seat ability
    primitives can be resolved by a non-owning player, so the audit needs who)."""
    payload = {k: v for k, v in action.items() if k not in _SENSITIVE_KEYS}
    payload.update(extras)  # cmd's raw_delta / actual_delta
    payload["actor_user_id"] = user_id
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
    # A seat with TWO commanders shares one counter here, but the rules track 21
    # from EACH commander separately — so its total crossing 21 proves nothing
    # and must not auto-eliminate anyone. The UI flags those cells instead.
    # Missing key (a blob written before v4.13.20) = no partner seats = the old
    # behaviour, unchanged.
    partners = set(state.get("partnerSeats") or [])
    cmd_map = {a: v for a, v in state.get("cmd", {}).get(sid, {}).items() if a not in partners}
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
    # #112 — damage to a SEAT (no index) drains its life; token damage does not.
    if atype == "momir_damage" and action.get("index") is None:
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
    # Manual and deck-out (#110) eliminations are permanent — never auto-revive
    # (deck-out has no _loss_cause signal, so a life mutation would otherwise
    # "recover" a decked player).
    if currently and causes.get(sid) in ("manual", "deck"):
        return

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
    # #155 instrumentation — the clock starts here because the read that can go
    # stale (get_viewable_game → game.live_state) happens below, so this is the
    # true start of the race window, not the route's request boundary.
    t0 = time.perf_counter()
    started_iso = utc_now().isoformat()
    with _track_in_flight(game_id) as overlap:
        return _apply_live_action_inner(
            session, game_id, user_id, action, table_token, t0, started_iso, overlap
        )


def _apply_live_action_inner(
    session: Session,
    game_id: int,
    user_id: int,
    action: dict,
    table_token: str | None,
    t0: float,
    started_iso: str,
    overlap: int,
) -> GameLiveState:
    """The body of :func:`apply_live_action`, unchanged except for the #155 record
    emitted after commit. Split out only so the in-flight counter wraps every exit
    path (including the raises) without indenting the whole function."""
    game = get_viewable_game(session, game_id, user_id)
    if game is None:
        raise LookupError("Game not found or not viewable")
    live = game.live_state
    if live is None:
        raise LookupError("No live state for this game")
    version_read = live.version

    atype = (action or {}).get("type")
    if atype not in _MUTATING_TYPES:
        raise ValueError(f"Unknown action type: {atype!r}")
    # Momir actions (+ the tokens state field) exist ONLY in Momir games; a
    # Commander game rejects them so its state stays untouched.
    if atype in _MOMIR_TYPES and not _is_momir(game):
        raise ValueError("Momir actions are only valid in Momir games")

    seats_by_id = {s.id: s for s in game.seats}
    _validate_action_seats(atype, action, seats_by_id)  # 400 on bad seat, table path included

    state = json.loads(live.state)
    has_table = bool(table_token) and table_token == game.client_token
    if not has_table:
        _authorize_seat_scoped(atype, action, game, user_id, seats_by_id, state)

    extras = _apply_mutation(session, atype, action, state, game, has_table)

    # Event append shares this transaction — no event without its mutation. The
    # triggering event is recorded first, then any auto-elimination/revive it
    # caused appends its own eliminate event (state serialized AFTER both).
    _append_event(session, game_id, atype, action, state, has_table, extras, user_id)
    affected = _affected_seat(atype, action)
    if affected is not None:
        _auto_eliminate(session, game_id, affected, state, has_table)

    live.state = json.dumps(state)
    live.version += 1
    live.updated_at = utc_now()
    session.commit()
    _record_live_action(
        game_id, user_id, atype, version_read, live.version, started_iso, t0, overlap
    )
    _publish(live)
    return live
