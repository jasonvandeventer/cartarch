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
from deckbooks.config import DECKBOOK_ID


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
    cards = repository.load_cards(DECKBOOK_ID)
    card = next((c for c in cards if c.get("deck_card_id") == deck_card_id), None)
    if card is None:
        return None
    db = repository.load_deckbook(DECKBOOK_ID)
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
    lines.append(f"- Copy in the deck today: {_printing_ref(card.get('current_printing'))}")
    lines.append(f"- Definitive (selected): {_printing_ref(decision.get('selected_printing'))}")
    lines.append(f"- Museum piece: {_printing_ref(decision.get('museum_printing'))}")
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


# Deckbook Printing Recommendation Policy v2 — the two recommendations optimize
# for DIFFERENT goals and are deliberately NOT reconciled. Encoded here so every
# briefing asks the model the same way. See deckbooks/PRINTING_POLICY.md.
_RECOMMENDATION_POLICY = """\
## Your task — two independent recommendations

Recommend **two** printings for this card. They optimise for **different goals**
and must **not** be reconciled into one — sometimes they match, often they differ.
Pick from the table above; name the exact set + collector number and the finish.

### 1. Definitive printing
*"What version should a player reasonably acquire for THIS deck?"*
Optimise for: deck theme / narrative, artwork appropriateness, cohesion with the
other cards, readability, availability, and cost / value — the best balance of
aesthetics and practicality. A player should be able to assemble the **whole deck**
from Definitive picks without unreasonable expense. Do **not** default to the
rarest, newest, or most expensive printing.
*Curator's note:* explain why this printing belongs in THIS deck.

### 2. Museum (Collector's Pick)
*"If budget and practicality were irrelevant, what is the most beautiful or
collectible expression of this card?"*
Optimise for: visual impact, premium treatment, collectibility, artistic
execution, and prestige. **Ignore deck theme.** It may be a Secret Lair, a
Universes Beyond card, a serialised version, a Masterpiece, a raised foil, or an
expensive promo — whatever is the most desirable presentation of the card. This is
an aspirational target that may be **proxied** in the deck while the original stays
protected in a collection.
*Curator's note:* explain why it is the pinnacle of this card as an object of
collection."""
