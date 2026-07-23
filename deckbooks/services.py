"""Read-side services: hydrate card records with printing metadata + images,
and DERIVE progress metrics from stored state (never hard-coded — Section 15.2).

Everything here is pure over the JSON records + the offline scryfall_cards
cache; no network, no writes.
"""

from __future__ import annotations

from typing import Any

from deckbooks import image_resolver, repository
from deckbooks.config import DECKBOOK_ID
from deckbooks.models import (
    CATEGORY_ORDER,
    CHAPTER_FLAVOR,
    CHAPTER_SEQUENCE,
    ROMAN,
    category_for_role,
    curation_complete,
    deck_copy_complete,
    has_museum_piece,
    is_proxy_candidate,
    is_upgrade_target,
)


def get_deckbook() -> dict:
    return repository.load_deckbook(DECKBOOK_ID)


def _all_cards() -> list[dict]:
    return repository.load_cards(DECKBOOK_ID)


def active_cards() -> list[dict]:
    """Cards physically in the deck (excludes removed + on-order)."""
    return [c for c in _all_cards() if c.get("status") == "active"]


def visible_cards() -> list[dict]:
    """Everything the book shows: deck cards + on-order research entries."""
    return [c for c in _all_cards() if c.get("status") != "removed"]


# ── Image hydration ─────────────────────────────────────────────────────────


def _printing_view(printing: dict | None) -> dict | None:
    """Turn a stored {scryfall_id, finish} into a render-ready view: the mirror
    image URL + onerror fallback + cached metadata. None stays None."""
    if not printing or not printing.get("scryfall_id"):
        return None
    sid = printing["scryfall_id"]
    meta = image_resolver.get_printing(sid)
    return {
        "scryfall_id": sid,
        "finish": printing.get("finish"),
        "image_url": image_resolver.mirror_image_url(sid),
        "image_fallback": image_resolver.scryfall_api_fallback(sid),
        "has_image": bool(meta and meta.image_url),
        "meta": meta,
    }


def _display_printing(card: dict) -> dict | None:
    """Which printing the gallery/detail leads with: the finalized selection if
    there is one, else the current physical printing."""
    decision = card.get("decision", {})
    if decision.get("finalized") and decision.get("selected_printing"):
        return decision["selected_printing"]
    return card.get("current_printing")


def hydrate(card: dict) -> dict:
    """A card record + everything a tile/detail needs to render."""
    return {
        **card,
        "category": category_for_role(card.get("role")),
        "curation_complete": curation_complete(card),
        "deck_copy_complete": deck_copy_complete(card),
        "is_upgrade": is_upgrade_target(card),
        "is_proxy": is_proxy_candidate(card),
        "has_museum": has_museum_piece(card),
        "display": _printing_view(_display_printing(card)),
        "current_view": _printing_view(card.get("current_printing")),
        "selected_view": _printing_view(card.get("decision", {}).get("selected_printing")),
        "museum_view": _printing_view(card.get("decision", {}).get("museum_printing")),
    }


def gallery() -> list[dict]:
    return [hydrate(c) for c in sorted(visible_cards(), key=_gallery_key)]


def _gallery_key(card: dict) -> tuple:
    # Commander first, then finalized cards, then by name — the book reads like
    # a curated volume, not raw import order.
    role_rank = 0 if card.get("role") == "Commander" else 1
    return (role_rank, 0 if curation_complete(card) else 1, card.get("card_name", "").lower())


def chapters() -> list[dict]:
    """The Collection as an ordered book of chapters (#8): the hydrated cards
    grouped by category, in reading order, each with a numeral + flavor line and
    its own completion tally. Empty categories are skipped; any unexpected
    category still appears (after the known sequence) rather than vanishing."""
    buckets: dict[str, list[dict]] = {}
    for c in gallery():  # already commander-first / finalized-first / name-sorted
        buckets.setdefault(c["category"], []).append(c)

    ordered = list(CHAPTER_SEQUENCE) + [k for k in buckets if k not in CHAPTER_SEQUENCE]
    out: list[dict] = []
    for cat in ordered:
        cards = buckets.get(cat)
        if not cards:
            continue
        out.append(
            {
                "name": cat,
                "numeral": ROMAN[len(out)] if len(out) < len(ROMAN) else str(len(out) + 1),
                "flavor": CHAPTER_FLAVOR.get(cat, ""),
                "cards": cards,
                "count": len(cards),
                "done": sum(1 for x in cards if x["curation_complete"]),
            }
        )
    return out


def museum_wall() -> dict:
    """The whole deck in its Museum (Collector's Pick) form — each card shown as
    its chosen museum printing, or its current copy (faded, 'awaiting a pick')
    where none is chosen yet. Commander first, then the cards that HAVE a pick,
    then the rest — so the finished exhibit leads."""
    cards = [hydrate(c) for c in visible_cards()]
    cards.sort(
        key=lambda c: (
            0 if c.get("role") == "Commander" else 1,
            0 if c.get("museum_view") else 1,
            c.get("card_name", "").lower(),
        )
    )
    return {"cards": cards, "with_pick": sum(1 for c in cards if c.get("museum_view"))}


