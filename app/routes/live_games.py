"""Companion mode — live game routes (start / action / SSE stream).

The first mid-game server write surface. ``/live/start`` is an owner-only opt-in
that flips a game to ``in_progress``; ``/live/action`` is the seat/table-scoped
mutation API (JSON); ``/live/stream`` is the viewer-scoped SSE feed. Games in
``created`` status keep using the localStorage-only tracker unchanged.

Authorization for mutations lives in ``live_game_service.apply_live_action`` (the
table-token-or-seat model). Read access (the stream) is viewer-scoped and NOT
token-gated. See ``live_game_service`` for the full model.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app import live_game_events
from app.db import SessionLocal
from app.dependencies import CsrfRequired, get_current_user, get_db_session
from app.live_game_service import apply_live_action, get_live_state, start_live_game, state_payload
from app.models import User

router = APIRouter()

_SSE_HEARTBEAT_SECONDS = 25  # ": keepalive" cadence to hold the conn through cloudflared


def _check_csrf_json(request: Request, body: dict) -> None:
    """CSRF for the JSON action endpoint. The Form-based ``require_csrf_token``
    can't read a JSON body, so accept the double-submit token from the
    ``X-CSRF-Token`` header or a ``csrf_token`` body field, matched against the
    session token (identical strictness to ``require_csrf_token``)."""
    expected = request.session.get("csrf_token", "")
    token = request.headers.get("X-CSRF-Token") or (
        body.get("csrf_token") if isinstance(body, dict) else ""
    )
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


@router.post("/games/{game_id}/live/start")
def live_start(
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Owner-only: start live/companion mode for a ``created`` game, then land on
    the game detail page (Session 2 renders live mode there; the page already
    receives ``client_token`` as its table token)."""
    try:
        start_live_game(session, game_id, current_user.id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return RedirectResponse(url=f"/games/{game_id}", status_code=303)


@router.post("/games/{game_id}/live/action")
async def live_action(
    request: Request,
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Apply one live action. JSON body = the action dict, plus an optional
    ``table_token`` field (an ``X-Table-Token`` header is also accepted; the body
    field wins). Returns ``{"version": N, "state": {...}}``. 403 on auth failure,
    400 on invalid action, 404 if the game has no live state."""
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Body must be JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    _check_csrf_json(request, body)

    table_token = body.get("table_token") or request.headers.get("X-Table-Token")
    # Run the sync service off the event loop so it never stalls open SSE streams;
    # publish() is thread-safe (schedules onto the captured loop).
    try:
        live = await run_in_threadpool(
            apply_live_action, session, game_id, current_user.id, body, table_token
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JSONResponse(state_payload(live))


def _sse_event(payload_str: str) -> str:
    """Format one SSE event: ``id: <version>`` + full-state ``data:`` line."""
    try:
        version = json.loads(payload_str).get("version")
    except Exception:
        version = None
    id_line = f"id: {version}\n" if version is not None else ""
    return f"{id_line}data: {payload_str}\n\n"


@router.get("/games/{game_id}/live/stream")
async def live_stream(request: Request, game_id: int):
    """Viewer-scoped SSE feed (owner / seat player / playgroup member — NOT
    token-gated). Sends the full current state immediately, then every published
    state. Full state (not deltas) so a reconnecting client self-heals.

    Reads the user from the session and uses a SHORT-LIVED DB session for the
    auth + initial snapshot, then closes it — an SSE connection must not pin a
    pooled DB connection for its whole (possibly long) lifetime."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = SessionLocal()
    try:
        try:
            live = get_live_state(session, game_id, user_id)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        initial_payload = json.dumps(state_payload(live))
    finally:
        session.close()

    async def event_gen():
        yield _sse_event(initial_payload)
        async with live_game_events.subscribe(game_id) as queue:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECONDS)
                    yield _sse_event(payload)
                except TimeoutError:
                    yield ": keepalive\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
