from __future__ import annotations

from sqlalchemy.orm import Session

from app.inventory_service import get_owned_cards_by_set, list_owned_sets
from app.models import Card, TokenInventory
from app.scryfall import fetch_set_cards_from_cache


def _get_owned_token_map(session: Session, set_code: str, user_id: int) -> dict[str, int]:
    """Map collector_number -> total owned quantity for tokens in a set.

    Sources from the token_inventory table (NOT inventory_rows). Set codes
    are matched case-insensitively. Counts BOTH front (set_code +
    collector_number) AND back (back_set_code + back_collector_number) so
    physical DFC tokens like TBLB#3 // TBLC#14 contribute to ownership of
    both sets even though only one face is the "front" in our schema.
    Multiple inventory rows for the same printing sum.
    """
    out: dict[str, int] = {}

    # Fronts
    front_rows = (
        session.query(TokenInventory.collector_number, TokenInventory.quantity)
        .filter(
            TokenInventory.user_id == user_id,
            TokenInventory.set_code.ilike(set_code),
        )
        .all()
    )
    for cn, qty in front_rows:
        if not cn:
            continue
        key = cn.lstrip("0") or "0"
        out[key] = out.get(key, 0) + int(qty or 0)

    # Backs
    back_rows = (
        session.query(TokenInventory.back_collector_number, TokenInventory.quantity)
        .filter(
            TokenInventory.user_id == user_id,
            TokenInventory.is_double_sided.is_(True),
            TokenInventory.back_set_code.ilike(set_code),
        )
        .all()
    )
    for cn, qty in back_rows:
        if not cn:
            continue
        key = cn.lstrip("0") or "0"
        out[key] = out.get(key, 0) + int(qty or 0)

    return out


def get_set_completion(
    session: Session,
    set_code: str,
    user_id: int,
    view: str = "all",
    include_tokens: bool = False,
) -> dict:
    """Build set-completion data for one user's collection.

    Scryfall set/card metadata is global, but ownership counts must always be
    scoped to the current user.
    """
    set_code = (set_code or "").strip().lower()
    view = view if view in {"all", "owned", "missing"} else "all"

    # v3.27.13 — read from the v3.25.0 scryfall_cards bulk cache instead
    # of paginating Scryfall live. Closes the 18-27s request-path latency
    # on /sets/{set_code} (one rate-limited Scryfall pagination per visit,
    # plus another for the token set, plus a third for substitutes — all
    # on the request path, every visit). The bulk cache is kept current
    # by the v3.25.0 _bulk_data_loop daemon and indexed on set_code +
    # collector_number, so this becomes a single SQLite SELECT. Cache
    # miss returns [] (no fallback to a live request-path Scryfall fetch
    # — the request-path network invariant requires misses fail visibly,
    # not degrade into per-row live fetches).
    set_cards = fetch_set_cards_from_cache(set_code)
    owned_map = get_owned_cards_by_set(session, set_code=set_code, user_id=user_id)

    # v3.27.15 — attach local Card.id per card so set_detail.html can wrap
    # the card name/image in /cards/{card_id} links. scryfall_cards rows
    # carry scryfall_id but NOT the local cards.id; one batched SELECT
    # against the cards table by scryfall_id resolves the mapping. Cards
    # never imported / never tokenized are absent from the cards table —
    # they get card_id=None and render without a link (graceful fallback).
    set_scryfall_ids = [c["scryfall_id"] for c in set_cards if c.get("scryfall_id")]
    card_id_map: dict[str, int] = {}
    if set_scryfall_ids:
        for row in (
            session.query(Card.scryfall_id, Card.id)
            .filter(Card.scryfall_id.in_(set_scryfall_ids))
            .all()
        ):
            card_id_map[row[0]] = row[1]

    owned_cards_list = []
    missing_cards = []

    for card in set_cards:
        collector_number = card["collector_number"]
        quantity_owned = owned_map.get(collector_number, 0)
        card["quantity_owned"] = quantity_owned
        card["card_id"] = card_id_map.get(card.get("scryfall_id"))

        if quantity_owned > 0:
            owned_cards_list.append(card)
        else:
            missing_cards.append(card)

    total_cards = len(set_cards)
    owned_cards = len(owned_cards_list)
    completion_pct = round((owned_cards / total_cards) * 100, 2) if total_cards else 0

    rarity_breakdown = _compute_rarity_breakdown(set_cards)

    visible_cards = set_cards
    if view == "owned":
        visible_cards = owned_cards_list
    elif view == "missing":
        visible_cards = missing_cards

    token_data = None
    if include_tokens:
        is_already_token_set = set_code.startswith("t")
        token_set_code = "t" + set_code if not is_already_token_set else set_code
        # v3.27.13 — token set ALSO reads from scryfall_cards. The
        # pre-fix path made a separate Scryfall pagination call for
        # t{set_code} on every visit; with the bulk cache this is one
        # more SELECT.
        token_cards = list(fetch_set_cards_from_cache(token_set_code))
        token_owned_map = _get_owned_token_map(session, token_set_code, user_id)
        for token in token_cards:
            cn_key = (token["collector_number"] or "").lstrip("0") or "0"
            token["quantity_owned"] = token_owned_map.get(cn_key, 0)
            token["is_substitute"] = False

        # Append substitute cards (s{code}) at the end of the token list. Not
        # every set has substitutes; fetch_set_cards_from_cache returns []
        # for unknown sets so this is safe even when the substitute set
        # doesn't exist. v3.27.13 — substitute set ALSO reads from the
        # bulk cache (was the third request-path Scryfall pagination call).
        substitute_set_code = "s" + set_code if not is_already_token_set else None
        substitute_cards: list = []
        if substitute_set_code:
            substitute_cards = list(fetch_set_cards_from_cache(substitute_set_code))
            if substitute_cards:
                sub_owned_map = _get_owned_token_map(session, substitute_set_code, user_id)
                for sub in substitute_cards:
                    cn_key = (sub["collector_number"] or "").lstrip("0") or "0"
                    sub["quantity_owned"] = sub_owned_map.get(cn_key, 0)
                    sub["is_substitute"] = True

        all_cards = token_cards + substitute_cards
        if all_cards:
            token_data = {
                "set_code": token_set_code,
                "substitute_set_code": substitute_set_code if substitute_cards else None,
                "cards": all_cards,
                "owned": sum(1 for t in all_cards if t["quantity_owned"] > 0),
                "total": len(all_cards),
                "token_count": len(token_cards),
                "substitute_count": len(substitute_cards),
            }

    return {
        "set_code": set_code,
        "set_name": set_cards[0]["set_name"] if set_cards else set_code.upper(),
        "total_cards": total_cards,
        "owned_cards": owned_cards,
        "completion_pct": completion_pct,
        "rarity_breakdown": rarity_breakdown,
        "owned_cards_list": owned_cards_list,
        "missing_cards": missing_cards,
        "visible_cards": visible_cards,
        "view": view,
        "token_data": token_data,
        "show_tokens": include_tokens,
    }