def card_detail(deck_card_id: str) -> dict | None:
    card = next((c for c in _all_cards() if c.get("deck_card_id") == deck_card_id), None)
    if card is None:
        return None
    view = hydrate(card)
    # Candidate printings for the comparison browser — every cached printing of
    # this name, each with its image + whether it's the current/selected/museum.
    marks = {
        (card.get("current_printing") or {}).get("scryfall_id"): "current",
        (card["decision"].get("selected_printing") or {}).get("scryfall_id"): "selected",
        (card["decision"].get("museum_printing") or {}).get("scryfall_id"): "museum",
    }
    candidates = []
    for meta in image_resolver.list_printings_for_name(card["card_name"]):
        candidates.append(
            {
                "meta": meta,
                "image_url": image_resolver.mirror_image_url(meta.scryfall_id),
                "image_fallback": image_resolver.scryfall_api_fallback(meta.scryfall_id),
                "mark": marks.get(meta.scryfall_id),
            }
        )
    view["candidates"] = candidates
    view["revisions"] = [
        r for r in repository.load_revisions(DECKBOOK_ID) if r.get("deck_card_id") == deck_card_id
    ]
    return view


# ── Progress metrics (derived) ──────────────────────────────────────────────


def progress() -> dict[str, Any]:
    cards = _all_cards()
    deck = [c for c in cards if c.get("status") == "active"]
    shown = [c for c in cards if c.get("status") != "removed"]
    total = len(deck)
    curated = sum(1 for c in deck if curation_complete(c))
    copies = sum(1 for c in deck if deck_copy_complete(c))

    # Per-category rollup (the PDF's dashboard rows).
    cats: dict[str, dict[str, int]] = {k: {"done": 0, "total": 0} for k in CATEGORY_ORDER}
    for c in deck:
        cat = category_for_role(c.get("role"))
        bucket = cats.setdefault(cat, {"done": 0, "total": 0})
        bucket["total"] += 1
        if curation_complete(c):
            bucket["done"] += 1

    return {
        "total": total,
        "curated": curated,
        "curated_pct": round(100 * curated / total) if total else 0,
        "deck_copies_complete": copies,
        "installed": sum(1 for c in deck if c.get("acquisition", {}).get("installed")),
        "owned": sum(1 for c in deck if c.get("acquisition", {}).get("target_owned")),
        "decisions_remaining": sum(1 for c in shown if not curation_complete(c)),
        "upgrade_targets": sum(1 for c in shown if is_upgrade_target(c)),
        "proxy_candidates": sum(1 for c in shown if is_proxy_candidate(c)),
        "museum_pieces": sum(1 for c in shown if has_museum_piece(c)),
        "categories": [{"name": k, **cats[k]} for k in CATEGORY_ORDER if cats[k]["total"]],
    }


def research_queue() -> list[dict]:
    """The open decisions the curator should act on next — every card still in
    the `research` stage (in-deck or on order), hydrated for a thumbnail."""
    q = [c for c in visible_cards() if c.get("decision", {}).get("status") == "research"]
    q.sort(key=lambda c: c.get("card_name", "").lower())
    return [hydrate(c) for c in q]


def recently_finalized(limit: int = 6) -> list[dict]:
    """The most recently finalized cards, newest first, from the revision log —
    so the overview shows momentum, not just a percentage."""
    revs = [
        r
        for r in repository.load_revisions(DECKBOOK_ID)
        if r.get("change_type") == "decision_finalized"
    ]
    # Newest first; dedup to the latest finalize per card.
    revs.sort(key=lambda r: (r.get("changed_at", ""), r.get("revision", 0)), reverse=True)
    seen: set[str] = set()
    cards_by_id = {c["deck_card_id"]: c for c in _all_cards()}
    out: list[dict] = []
    for r in revs:
        cid = r.get("deck_card_id")
        if cid in seen or cid not in cards_by_id:
            continue
        seen.add(cid)
        out.append({**hydrate(cards_by_id[cid]), "finalized_on": r.get("changed_at")})
        if len(out) >= limit:
            break
    return out


def on_order() -> list[dict]:
    """Cards acquired but not yet installed (the acquisition-ledger feed)."""
    return [hydrate(c) for c in _all_cards() if c.get("status") == "on_order"]


def overview() -> dict:
    """Everything the dashboard needs: the stats PLUS the 'what next' feeds."""
    return {
        "progress": progress(),
        "research_queue": research_queue(),
        "recently_finalized": recently_finalized(),
        "on_order": on_order(),
    }
