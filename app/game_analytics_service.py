"""Per-game analytics reconstructed from the ``game_events`` stream (#95, Phase 3
of the game-event-history spec).

Everything here is a replay of taps players already made in live mode — no new
data collection. ``build_game_analytics`` does ONE ordered pass over the events
and returns a dict the finalized-game template renders (life-over-time SVG,
elimination timeline, commander-damage matrix, pace). Games with no events
(pre-v4.3 / localStorage tracker games) return ``None`` → the section hides.

Replay rules that must match live_game_service exactly (never re-derived here):
  * life event  → ``lives[seat_id] += payload.delta``
  * cmd event   → ``lives[receiver_seat_id] -= payload.actual_delta`` (the coupled,
                  post-floor life change the service already computed)
The ``live_started`` / ``finalized`` bookend payloads are the initial / final
state blobs, so seats, final cmd grid, and eliminations read straight off them.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import Game, GameEvent
from app.playgroup_service import GUESTS_LABEL  # #158 reuses #152's guest-row convention

# Distinct, theme-agnostic seat colors (max 6 seats in a Commander pod).
_PALETTE = ["#3fb950", "#58a6ff", "#f85149", "#d29922", "#bc8cff", "#39c5cf"]
# Pace bar for a segment whose owner couldn't be derived (seatless game, or a
# stamped seat that has since been deleted) — neutral, never a wrong player's color.
_UNKNOWN_SEAT_COLOR = "#8b949e"

# Life chart SVG geometry (viewBox units; rendered responsive via width:100%).
_W, _H, _PAD = 340, 140, 10
# Width of the stub drawn for a seat eliminated before it ever took damage — a
# one-point polyline renders nothing, and that seat must not vanish from the chart.
_STUB_W = 3.0


def _seat_label(seat) -> str:
    return seat.player_name or f"Seat {seat.seat_number}"


def life_delta_for_event(event: GameEvent) -> tuple[str, int] | None:
    """``(seat_id_string, signed life change)`` for a life-affecting event, else
    ``None``.

    THE one definition of how a `game_events` row moves a life total. The chart
    replay and the #153 consistency check both go through it, so a checker can
    never report a false divergence by re-deriving the rule slightly differently.

    Only ``life`` and ``cmd`` qualify in a Commander game — see the
    ``state["lives"]`` writer table in #153; the two Momir writers
    (``momir_damage`` seat targets, ``_run_combat``) mutate life WITHOUT a
    reconstructable per-event delta in the payload, which is why
    :func:`check_life_consistency` refuses to judge a Momir game."""
    if event.action_type == "life":
        p = json.loads(event.payload)
        return str(p.get("seat_id")), int(p.get("delta", 0))
    if event.action_type == "cmd":
        # The coupled, post-floor life change the service already computed.
        # NEVER re-derive the floor rule here.
        p = json.loads(event.payload)
        return str(p.get("receiver_seat_id")), -int(p.get("actual_delta", 0))
    return None


def _segment_owners(game: Game, events: list[GameEvent], init: dict) -> list[str]:
    """Seat-id STRING owning each pace segment, in order — ``[starting seat,
    owner after turn event 0, owner after turn event 1, ...]``.

    A ``turn`` event carries ``seat_id IS NULL`` by design (``_event_seat_id``),
    and ``actor_user_id`` is neither present on pre-#112 games nor a reliable
    owner signal (whoever tapped, not whose turn it was). So the owner is
    DERIVED: read the ``active_seat_id`` the live service now stamps into the
    turn payload, and fall back to replaying the same rotation for games that
    predate the stamp.

    Two things the replay must get right or it desyncs permanently:
      * rotation follows ``turn_rotation`` (CLOCKWISE grid_position order), NOT
        seat_number order — they differ in real pods. Colors stay on seat_number
        order, so the life chart is unaffected.
      * elimination state is replayed from the event stream (manual + ``auto``
        eliminate events, including revives), because an eliminated seat is
        skipped and a wrong set shifts every later segment.
    """
    # Imported here, not at module scope: live_game_service imports game_service,
    # and analytics is a leaf that only needs the two rotation helpers.
    from app.live_game_service import next_seat_in_rotation, turn_rotation

    rot = turn_rotation(game)
    eliminated = {str(k): bool(v) for k, v in (init.get("eliminated") or {}).items()}
    current = rot[0] if rot else None
    owners = [str(current)]
    for e in events:
        if e.action_type == "eliminate":
            p = json.loads(e.payload)
            # Manual eliminates name the seat in the payload, auto ones only in
            # the column — the column is set for both, so prefer it.
            sid = e.seat_id if e.seat_id is not None else p.get("seat_id")
            if sid is not None:
                eliminated[str(sid)] = bool(p.get("eliminated"))
        elif e.action_type == "turn":
            stamped = json.loads(e.payload).get("active_seat_id")
            if stamped is not None:
                current = int(stamped)
            else:
                nxt, _ = next_seat_in_rotation(rot, current, eliminated)
                current = nxt if nxt is not None else current
            owners.append(str(current))
    return owners


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m}m"
    return f"{m}m {s:02d}s" if m else f"{s}s"


def build_game_analytics(session: Session, game_id: int) -> dict | None:
    game = session.get(Game, game_id)
    if game is None:
        return None
    events = (
        session.query(GameEvent)
        .filter(GameEvent.game_id == game_id)
        .order_by(GameEvent.created_at.asc(), GameEvent.id.asc())
        .all()
    )
    started = next((e for e in events if e.action_type == "live_started"), None)
    if started is None:
        return None  # not a recorded live game — nothing to replay
    finalized = next((e for e in events if e.action_type == "finalized"), None)

    init = json.loads(started.payload)
    final = json.loads(finalized.payload) if finalized else init

    seats = sorted(game.seats, key=lambda s: s.seat_number)
    seat_ids = [str(s.id) for s in seats]
    labels = {str(s.id): _seat_label(s) for s in seats}
    colors = {sid: _PALETTE[i % len(_PALETTE)] for i, sid in enumerate(seat_ids)}
    if not seat_ids:
        return None

    init_lives = init.get("lives", {})
    lives0 = {str(s.id): int(init_lives.get(str(s.id), s.starting_life)) for s in seats}
    max_turn = max((e.turn for e in events), default=1)

    # ── 1. Life-over-time: one sample per life-affecting RUN (#151, #161) ────
    # #151: the x-axis was one point per ROUND, which discarded every intra-round
    # swing — `changed_at[sid][e.turn]` overwrote, so only a round's last value
    # survived, and `game_events.turn` only increments when the rotation wraps past
    # the first seat, so in a 4-seat pod one x-position spanned four seat turns.
    # Per-event sampling fixed that and is NOT being reverted.
    #
    # #161: but players enter damage by tapping ±1 repeatedly, so one hit for 12 drew
    # as twelve one-life steps — a staircase where the game had a cliff. 824 of the
    # 1075 life-affecting events on record are `life` taps and almost all are ±1.
    # Consecutive samples are therefore COLLAPSED into a single x-position while all
    # three of these hold, and a new column starts when any one changes:
    #
    #   1. same affected seat   (payload.seat_id / receiver_seat_id, per
    #                            life_delta_for_event — never re-derived here)
    #   2. same round           (e.turn)
    #   3. same direction       (sign of the delta)
    #
    # Measured on the recorded corpus: **678 of 1075 x-positions absorbed (63.1%)**.
    # #161 predicted 689 (64.1%) from a three-guard rule; the elimination break below
    # is a fourth guard the issue did not model, and it costs exactly 11 columns —
    # runs where an `eliminate` event sits between two otherwise-continuous samples.
    # Worth the 11: without it a revived seat's post-revive value folds into a column
    # that sits BEFORE its own `cut_at`, i.e. on the far side of its truncation.
    #
    # **The direction guard is not optional.** Without it this reintroduces exactly
    # the bug #151 fixed: a seat going 40 → 10 → 35 with nobody acting in between is
    # two consecutive same-seat samples, and merging them renders 40 → 35 and erases
    # the trough. `test_a_drop_and_recovery_inside_one_rotation_both_render` is that
    # case and must keep passing with its assertions unchanged.
    #
    # **The round guard** stops a run straddling a boundary, which would otherwise put
    # the round tick at the same x as the collapsed point. It costs 11 of 700 absorbed
    # points — the faithful behaviour is nearly free.
    #
    # Collapsing happens AT APPEND TIME, not as a post-process, because every consumer
    # below is index-based: `cut_at` (set from the live `idx`), `round_ticks`,
    # `n_samples`, `x_span`, and `_step_points` walking each row positionally. Doing it
    # here keeps all of them consistent for free; post-processing would need every one
    # remapped, and a missed remap puts round ticks and elimination cutoffs at the
    # wrong x with no test failure to catch it.
    cur = dict(lives0)
    samples: dict[str, list[int]] = {sid: [lives0[sid]] for sid in seat_ids}
    # Sample index at which each seat was last eliminated (None = still in, or
    # revived). Read from the EVENT STREAM, never from the finalized blob: the blob
    # gives the fact, not the position, and cannot tell an elimination that stuck from
    # one that was reverted (auto-eliminations auto-revive — see _auto_eliminate).
    cut_at: dict[str, int | None] = dict.fromkeys(seat_ids)
    round_ticks: list[dict] = []
    current_round = 1
    idx = 0
    # #161 — the open run: which seat, which round, which direction. `run_sign` of 0
    # never continues a run, so a no-op (delta == 0) event always starts its own
    # column rather than silently joining its neighbour.
    run_sid: str | None = None
    run_round: int | None = None
    run_sign = 0

    for e in events:
        if e.action_type == "eliminate":
            p = json.loads(e.payload)
            # Manual eliminates name the seat in the payload, auto ones only in the
            # column — the column is set for both, so prefer it.
            raw = e.seat_id if e.seat_id is not None else p.get("seat_id")
            sid = str(raw) if raw is not None else None
            if sid in cut_at:
                cut_at[sid] = idx if p.get("eliminated") else None
            # #161 — an elimination always breaks the open run. Otherwise a seat
            # eliminated mid-run and revived could have its post-revive value folded
            # back into a column that sits BEFORE its own `cut_at`, putting the value
            # on the far side of the truncation.
            run_sid = run_round = None
            run_sign = 0
            continue
        hit = life_delta_for_event(e)
        if hit is None:
            continue
        sid, delta = hit
        sign = (delta > 0) - (delta < 0)
        continues_run = sid == run_sid and e.turn == run_round and sign != 0 and sign == run_sign
        if sid in cur:
            cur[sid] += delta
        if continues_run:
            # Same seat, same round, same direction — fold into the current column so
            # a run of twelve ±1 taps reads as one drop. Every seat is rewritten, not
            # just the actor, so the carried-forward rows stay the same length.
            for s in seat_ids:
                samples[s][-1] = cur[s]
        else:
            idx += 1
            for s in seat_ids:
                samples[s].append(cur[s])
            run_sid, run_round, run_sign = sid, e.turn, sign
        if e.turn > current_round:
            current_round = e.turn
            round_ticks.append({"index": idx, "round": e.turn})

    n_samples = idx + 1

    # Truncate at elimination, inclusive. A seat with no eliminate event — including
    # one marked out only at finalize — keeps its full row and runs to the right edge;
    # do NOT synthesize a cut from `eliminatedAtTurn`, which is round-grained and would
    # land in the wrong place at event resolution.
    series_values: list[list[int]] = []
    for sid in seat_ids:
        end = cut_at[sid]
        series_values.append(samples[sid] if end is None else samples[sid][: end + 1])

    flat = [v for row in series_values for v in row] or [0]
    lo, hi = min(flat), max(flat)
    span = hi - lo or 1
    x_span = (n_samples - 1) or 1

    def _x(sample_idx: int) -> float:
        return _PAD + sample_idx / x_span * (_W - 2 * _PAD)

    def _y(life: int) -> float:
        return _PAD + (hi - life) / span * (_H - 2 * _PAD)

    def _step_points(row: list[int]) -> str:
        """Step (not diagonal) rendering: hold the old value across to the new x, then
        drop vertically. Life moves in discrete jumps, and a diagonal reads as a
        gradual slide."""
        y0 = _y(row[0])
        if len(row) == 1:
            # Eliminated before any life change. A one-point polyline draws nothing,
            # so emit a short visible stub instead of losing the seat entirely.
            return f"{_x(0):.1f},{y0:.1f} {_x(0) + _STUB_W:.1f},{y0:.1f}"
        out = [f"{_x(0):.1f},{y0:.1f}"]
        for i in range(1, len(row)):
            x = _x(i)
            out.append(f"{x:.1f},{_y(row[i - 1]):.1f}")  # across at the old value
            out.append(f"{x:.1f},{_y(row[i]):.1f}")  # then down/up to the new one
        return " ".join(out)

    life_series = []
    for sid, row in zip(seat_ids, series_values, strict=True):
        life_series.append(
            {
                "sid": sid,
                "label": labels[sid],
                "color": colors[sid],
                "points": _step_points(row),
                # After truncation this is life AT ELIMINATION, not the recorded
                # `final_life` — the two differ by construction for every cmd/poison
                # death (killed at 21 commander damage while still on positive life).
                # The template labels it; the standings table above carries the
                # authoritative number. See #154.
                "final": row[-1],
                "ended_at_elimination": cut_at[sid] is not None,
            }
        )
    life_chart = {
        "width": _W,
        "height": _H,
        "max_turn": max_turn,
        "life_lo": lo,
        "life_hi": hi,
        "series": life_series,
        # #154 — drives the explanatory note. Only shown when at least one line is
        # actually truncated, so a game with no eliminations carries no caveat about
        # a case it doesn't have. Computed here rather than in Jinja: `selectattr`
        # over a list of dicts relies on Jinja's attribute/item fallback, and an
        # explicit boolean is one line and cannot surprise anyone.
        "any_truncated": any(s["ended_at_elimination"] for s in life_series),
        # Faint verticals where the rotation wrapped — the only surviving use of
        # `e.turn` on this axis.
        "round_ticks": [{"x": round(_x(t["index"]), 1), "round": t["round"]} for t in round_ticks],
    }

    # ── 2. Elimination timeline — ORDER from the event stream ─────────────────
    # Membership still comes from the final blob, but the ORDER does not.
    # `eliminatedAtTurn` is round-grained, so two seats out in the same round tie and
    # were broken by seat id — game 67 reported Phil before Alex although Alex died
    # first (events 1602 vs 1618). `_final_elimination_events` orders by the event
    # that actually removed each seat, and handles revives (the LAST elimination that
    # stuck), matching #151's truncation rule and #158's first-out statistic.
    #
    # Membership is equivalent by construction: every write to the blob's `eliminated`
    # map is accompanied by an eliminate event (`_apply_mutation`, `_auto_eliminate`,
    # `_deck_out` all append one), verified 0 blob-only eliminations across prod. The
    # second clause is belt-and-braces so an unforeseen write path cannot make a seat
    # silently vanish from the timeline — it appends by round rather than dropping.
    elim = final.get("eliminated", {})
    at_turn = final.get("eliminatedAtTurn", {})
    causes = final.get("eliminationCause", {})
    out_ids = [sid for sid, _e in _final_elimination_events(events) if sid in labels]
    out_ids += sorted(
        (sid for sid in seat_ids if elim.get(sid) and sid not in out_ids),
        key=lambda s: (int(at_turn.get(s, 0)), int(s)),
    )
    timeline = []
    total = len(seat_ids)
    for k, sid in enumerate(out_ids, start=1):
        timeline.append(
            {
                "label": labels[sid],
                "color": colors[sid],
                "turn": int(at_turn.get(sid, 0)) or None,
                "cause": causes.get(sid),
                "remaining": total - k,
            }
        )

    # ── 3. Commander-damage matrix (final cmd grid; lethal = a single source ≥21) ─
    cmd = final.get("cmd", {})
    matrix_rows = []
    for recv in seat_ids:
        row_map = cmd.get(recv, {})
        cells = []
        for atk in seat_ids:
            # v4.13.21 — a source key is "<seat>" or "<seat>:<card>" (one per
            # commander of a partner seat). The CELL shows the seat's total,
            # which is what a damage matrix means; LETHAL is per source, since
            # 21 is per commander and summing two partners is the bug this
            # keying exists to end.
            sources = [
                int(v) for key, v in row_map.items() if key == atk or str(key).startswith(f"{atk}:")
            ]
            value = sum(sources) if atk != recv else 0
            lethal = any(v >= 21 for v in sources) if atk != recv else False
            cells.append({"value": value, "lethal": lethal, "self": atk == recv})
        matrix_rows.append({"label": labels[recv], "sid": recv, "cells": cells})
    cmd_matrix = {
        "columns": [{"label": labels[sid], "sid": sid, "color": colors[sid]} for sid in seat_ids],
        "rows": matrix_rows,
        "any": any(
            int(v) > 0
            for r in seat_ids
            for key, v in cmd.get(r, {}).items()
            if str(key).split(":")[0] != r
        ),
    }

    # ── 4. Pace: turn durations from turn-advance timestamps + total wall clock ─
    turn_events = [e for e in events if e.action_type == "turn"]
    turn_marks = [started] + turn_events  # started opens turn 1
    seg_owners = _segment_owners(game, events, init)
    turns = []
    for i in range(len(turn_marks) - 1):
        secs = (turn_marks[i + 1].created_at - turn_marks[i].created_at).total_seconds()
        sid = seg_owners[i] if i < len(seg_owners) else None
        turns.append(
            {
                "turn": i + 1,  # segment index (NOT the round — see "round" below)
                "round": turn_marks[i].turn,  # the round this segment was played in
                "seconds": secs,
                "label": _fmt_duration(secs),
                "sid": sid,
                "player": labels.get(sid, "—"),
                "color": colors.get(sid, _UNKNOWN_SEAT_COLOR),
            }
        )
    last = finalized or (events[-1] if events else started)
    total_seconds = (last.created_at - started.created_at).total_seconds()
    longest = max((t["seconds"] for t in turns), default=0) or 1
    for t in turns:
        t["pct"] = round(t["seconds"] / longest * 100, 1)
    pace = {
        "total": _fmt_duration(total_seconds),
        "total_seconds": total_seconds,
        "turn_count": max_turn,
        "avg_turn": _fmt_duration(total_seconds / max_turn) if max_turn else "—",
        "turns": turns,
    }

    return {
        "life_chart": life_chart,
        "timeline": timeline,
        "cmd_matrix": cmd_matrix,
        "pace": pace,
    }


# ── #153 — life-total consistency check ───────────────────────────────────────
# Diagnostic only. Reads nothing but `game_events` + `game_seats`, writes nothing,
# and is NOT wired into any page. Exists so the divergence in #153 is measurable
# repeatedly rather than re-derived by hand each time someone looks.
#
# THREE artifacts claim to know a seat's final life, with three different
# provenances (#153):
#   blob       — the `finalized` bookend payload. SERVER state at end_game time.
#   replay     — `live_started` baseline + every life/cmd event delta. SERVER,
#                reconstructed. Should equal `blob`; in 10 known seats it does not.
#   final_life — `game_seats.final_life`. CLIENT-submitted: the End-game modal
#                posts `final_life_{seat_id}`, prefilled at modal-open from the
#                browser's in-memory state and hand-editable before submit. A gap
#                here needs NO server bug to explain, so it is reported separately
#                and must not be conflated with replay-vs-blob.


def check_life_consistency(session: Session, game_id: int) -> dict | None:
    """Per-seat reconciliation of the three life artifacts for one game.

    Returns ``None`` when the game has no ``live_started`` event (a pre-v4.3 /
    localStorage game — nothing to replay), or when it is a Momir game, whose
    ``momir_damage`` / combat life changes carry no reconstructable per-event
    delta (see :func:`life_delta_for_event`) and would produce false divergences.
    """
    game = session.get(Game, game_id)
    if game is None:
        return None
    if (game.format or "").casefold() == "momir":
        return None
    events = (
        session.query(GameEvent)
        .filter(GameEvent.game_id == game_id)
        .order_by(GameEvent.created_at.asc(), GameEvent.id.asc())
        .all()
    )
    started = next((e for e in events if e.action_type == "live_started"), None)
    if started is None:
        return None
    finalized = next((e for e in events if e.action_type == "finalized"), None)

    baseline = json.loads(started.payload).get("lives", {})
    blob = json.loads(finalized.payload).get("lives", {}) if finalized else None

    replay = {str(k): int(v) for k, v in baseline.items()}
    for e in events:
        hit = life_delta_for_event(e)
        if hit is None:
            continue
        sid, delta = hit
        if sid in replay:
            replay[sid] += delta

    seats = []
    for seat in sorted(game.seats, key=lambda s: s.seat_number):
        sid = str(seat.id)
        b = int(blob[sid]) if blob is not None and sid in blob else None
        r = replay.get(sid)
        f = seat.final_life
        seats.append(
            {
                "seat_id": seat.id,
                "label": _seat_label(seat),
                "baseline": baseline.get(sid),
                "replay": r,
                "blob": b,
                "final_life": f,
                # replay - blob: SERVER-side divergence. Non-zero is the #153 defect.
                "replay_vs_blob": None if (b is None or r is None) else r - b,
                # final_life - blob: the CLIENT-form gap. Non-zero needs no server bug.
                "final_vs_blob": None if (b is None or f is None) else f - b,
            }
        )
    return {
        "game_id": game.id,
        "format": game.format,
        "status": game.status,
        "has_finalized": finalized is not None,
        "seats": seats,
        "replay_diverged": [s for s in seats if s["replay_vs_blob"] not in (None, 0)],
        "final_diverged": [s for s in seats if s["final_vs_blob"] not in (None, 0)],
    }


def scan_life_consistency(session: Session) -> list[dict]:
    """:func:`check_life_consistency` over every game that has events, newest first.
    Games it cannot judge (no ``live_started``, or Momir) are skipped."""
    game_ids = [
        gid
        for (gid,) in session.query(GameEvent.game_id).distinct().order_by(GameEvent.game_id.desc())
    ]
    return [r for r in (check_life_consistency(session, gid) for gid in game_ids) if r is not None]


# ── #158 — cross-game pod dynamics + pace ─────────────────────────────────────
# Aggregates ACROSS recorded live games. Split out of #96 because its gate is
# different: #96's per-deck surfaces need repeat samples of the same deck (1.8
# games each today), while these are player-level and the roster is stable, so
# each player already has 4-8 observations.
#
# Scope is the viewer's PARTICIPANT set (`_participant_games_predicate` — owned +
# played-in, the same definition the Recent Games list uses), narrowed to games
# that actually have an event stream. A game with no `live_started` is EXCLUDED,
# never counted as a zero — it has no data, which is not the same as no time.


def _final_elimination_events(events: list[GameEvent]) -> list[tuple[str, GameEvent]]:
    """``[(seat_id_str, the eliminate event that finally removed it)]``, in event order.

    A seat can be eliminated and revived — manually, or automatically when its own
    loss condition un-triggers (`_auto_eliminate`) — so the ordering key is the LAST
    elimination that STUCK, not the first that fired. Same rule #151 uses to decide
    where to truncate a chart line.

    Deliberately NOT read from the finalized blob's `eliminated` / `eliminatedAtTurn`
    maps: those are round-grained, so two seats out in the same round tie and get
    broken by seat id. Real example — game 67, where Alex (event 1602) died before
    Phil (1618) but the blob ordering reports Phil first. First-out frequency has to
    be right about exactly that case.
    """
    last: dict[str, GameEvent] = {}
    for e in events:
        if e.action_type != "eliminate":
            continue
        p = json.loads(e.payload)
        # Manual eliminates name the seat in the payload, auto ones only in the
        # column — the column is set for both, so prefer it.
        raw = e.seat_id if e.seat_id is not None else p.get("seat_id")
        if raw is None:
            continue
        sid = str(raw)
        if p.get("eliminated"):
            last[sid] = e
        else:
            last.pop(sid, None)  # revived — no longer out
    return sorted(last.items(), key=lambda kv: (kv[1].created_at, kv[1].id))


def _player_key(seat) -> tuple:
    """Aggregation key for a player across games.

    Grouped by ``user_id``, NEVER ``player_name`` — the same rule #152 established,
    for the same reason: `player_name` is a per-seat free-text snapshot and one
    account really has played under several spellings. Unattributed seats collapse
    into a single Guests row rather than one row per placeholder name.
    """
    return ("u", seat.user_id) if seat.user_id is not None else ("guest",)


def build_cross_game_analytics(session: Session, user_id: int) -> dict | None:
    """Pod dynamics + pace across every recorded live game the viewer was part of.

    Returns ``None`` when no qualifying game has an event stream — the caller hides
    the section rather than rendering zeros.

    Every figure carries its own sample size. With 8 recorded games and pod sizes of
    only 4 and 5 (four games each), a bare average would imply far more than the data
    supports; the template is expected to show ``n`` beside each number.
    """
    from app.game_service import _participant_games_predicate

    games = (
        session.query(Game)
        .filter(_participant_games_predicate(user_id))
        .order_by(Game.played_at.desc(), Game.id.desc())
        .all()
    )
    if not games:
        return None

    rows = (
        session.query(GameEvent)
        .filter(GameEvent.game_id.in_([g.id for g in games]))
        .order_by(GameEvent.created_at.asc(), GameEvent.id.asc())
        .all()
    )
    by_game: dict[int, list[GameEvent]] = {}
    for e in rows:
        by_game.setdefault(e.game_id, []).append(e)

    labels: dict[tuple, str] = {}
    first_out: dict[tuple, int] = {}
    positions: dict[tuple, dict[int, int]] = {}
    survived: dict[tuple, int] = {}
    appearances: dict[tuple, int] = {}
    seg_time: dict[tuple, float] = {}
    seg_count: dict[tuple, int] = {}
    pod: dict[int, list[float]] = {}
    per_game: list[dict] = []

    for g in games:
        events = by_game.get(g.id, [])
        started = next((e for e in events if e.action_type == "live_started"), None)
        if started is None:
            continue  # no event stream — excluded, NOT counted as a zero
        seats = {str(s.id): s for s in g.seats}
        if not seats:
            continue
        for s in g.seats:
            key = _player_key(s)
            # Games are ordered newest-first, so setdefault keeps the MOST RECENT
            # spelling for an account that has played under several — the same
            # reason #152 groups on user_id in the first place.
            labels.setdefault(key, GUESTS_LABEL if key == ("guest",) else _seat_label(s))
            appearances[key] = appearances.get(key, 0) + 1

        # ── elimination order, from the event stream (see _final_elimination_events)
        order = [sid for sid, _e in _final_elimination_events(events) if sid in seats]
        for place, sid in enumerate(order, start=1):
            key = _player_key(seats[sid])
            positions.setdefault(key, {})[place] = positions.setdefault(key, {}).get(place, 0) + 1
            if place == 1:
                first_out[key] = first_out.get(key, 0) + 1
        for sid, seat in seats.items():
            if sid not in order:
                key = _player_key(seat)
                survived[key] = survived.get(key, 0) + 1

        # ── pace: total wall clock, and per-segment time attributed via _segment_owners
        finalized = next((e for e in events if e.action_type == "finalized"), None)
        last = finalized or events[-1]
        total = (last.created_at - started.created_at).total_seconds()
        pod.setdefault(len(seats), []).append(total)

        owners = _segment_owners(g, events, json.loads(started.payload))
        marks = [started] + [e for e in events if e.action_type == "turn"]
        for i in range(len(marks) - 1):
            sid = owners[i] if i < len(owners) else None
            seat = seats.get(sid)
            if seat is None:
                continue
            key = _player_key(seat)
            secs = (marks[i + 1].created_at - marks[i].created_at).total_seconds()
            seg_time[key] = seg_time.get(key, 0.0) + secs
            seg_count[key] = seg_count.get(key, 0) + 1

        per_game.append(
            {
                "game_id": g.id,
                "played_at": g.played_at,
                "pod_size": len(seats),
                "order": [_seat_label(seats[sid]) for sid in order],
                "survivors": [_seat_label(s) for sid, s in seats.items() if sid not in order],
                "total_label": _fmt_duration(total),
            }
        )

    if not per_game:
        return None

    total_games = len(per_game)
    max_place = max((p for d in positions.values() for p in d), default=0)

    players = []
    for key, label in labels.items():
        fo = first_out.get(key, 0)
        seen = appearances.get(key, 0)
        players.append(
            {
                "label": label,
                "games": seen,
                "first_out": fo,
                # Share of THIS player's games, not of all games — they did not sit
                # in every one.
                "first_out_pct": round(100.0 * fo / seen) if seen else 0,
                "survived": survived.get(key, 0),
                "positions": [positions.get(key, {}).get(p, 0) for p in range(1, max_place + 1)],
                "turns": seg_count.get(key, 0),
                "avg_turn_label": (
                    _fmt_duration(seg_time[key] / seg_count[key]) if seg_count.get(key) else "—"
                ),
                "avg_turn_seconds": (
                    round(seg_time[key] / seg_count[key], 1) if seg_count.get(key) else None
                ),
            }
        )
    players.sort(key=lambda r: (-r["first_out"], -r["games"], r["label"].lower()))

    return {
        "games": total_games,
        "max_place": max_place,
        "players": players,
        "per_game": per_game,
        "pace_by_pod": [
            {
                "pod_size": size,
                "games": len(vals),  # sample size — must be rendered beside the average
                "avg_label": _fmt_duration(sum(vals) / len(vals)),
                "avg_seconds": round(sum(vals) / len(vals), 1),
            }
            for size, vals in sorted(pod.items())
        ],
    }