# Display order for per-rarity rows. Mythic + rare first because that's the
# meaningful signal for Commander collectors (the overall completion %
# obscures the gap between "I have most commons" and "I'm missing 3 mythics").
_RARITY_ORDER = ["mythic", "rare", "uncommon", "common", "special", "bonus"]


def _compute_rarity_breakdown(set_cards: list[dict]) -> list[dict]:
    """Return per-rarity owned/total counts for the set, ordered for display.

    Each entry: {rarity, label, owned, total, completion_pct}. Rarities with
    zero cards in the set are omitted. Unknown rarity strings are skipped
    rather than bucketed; Scryfall rarities are stable.
    """
    counts: dict[str, dict[str, int]] = {}
    for card in set_cards:
        rarity = (card.get("rarity") or "").lower()
        if rarity not in _RARITY_ORDER:
            continue
        bucket = counts.setdefault(rarity, {"owned": 0, "total": 0})
        bucket["total"] += 1
        if card.get("quantity_owned", 0) > 0:
            bucket["owned"] += 1

    breakdown: list[dict] = []
    for rarity in _RARITY_ORDER:
        bucket = counts.get(rarity)
        if not bucket or bucket["total"] == 0:
            continue
        completion_pct = round((bucket["owned"] / bucket["total"]) * 100, 2)
        breakdown.append(
            {
                "rarity": rarity,
                "label": rarity.capitalize(),
                "owned": bucket["owned"],
                "total": bucket["total"],
                "completion_pct": completion_pct,
            }
        )
    return breakdown


def list_set_completion_summaries(session: Session, user_id: int) -> list[dict]:
    """Build set-completion summaries for one user's owned sets.

    Note: this function is currently unused (no callers as of v3.27.13);
    it was scaffolding for a dashboard surface that didn't ship. Kept
    importable for potential future use. The per-set ``fetch_set_cards``
    call originally here was repointed to ``fetch_set_cards_from_cache``
    as part of v3.27.13's request-path-network-invariant restoration so
    any future wiring inherits the cache-read path by default.
    """
    owned_sets = list_owned_sets(session, user_id=user_id)
    summaries = []

    for owned_set in owned_sets:
        set_code = owned_set["set_code"]
        set_cards = fetch_set_cards_from_cache(set_code)
        owned_map = get_owned_cards_by_set(session, set_code=set_code, user_id=user_id)

        total_cards = len(set_cards)
        owned_cards = sum(1 for card in set_cards if owned_map.get(card["collector_number"], 0) > 0)
        missing_count = max(total_cards - owned_cards, 0)
        completion_pct = round((owned_cards / total_cards) * 100, 2) if total_cards else 0

        summaries.append(
            {
                "set_code": set_code,
                "set_name": owned_set["set_name"],
                "unique_owned": owned_set["unique_owned"],
                "total_cards": total_cards,
                "owned_cards": owned_cards,
                "missing_count": missing_count,
                "completion_pct": completion_pct,
            }
        )

    summaries.sort(key=lambda s: (-s["completion_pct"], s["set_code"]))
    return summaries
