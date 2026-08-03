"""#179 — read-only JSON API for non-browser clients (a Discord bot, a script).

**This module adds authentication, not serialization.** Three of its four routes
delegate straight into the existing ``?format=json`` export handlers, which
already emit per-card gameplay metadata via ``pricing.card_metadata`` plus
inventory context and a server-computed deck rollup — all from persisted
columns, no Scryfall call on the request path. Building a parallel set of JSON
views would have been rebuilding the half that already works.

The one thing missing was a way for a client with no session cookie to say who
it is, which is what ``users.api_token`` + :func:`require_api_user` are.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.deck_service import list_decks_basic
from app.dependencies import get_db_session
from app.models import InventoryRow, User
from app.routes.collections import CollectionFilter, collection_export, collection_filter
from app.routes.decks import decks_export

router = APIRouter(prefix="/api/v1", tags=["api"])


def require_api_user(
    request: Request,
    session: Session = Depends(get_db_session),
) -> User:
    """Resolve ``Authorization: Bearer <users.api_token>`` to a User, or 401.

    **Bearer only — it deliberately does NOT also accept the session cookie.**
    One way in means no ambient-credential path, which (with every route here
    being GET) is also why this surface needs no CSRF consideration.

    **It raises 401 JSON and never a redirect, which is the whole reason it is
    not ``get_current_user``.** That dependency raises a **303 to /login** when
    there is no session — the ``?next=`` deep-link behaviour, correct for a
    browser and useless to a bot, which would receive a redirect and an HTML
    login page in answer to a bad token and be told nothing.

    ``Bearer`` is matched case-insensitively (RFC 7235: the auth-scheme token is
    case-insensitive) while the credential stays exact — the same rule
    ``require_metrics_token`` follows. That gate is otherwise the WRONG
    precedent here: it compares against a single process-wide ``METRICS_TOKEN``
    env var, whereas this must resolve *which user* is asking, since every route
    below is owner-scoped.

    **An empty credential must never authenticate.** ``api_token`` is NULL for
    every user who has not enabled the API, and while ``NULL = ''`` is NULL (not
    true) in SQL, the guard is explicit rather than relying on that: a missing
    header must fail on its own terms, not on a dialect's null semantics.

    ponytail: plaintext token compared by an indexed lookup, not
    ``hmac.compare_digest`` — the value is a 256-bit ``secrets.token_urlsafe``
    and the scope is read-only over card data. Hash it (and show it once at
    generation) if the API ever gains writes.
    """
    auth = request.headers.get("Authorization", "")
    scheme, _, supplied = auth.partition(" ")
    if scheme.lower() != "bearer":
        supplied = ""

    user = None
    if supplied:
        user = session.query(User).filter(User.api_token == supplied).first()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def _no_store(payload) -> Response:
    """Wrap a payload as JSON with ``Cache-Control: no-store``.

    ``render()`` sets ``no-store`` on every HTML page for exactly this reason —
    the response is per-user and must never be held by a browser or an
    intermediary. Nothing on this router goes through ``render()``, so without
    this the API would be the one per-user surface shipping no cache directive
    at all, sitting behind a CDN. A shared cache keying on URL alone would be
    free to hand one user's collection to the next caller.

    Applied to EVERY route here, including the two that delegate: FastAPI
    returns a handler-built ``JSONResponse`` verbatim, so an injected
    ``Response`` parameter would not reach those and the header has to be set on
    the object itself.
    """
    resp = payload if isinstance(payload, Response) else JSONResponse(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/me")
def api_me(user: User = Depends(require_api_user)) -> Response:
    """Identity echo, so a client can confirm a token resolves and name its owner."""
    return _no_store(
        {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
        }
    )


@router.get("/collection")
def api_collection(
    filters: CollectionFilter = Depends(collection_filter),
    session: Session = Depends(get_db_session),
    user: User = Depends(require_api_user),
):
    """The caller's collection as JSON — the existing export handler, bearer-authed.

    Taking ``collection_filter`` as a dependency (rather than synthesizing a
    default ``CollectionFilter``) hands the client the whole existing filter
    surface for free: ``?search=`` runs the app's boolean/Scryfall query
    grammar, and the colour/type/status/price facets compose with it. A bot
    answering "do I own a Rhystic Study?" fetches one card, not the collection.

    ponytail: unpaginated. Measured 2026-08-03 the largest prod collection
    serializes to ~4.9 MB, of which ``legalities`` alone is 2.6 MB (54%) —
    ``card_metadata`` is shaped for an LLM consumer. That is the existing cost
    of ``/collection/export?format=json``, not a new one, so it is not worth a
    second serializer today. Add paging or a ``?fields=`` trim when a client
    actually polls this.
    """
    try:
        return _no_store(
            collection_export(filters=filters, format="json", session=session, current_user=user)
        )
    except ValueError as exc:
        # A malformed search term raises ValueError, which the app-wide handler
        # renders as HTML — fine for a page, useless to a JSON client.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/decks")
def api_decks(
    session: Session = Depends(get_db_session),
    user: User = Depends(require_api_user),
) -> Response:
    """The caller's decks: id / name / format / card_count.

    ``list_decks_basic``, NOT ``list_decks`` — the latter runs ~3 queries plus
    combo/bracket/consistency work *per deck* (including CommanderSpellbook
    lookups) to feed the /decks table, none of which a client listing deck names
    needs. Card counts come from ONE batched group-by, so this is two queries
    total regardless of deck count and makes no external call.

    Retired decks (#163) are excluded — ``list_decks_basic`` filters
    ``retired_at IS NULL``, exactly as every other deck surface does.

    ponytail: ``card_count`` covers the deck's OWN rows only, so for a deck in a
    variant group it reads lower than the card list ``/api/v1/decks/{id}``
    returns (that one folds in inbound ``DeckCardShare`` rows, as the deck page
    does). Folding them in here costs a query per deck; do it if variant groups
    stop being rare.
    """
    decks = list_decks_basic(session, user.id)
    loc_ids = [d.storage_location_id for d in decks if d.storage_location_id]
    counts: dict[int, int] = {}
    if loc_ids:
        counts = dict(
            session.query(
                InventoryRow.storage_location_id,
                func.coalesce(func.sum(InventoryRow.quantity), 0),
            )
            .filter(InventoryRow.storage_location_id.in_(loc_ids))
            .group_by(InventoryRow.storage_location_id)
            .all()
        )
    return _no_store(
        {
            "decks": [
                {
                    "id": d.id,
                    "name": d.name,
                    "format": d.format,
                    "card_count": counts.get(d.storage_location_id, 0),
                }
                for d in decks
            ]
        }
    )


@router.get("/decks/{deck_id}")
def api_deck(
    deck_id: int,
    session: Session = Depends(get_db_session),
    user: User = Depends(require_api_user),
):
    """One deck's full card list + rollup — the existing export handler, bearer-authed.

    Owner scoping is ``decks_export``'s own: it resolves the deck through
    ``get_deck(session, deck_id, user_id)`` and 404s on a miss, so another
    user's deck id is indistinguishable from a nonexistent one.
    """
    return _no_store(
        decks_export(deck_id=deck_id, format="json", session=session, current_user=user)
    )
