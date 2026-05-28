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

from app.deck_service import get_deck_produced_tokens_for_goldfish
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

    # v3.30.21 — primary path: auto-detect produced tokens from the
    # scryfall_cards.produced_tokens column (v3.30.11 daemon-populated,
    # v3.30.19 consumer-flipped to local reads). For decks whose cards
    # have been backfilled, this surfaces every produced token in the
    # quick-add panel without requiring DeckTokenRequirement curation.
    # The common case on prod: produced_tokens is fully populated
    # (114,242/114,242 rows at v3.30.19 ship time), goldfish quick-add
    # works automatically for every deck. Pure local SQLite read; zero
    # external calls. Defensive try/except so any unexpected ORM error
    # degrades to an empty list rather than 500 — same posture the
    # DeckTokenRequirement-fallback block below uses.
    try:
        tokens_produced = get_deck_produced_tokens_for_goldfish(deck.id, session)
    except Exception:
        tokens_produced = []

    # v3.30.8 fallback — manually-curated DeckTokenRequirement rows.
    # Retained for edge cases the auto-detect path doesn't cover:
    # non-standard tokens, manual overrides, scryfall_cards rows the
    # daemon hasn't backfilled yet (fresh-install case; on prod after
    # the v3.30.19 backfill this is rare). Loose name-only requirements
    # (no token_inventory_id link) still degrade to name + quantity;
    # the client's renderFallback handles missing image_url. Same
    # defensive try/except as before — schema drift or missing-table
    # paths degrade silently, the playtester stays usable.
    tokens_manual: list[dict] = []
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
            tokens_manual.append(
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
        tokens_manual = []

    # v3.30.21 — combine: produced first; append manual entries whose
    # name isn't already covered by the produced set. Belt-and-suspenders
    # — the common case on prod is `tokens_manual` is empty and
    # `tokens_produced` has everything the playtester needs. Edge case
    # (deck has manual requirements for tokens the daemon hasn't or
    # can't detect — non-standard tokens, the rare uncached row): those
    # manual rows still surface so the user keeps the curation they put
    # in. Token-name match is case-insensitive; produced wins on collision.
    tokens: list[dict] = list(tokens_produced)
    if tokens_manual:
        produced_names = {(t.get("token_name") or "").strip().lower() for t in tokens_produced}
        for m in tokens_manual:
            name_key = (m.get("token_name") or "").strip().lower()
            if name_key and name_key not in produced_names:
                tokens.append(m)

    # v3.30.10 — panels-cache enrichment for token shape. The v3.30.8
    # payload above sourced type_line/image_url ONLY from a joined
    # TokenInventory row; v3.30.9's auto-tracked DeckTokenRequirement
    # rows are loose (token_inventory_id NULL) so the join produced
    # null fields and goldfish.js defaulted them to "Token Creature" —
    # which classifyRegion routed to bf-creatures, regardless of what
    # the token actually was. Food/Treasure/Clue/etc landed in the
    # Creatures region. The fix consumes the per-deck panels-cache
    # (written by fetch_deck_tokens, the same data the deck-detail
    # "Tokens" panel reads to render correct art + types). For each
    # token whose type_line or image_url is still null, look up by
    # case-insensitive name in the cache and fill in. Best-effort
    # against the LAST-WRITTEN cache (NOT a live read; if the deck
    # changed since the cache was written, the token data may be stale
    # — acceptable for a playtester, documented here so a future
    # reader doesn't mistake this for a live data path). Precedence:
    # (a) TokenInventory link (user-curated, authoritative) → (b)
    # panels-cache by name → (c) None (goldfish.js falls back to a
    # neutral "Token" string that classifyRegion routes to bf-other,
    # NOT bf-creatures — see goldfish.js quickAddDetectedToken). Reads
    # _panels_cache_key + _read_panels_cache lazily from app.main to
    # avoid a circular import (app.main imports this router). Zero
    # new request-path network calls — the cache is a local JSON
    # file. Token-name collisions (e.g. "Spirit", "Soldier") resolve
    # to first-wins deterministically.
    if tokens:
        try:
            from app.main import _panels_cache_key, _read_panels_cache

            ck = _panels_cache_key(rows)
            cached = _read_panels_cache(deck.id, ck)
            cache_lookup: dict[str, dict] = {}
            if cached:
                for t in cached.get("tokens") or []:
                    name_key = (t.get("name") or "").strip().lower()
                    if not name_key:
                        continue
                    # First-wins on collisions — deterministic single match
                    # per spec.
                    cache_lookup.setdefault(name_key, t)
            if cache_lookup:
                for tok in tokens:
                    key = (tok.get("token_name") or "").strip().lower()
                    hit = cache_lookup.get(key)
                    if not hit:
                        continue
                    if tok.get("type_line") is None and hit.get("type_line"):
                        tok["type_line"] = hit["type_line"]
                    if tok.get("image_url") is None and hit.get("image_url"):
                        tok["image_url"] = hit["image_url"]
                    if tok.get("scryfall_id") is None and hit.get("scryfall_id"):
                        tok["scryfall_id"] = hit["scryfall_id"]
                    if tok.get("set_code") is None and hit.get("set_code"):
                        tok["set_code"] = hit["set_code"]
                    if tok.get("collector_number") is None and hit.get("collector_number"):
                        tok["collector_number"] = hit["collector_number"]
        except Exception:
            # Defensive: cache-read errors must not break payload assembly.
            # User keeps quick-add buttons (now with raw v3.30.8 shape);
            # goldfish.js handles missing type_line via the neutral fallback.
            pass

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
