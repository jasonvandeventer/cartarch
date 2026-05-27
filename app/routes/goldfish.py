"""Goldfish playtester route (v3.30.0).

Single-player, session-only deck-testing surface. **New page**, not a
game-tracker extension — the tracker has no card model, no zone model,
no hand. This route serves a thin HTML shell that injects a deck's
card payload as JSON; the client-side state machine (``goldfish.js``)
owns every zone transition, draw, mulligan, tap, and rendering.

**Read-only against InventoryRow** — InventoryRow stays the single
source of truth; the goldfish surface never writes to it. The page
queries the deck's StorageLocation (decks-as-storage-locations
architecture from v3.3), expands quantities into per-card payload
entries with stable instance ids, and flags commanders via
``InventoryRow.role == 'commander'`` so the client seeds the Command
Zone correctly.

**Zero request-path network calls** — ``Card.image_url`` is read from
the v3.25.0 local bulk cache; the browser fetches the actual image at
render time, not the server during request handling. Same precedent
v3.26.1 commander-art rendering established.

**No POST routes** in iteration 1 — zone state is session-only on the
client (deliberate; sidesteps the v4 persistence decision). No CSRF
needed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.dependencies import get_current_user, get_db_session, render
from app.models import Card, Deck, DeckTokenRequirement, InventoryRow, TokenInventory, User

router = APIRouter()


@router.get("/decks/{deck_id}/goldfish")
def goldfish_page(
    deck_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    deck = session.query(Deck).filter(Deck.id == deck_id, Deck.user_id == current_user.id).first()
    if deck is None:
        # Non-leakage discipline: bounce to the decks index rather than 404
        # leaking the existence of a deck the user does not own.
        return RedirectResponse(url="/decks", status_code=303)

    if deck.storage_location_id is None:
        # Misconfigured deck (no backing storage location); render an empty
        # surface rather than crashing the page.
        payload = {
            "deck_id": deck.id,
            "deck_name": deck.name,
            "format": deck.format,
            "cards": [],
            # v3.30.8 — tokens field present on every payload (shape
            # consistency); empty on the misconfigured-deck branch.
            "tokens": [],
        }
        return render(
            request,
            "goldfish.html",
            {
                "title": f"Goldfish · {deck.name}",
                "current_user": current_user,
                "deck": deck,
                "deck_payload": payload,
            },
        )

    rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .filter(
            InventoryRow.storage_location_id == deck.storage_location_id,
            InventoryRow.user_id == current_user.id,
        )
        .all()
    )

    cards: list[dict] = []
    for row in rows:
        card: Card | None = row.card
        if card is None:
            continue
        cards.append(
            {
                "inventory_row_id": row.id,
                "card_id": card.id,
                "name": card.name,
                "set_code": card.set_code,
                "collector_number": card.collector_number,
                "image_url": card.image_url,
                "mana_cost": card.mana_cost,
                "cmc": float(card.cmc) if card.cmc is not None else None,
                "type_line": card.type_line,
                "oracle_text": card.oracle_text,
                "colors": card.colors,
                "color_identity": card.color_identity,
                "quantity": int(row.quantity or 0),
                "is_commander": (row.role == "commander"),
            }
        )

    # v3.30.8 — deck-token requirements as quick-add seeds. Reads
    # the user-curated DeckTokenRequirement rows for this deck and
    # joinedloads the linked TokenInventory (when present) so the
    # client can render the token with art + type_line. Loose name-
    # only requirements (no token_inventory_id link) degrade to a
    # name + quantity entry; the client falls through to its
    # renderFallback text card-face when image_url is null. Local
    # SQLite read only — the request-path network invariant from
    # v3.25.0 stands. A deck with zero requirements emits
    # tokens: []; the client suppresses the quick-add panel
    # entirely so no empty box renders. Defensive try/except so
    # any unexpected ORM error degrades to an empty list rather
    # than a 500 — matches the spec's "must not crash the route"
    # discipline.
    tokens: list[dict] = []
    try:
        reqs = (
            session.query(DeckTokenRequirement)
            .options(joinedload(DeckTokenRequirement.token_inventory))
            .filter(DeckTokenRequirement.deck_id == deck.id)
            .order_by(DeckTokenRequirement.token_name.asc())
            .all()
        )
        for req in reqs:
            ti: TokenInventory | None = req.token_inventory
            tokens.append(
                {
                    "requirement_id": req.id,
                    "token_name": req.token_name,
                    "quantity_needed": int(req.quantity_needed or 1),
                    # Joined-row fields when the requirement links a real
                    # TokenInventory row; null when loose name-only.
                    "type_line": (ti.type_line if ti else None),
                    "image_url": (ti.image_url if ti else None),
                    "scryfall_id": (ti.scryfall_id if ti else None),
                    "set_code": (ti.set_code if ti else None),
                    "collector_number": (ti.collector_number if ti else None),
                }
            )
    except Exception:
        # Belt-and-suspenders against schema drift / missing-table
        # / ORM-relationship-misconfigured paths. Degrade silently;
        # the playtester is still fully usable without quick-add
        # buttons — the v3.30.7 manual Create-token control still
        # works.
        tokens = []

    payload = {
        "deck_id": deck.id,
        "deck_name": deck.name,
        "format": deck.format,
        "cards": cards,
        "tokens": tokens,
    }

    return render(
        request,
        "goldfish.html",
        {
            "title": f"Goldfish · {deck.name}",
            "current_user": current_user,
            "deck": deck,
            "deck_payload": payload,
        },
    )
