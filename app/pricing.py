"""Pricing helpers.

Prices are stored as strings because they arrive from Scryfall as string-like
JSON values. The rest of the app should consume normalized floats.
"""

from __future__ import annotations

import json

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
