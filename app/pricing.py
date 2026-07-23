"""Pricing helpers.

Prices are stored as strings because they arrive from Scryfall as string-like
JSON values. The rest of the app should consume normalized floats.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.models import Card


def card_metadata(card: Card) -> dict:
    """LLM-parseable gameplay metadata for a Card, from PERSISTED columns only.

    No network call (same request-path posture as ``effective_price``). Colors
    and color_identity are emitted as arrays — ``[]`` for colorless, never ``""``
    (a model misreads the empty string as missing). Legalities is the stored
    JSON re-parsed into a nested object, not a string. Consumed by the JSON
    export variant on both the collection and deck export routes.
    """
    try:
        legalities = json.loads(card.legalities) if card.legalities else {}
    except (TypeError, ValueError):
        legalities = {}
    try:
        keywords = json.loads(card.keywords) if card.keywords else []
    except (TypeError, ValueError):
        keywords = []
    return {
        "name": card.name or "",
        "set_code": (card.set_code or "").upper(),
        "set_name": card.set_name or "",
        "collector_number": card.collector_number or "",
        "rarity": card.rarity or "",
        "mana_cost": card.mana_cost or "",
        "mana_value": card.cmc if card.cmc is not None else None,
        "colors": (card.colors or "").split(),
        "color_identity": (card.color_identity or "").split(),
        "type_line": card.type_line or "",
        "oracle_text": card.oracle_text or "",
        # #76 — P/T are raw strings ("*"/"X" possible), None on non-creatures
        # (a model misreads "" as an actual value); keywords is a parsed
        # array like legalities, [] when none/unpopulated.
        "power": card.power or None,
        "toughness": card.toughness or None,
        "keywords": keywords,
        "legalities": legalities,
        "scryfall_id": card.scryfall_id or "",
    }


# Provider priority for the displayed USD price (MTGJSON ingest issue).
# cardmarket is excluded — it is EUR, and mixing it into a USD-displayed price
# corrupts valuation. A manual override always wins over every provider. This
# is the ONE resolution function; the ingest uses it to denormalize the result
# onto Card.price_usd*, so there is no second copy of the chain to drift.
PRICE_PROVIDER_ORDER = ("tcgplayer", "cardkingdom", "cardsphere")


def resolve_price_value(price) -> str | None:
    """Resolved display price for a CardPrice row.

    Manual override first, then tcgplayer/cardkingdom/cardsphere retail in
    priority order, first non-null. ``None`` (no provider value, no override)
    → the UI renders "no price". Never falls back to Scryfall.
    """
    if price is None:
        return None
    if price.manual_override:
        return price.manual_override
    for value in (price.tcgplayer_retail, price.cardkingdom_retail, price.cardsphere_retail):
        if value:
            return value
    return None


def parse_price(value: str | None) -> float:
    """Parse a nullable price string into a safe float."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ── scryfall_cards bulk-cache price fallback ────────────────────────────────
# The `cards` table (and therefore `effective_price` / the MTGJSON `card_prices`
# ingest) only covers cards a user actually TOUCHED — owned, imported, or
# decked. `scryfall_cards` is the daily Scryfall bulk cache and holds a price
# for EVERY printing, which is the whole point of that cache: it lets a surface
# show a price for a card nobody owns yet (a wishlist hunt is the motivating
# case). These two helpers are the batched, request-path-safe bridge (one IN
# query, no per-row network — the request-path network invariant); MTGJSON stays
# authoritative WHERE it has a value, and this only fills the gap it leaves.
#
# The per-printing "cheapest finish" and per-name min are folded in PYTHON, not
# SQL: SQLite has no `least()`/`greatest()`, and emulating it engine-agnostically
# is uglier than the fold. The row set is bounded (wishlist names × printings),
# so this is the same shape as list_watchlist's existing price fold.


def _least_price(*raws: str | None) -> float | None:
    """Lowest parseable price across the given finish strings; None if all are
    NULL/empty/unparseable ('' and None both mean "no price for this finish")."""
    vals = [parse_price(r) for r in raws if r]
    vals = [v for v in vals if v > 0]
    return min(vals) if vals else None


def bulk_cache_min_price_by_name(session: Session, names: Iterable[str]) -> dict[str, float]:
    """Cheapest printing (any finish) per card NAME, from the bulk cache.

    One indexed ``name IN (...)`` query over every printing of the requested
    names (never a full-table scan of the ~100k-row cache), folded to a per-name
    min in Python; a name with no priced printing is simply absent (caller
    renders "no price"). Keyed by the EXACT name the caller passed — the watch's
    stored ``card_name`` is the Scryfall-canonical form, the same exact-case the
    cache stores, so the ``ix_scryfall_cards_name`` index applies. Used for
    printing-agnostic ("any printing") wishlist watches, which have no Card row.
    """
    from app.legacy_tables import scryfall_cards as sc

    wanted = {n for n in names if n and n.strip()}
    if not wanted:
        return {}
    rows = session.execute(
        sc.select()
        .with_only_columns(sc.c.name, sc.c.price_usd, sc.c.price_usd_foil, sc.c.price_usd_etched)
        .where(sc.c.name.in_(wanted))
    ).all()
    out: dict[str, float] = {}
    for r in rows:
        p = _least_price(r.price_usd, r.price_usd_foil, r.price_usd_etched)
        if p is None:
            continue
        cur = out.get(r.name)
        if cur is None or p < cur:
            out[r.name] = p
    return out


def bulk_cache_min_price_by_id(session: Session, scryfall_ids: Iterable[str]) -> dict[str, float]:
    """Cheapest finish for each exact printing (by scryfall_id), from the cache.

    The by-id counterpart of the name helper: used for a printing-SPECIFIC
    wishlist watch whose Card row exists but carries no MTGJSON price yet, so it
    still shows the daily-cached number for that exact printing.
    """
    from app.legacy_tables import scryfall_cards as sc

    wanted = {i for i in scryfall_ids if i}
    if not wanted:
        return {}
    rows = session.execute(
        sc.select()
        .with_only_columns(
            sc.c.scryfall_id, sc.c.price_usd, sc.c.price_usd_foil, sc.c.price_usd_etched
        )
        .where(sc.c.scryfall_id.in_(wanted))
    ).all()
    out: dict[str, float] = {}
    for r in rows:
        p = _least_price(r.price_usd, r.price_usd_foil, r.price_usd_etched)
        if p is not None:
            out[r.scryfall_id] = p
    return out


def effective_price(card: Card, finish: str) -> float:
    """Return the best price for a card finish with sensible fallbacks."""
    finish = (finish or "normal").strip().lower()

    if finish == "foil":
        return parse_price(card.price_usd_foil) or parse_price(card.price_usd)

    if finish == "etched":
        return (
            parse_price(card.price_usd_etched)
            or parse_price(card.price_usd_foil)
            or parse_price(card.price_usd)
        )

    return parse_price(card.price_usd)
