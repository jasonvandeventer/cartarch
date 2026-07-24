"""LLM briefing export — a self-contained Markdown snapshot of one card's
decision plus every available printing, to paste/upload into ChatGPT (or any
model) for a printing recommendation.

Reasoning over structured data beats screen-scraping a live app, and it needs
no network exposure: the file carries the deck's aesthetic criteria, the current
decision, and each printing's set / rarity / finishes / price / treatment /
Scryfall link — everything a model needs to argue for a definitive printing.
"""

from __future__ import annotations

from deckbooks import image_resolver, repository
from deckbooks.context import get_book


def _price_summary(prices: dict) -> str:
    parts = []
    for label in ("normal", "foil", "etched"):
        v = prices.get(label)
        if v:
            parts.append(f"{label} ${v}")
    return ", ".join(parts) if parts else "no price"


def _printing_ref(p: dict | None) -> str:
    if not p or not p.get("scryfall_id"):
        return "none chosen"
    meta = image_resolver.get_printing(p["scryfall_id"])
    if meta is None:
        return f"{p['scryfall_id']} ({p.get('finish', 'normal')})"
    return f"{meta.set_code.upper()} #{meta.collector_number} ({p.get('finish', 'normal')})"


def card_briefing(deck_card_id: str) -> str | None:
    """Markdown briefing for one card, or None if the card isn't found."""
    cards = repository.load_cards(get_book())
    card = next((c for c in cards if c.get("deck_card_id") == deck_card_id), None)
    if card is None:
        return None
    db = repository.load_deckbook(get_book())
    ident = db.get("identity", {})
    decision = card.get("decision", {})

    lines: list[str] = []
    lines.append(f"# Printing briefing — {card['card_name']}")
    lines.append("")
    lines.append(f"**Deck:** {db.get('name')} ({', '.join(db.get('commander_names', []))})")
    if ident.get("mission"):
        lines.append(f"**Mission:** {ident['mission']}")
    if ident.get("pillars"):
        pillars = "; ".join(f"{p['name']} — {p['description']}" for p in ident["pillars"])
        lines.append(f"**Aesthetic pillars:** {pillars}")
    if ident.get("palette"):
        lines.append(f"**Palette:** {', '.join(ident['palette'])}")
    if ident.get("selection_rule"):
        lines.append(f"**Selection rule:** {ident['selection_rule']}")
    lines.append("")

    lines.append(f"## Current decision for {card['card_name']}")
    lines.append(f"- Role: {card.get('role')}")
    lines.append(
        f"- Status: {decision.get('status')} "
        f"(finalized: {'yes' if decision.get('finalized') else 'no'})"
    )
    lines.append(f"- Current printing: {_printing_ref(card.get('current_printing'))}")
    lines.append(f"- Destination printing: {_printing_ref(decision.get('selected_printing'))}")
    if decision.get("verdict"):
        lines.append(f"- Existing verdict: {decision['verdict']}")
    lines.append("")

    printings = image_resolver.list_printings_detailed(card["card_name"])
    lines.append(f"## Every official printing ({len(printings)})")
    lines.append("")
    lines.append("| Set | # | Rarity | Finishes | Prices (USD) | Treatment | Scryfall |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for p in printings:
        treatment = ", ".join(p["treatments"]) if p["treatments"] else "standard"
        lines.append(
            f"| {p['set_name'] or p['set_code'].upper()} "
            f"| {p['collector_number']} | {p['rarity'] or '—'} "
            f"| {', '.join(p['finishes'])} | {_price_summary(p['prices'])} "
            f"| {treatment} | {p['scryfall_url']} |"
        )
    lines.append("")

    lines.append(_RECOMMENDATION_POLICY)
    lines.append("")
    return "\n".join(lines)


# Deckbook Printing Recommendation Policy v3 — evaluate the card in two stages:
# document the CURRENT copy (a baseline, not a purchase rec) and choose the one
# aspirational DESTINATION upgrade that belongs IN the deck (not protected in a
# collection). Encoded here so every briefing asks the model the same way.
# See deckbooks/PRINTING_POLICY.md.
_RECOMMENDATION_POLICY = """\
## Your task — current printing and destination upgrade

Evaluate the card in two stages. Pick from the table above; name the exact set +
collector number and the finish.

### 1. Current printing
**"What version is in the deck today?"**

Retain the printing currently listed in the deck. Briefly explain how well it
serves the deck's theme, artwork, palette, readability, and overall cohesion.

This is not a recommendation to purchase a different practical printing. Its
purpose is to document the deck's present state and provide a baseline for the
eventual upgrade.

### 2. Destination printing
**"What version should eventually replace it in the finished deck?"**

Choose the single official printing that would make the completed deck feel most
thematic, beautiful, and deliberately curated.

Optimise for:

* Connection to the deck's mission and aesthetic pillars
* Artwork that feels native to the deck's world
* Cohesion with the deck's palette
* Strong visual presence, including borderless, full-art, showcase, or especially
  effective foil treatments
* Artistic execution and how the physical treatment supports the illustration
* Collectibility and prestige as secondary considerations

Do not simply choose the rarest, most expensive, or most historically important
printing. The goal is not to identify the best collectible version in isolation.
The goal is to identify the best **in-deck destination upgrade** for this specific
deck.

The Destination printing should be the version worth acquiring when gradually
blinging out the deck. It may be expensive or aspirational, but it should
ultimately belong in the deck rather than remain protected in a collection.

*Curator's note:* explain why this printing is the strongest final expression of
the card within this deck.

## Required output

**Current:** exact set, collector number, and finish
**Destination:** exact set, collector number, and finish"""
