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

# Distinct, theme-agnostic seat colors (max 6 seats in a Commander pod).
_PALETTE = ["#3fb950", "#58a6ff", "#f85149", "#d29922", "#bc8cff", "#39c5cf"]

# Life chart SVG geometry (viewBox units; rendered responsive via width:100%).
_W, _H, _PAD = 340, 140, 10


def _seat_label(seat) -> str:
    return seat.player_name or f"Seat {seat.seat_number}"


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

    # ── 1. Life-over-time: replay life + cmd, snapshotting each seat per turn ──
    cur = dict(lives0)
    changed_at: dict[str, dict[int, int]] = {sid: {} for sid in seat_ids}
    for e in events:
        if e.action_type == "life":
            p = json.loads(e.payload)
            sid = str(p.get("seat_id"))
            if sid in cur:
                cur[sid] += int(p.get("delta", 0))
                changed_at[sid][e.turn] = cur[sid]
        elif e.action_type == "cmd":
            p = json.loads(e.payload)
            sid = str(p.get("receiver_seat_id"))
            if sid in cur:
                cur[sid] -= int(p.get("actual_delta", 0))
                changed_at[sid][e.turn] = cur[sid]

    # Carry each seat's life forward across turns with no change of its own.
    series_values: list[list[int]] = []
    for sid in seat_ids:
        val = lives0[sid]
        row = []
        for t in range(1, max_turn + 1):
            if t in changed_at[sid]:
                val = changed_at[sid][t]
            row.append(val)
        series_values.append(row)

    flat = [v for row in series_values for v in row] or [0]
    lo, hi = min(flat), max(flat)
    span = hi - lo or 1
    x_span = (max_turn - 1) or 1

    def _x(turn_idx: int) -> float:
        return _PAD + turn_idx / x_span * (_W - 2 * _PAD)

    def _y(life: int) -> float:
        return _PAD + (hi - life) / span * (_H - 2 * _PAD)

    life_series = []
    for sid, row in zip(seat_ids, series_values, strict=True):
        points = " ".join(f"{_x(i):.1f},{_y(v):.1f}" for i, v in enumerate(row))
        life_series.append(
            {
                "sid": sid,
                "label": labels[sid],
                "color": colors[sid],
                "points": points,
                "final": row[-1],
            }
        )
    life_chart = {
        "width": _W,
        "height": _H,
        "max_turn": max_turn,
        "life_lo": lo,
        "life_hi": hi,
        "series": life_series,
    }

    # ── 2. Elimination timeline (from the final blob) ─────────────────────────
    elim = final.get("eliminated", {})
    at_turn = final.get("eliminatedAtTurn", {})
    causes = final.get("eliminationCause", {})
    out_ids = sorted(
        (sid for sid in seat_ids if elim.get(sid)),
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
            value = int(row_map.get(atk, 0)) if atk != recv else 0
            cells.append({"value": value, "lethal": value >= 21, "self": atk == recv})
        matrix_rows.append({"label": labels[recv], "sid": recv, "cells": cells})
    cmd_matrix = {
        "columns": [{"label": labels[sid], "sid": sid, "color": colors[sid]} for sid in seat_ids],
        "rows": matrix_rows,
        "any": any(int(cmd.get(r, {}).get(a, 0)) > 0 for r in seat_ids for a in seat_ids if a != r),
    }

    # ── 4. Pace: turn durations from turn-advance timestamps + total wall clock ─
    turn_events = [e for e in events if e.action_type == "turn"]
    turn_marks = [started] + turn_events  # started opens turn 1
    turns = []
    for i in range(len(turn_marks) - 1):
        secs = (turn_marks[i + 1].created_at - turn_marks[i].created_at).total_seconds()
        turns.append({"turn": i + 1, "seconds": secs, "label": _fmt_duration(secs)})
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
