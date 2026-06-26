"""Collection-aware deck recommendations (issue #51).

Deterministic Commander **Brew** generator: given a commander the user owns
and their local collection, build a legal 100-card Commander decklist from
owned cards only, with explainable per-card reasons.

NOT an LLM deckbuilder. Pure local DB data — no Scryfall on the request path
(every signal comes from persisted ``Card`` columns + ``InventoryRow`` state,
the same posture as the deck-health / theme analytics this reuses).

The flow:

    collection -> candidate pool (legal, in-identity, owned)
               -> per-card scoring (theme/role/tribal/availability/need)
               -> greedy need-aware assembly into a 100-card skeleton
               -> validation
               -> DeckRecommendation (explainable preview)
               -> optional Brew Mode deck (proxy/planning rows, no moves)

Reuses ``deck_service`` primitives (themes, roles, health, legality, create_deck)
rather than re-implementing card analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app import deck_service
from app.models import Card, InventoryRow

# --- Skeleton + scoring constants ---------------------------------------------

TARGET_TOTAL = 100
LAND_TARGET = 37  # within the 36-38 band; basics fill any shortfall

# Role minimums the assembler tries to satisfy before filling with synergy.
ROLE_TARGETS = {"Ramp": 10, "Draw": 10, "Removal": 8, "Wipe": 3}

# Base value a role contributes to a card's static score.
ROLE_WEIGHT = {
    "Ramp": 2.0,
    "Draw": 2.0,
    "Removal": 2.0,
    "Wipe": 1.5,
    "Protection": 1.5,
    "Engine": 1.5,
    "Tutor": 1.0,
    "Threat": 1.0,
    "Synergy": 1.0,
    "Hate": 0.5,
}

# Extra score for a role the deck still needs (drives need-aware assembly).
NEED_BOOST = 2.5

# Basic-land card name by WUBRG color (commander-color fill).
BASIC_LAND_BY_COLOR = {
    "W": "Plains",
    "U": "Island",
    "B": "Swamp",
    "R": "Mountain",
    "G": "Forest",
}

# Legality strings that disqualify a card from Commander.
_ILLEGAL = {"banned", "not_legal", "restricted"}


@dataclass
class DeckBuildIntent:
    commander_card_id: int
    format: str = "commander"
    target_power: str = "mid"
    primary_theme: str | None = None
    avoid_themes: set[str] = field(default_factory=set)
    use_cards_in_other_decks: bool = False
    allow_proxies: bool = False


@dataclass
class CandidateCard:
    card: Card
    owned_quantity: int
    available_quantity: int  # owned copies NOT currently in a deck
    best_inventory_row_id: int | None
    already_in_deck_names: list[str]
    tags: list[str]  # auto-detected role tags
    theme_matches: list[str]
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    deck_quantity: int = 1  # copies used in the assembled deck (>1 for basics)


@dataclass
class DeckRecommendation:
    commander: Card
    mainboard: list[CandidateCard]
    lands: list[CandidateCard]
    cuts: list[CandidateCard]
    warnings: list[str]
    role_counts: dict[str, int]
    legality: dict[str, Any]

    @property
    def total_cards(self) -> int:
        return sum(c.deck_quantity for c in self.mainboard) + sum(
            c.deck_quantity for c in self.lands
        )


# --- small deck-row shim so we can reuse deck_service analytics off-DB ---------


@dataclass
class _DeckRow:
    """Mimics the InventoryRow attributes the analytics helpers read
    (``.card`` / ``.quantity`` / ``.role`` / ``.tags``) so a preview (which has
    no persisted rows yet) can be fed to ``compute_deck_health`` etc."""

    card: Card
    quantity: int = 1
    role: str | None = None
    tags: str | None = None


# --- card predicates ----------------------------------------------------------


def _is_land(card: Card) -> bool:
    return "land" in (card.type_line or "").lower()


def _is_basic_land(card: Card) -> bool:
    return "basic land" in (card.type_line or "").lower()


def is_commander_legal(card: Card) -> bool:
    """A card is Commander-legal unless its stored legality is explicitly
    banned/not-legal. Unknown (NULL legalities, e.g. unfetched) is treated as
    allowed — these are real owned cards; we never *silently* include a card we
    know to be illegal."""
    legality = deck_service.get_card_legality(card, "commander")
    return legality not in _ILLEGAL


def can_be_commander(card: Card) -> bool:
    """v1 commander eligibility from local metadata only."""
    tl = (card.type_line or "").lower()
    oracle = (card.oracle_text or "").lower()
    return ("legendary" in tl and "creature" in tl) or "can be your commander" in oracle


def card_in_color_identity(card: Card, commander_colors: set[str]) -> bool:
    """Subset semantics (Commander 'castable within' rule). Colorless ('')
    matches any commander; NULL identity (unfetched) can't be verified → False
    so we never silently include an off-color card."""
    if card.color_identity is None:
        return False
    return set(card.color_identity) <= commander_colors


def commander_color_identity(card: Card) -> set[str] | None:
    """Commander's color identity as a WUBRG set, or None if unfetched."""
    if card.color_identity is None:
        return None
    return set(card.color_identity)


# --- candidate pool -----------------------------------------------------------


def build_candidate_pool(
    session: Session,
    user_id: int,
    commander: Card,
    intent: DeckBuildIntent,
) -> list[CandidateCard]:
    """Build the legal, in-identity, owned candidate pool from local data only.

    User-scoped. One CandidateCard per distinct ``card_id`` (the singleton
    grain); basic lands are kept (duplicates allowed downstream). Proxies are
    excluded unless ``intent.allow_proxies``.
    """
    commander_colors = commander_color_identity(commander)
    if commander_colors is None:
        return []

    rows = (
        session.query(InventoryRow)
        .options(
            joinedload(InventoryRow.card),
            joinedload(InventoryRow.storage_location),
        )
        .filter(InventoryRow.user_id == user_id)
        .all()
    )

    # Aggregate rows per card_id.
    by_card: dict[int, dict] = {}
    for row in rows:
        card = row.card
        if not card:
            continue
        if row.is_proxy and not intent.allow_proxies:
            continue
        loc = row.storage_location
        in_deck = bool(loc and loc.type == "deck")
        agg = by_card.setdefault(
            card.id,
            {
                "card": card,
                "owned": 0,
                "available": 0,
                "deck_names": [],
                "loose_row_id": None,
                "any_row_id": None,
            },
        )
        agg["owned"] += row.quantity
        agg["any_row_id"] = agg["any_row_id"] or row.id
        if in_deck:
            if loc.name not in agg["deck_names"]:
                agg["deck_names"].append(loc.name)
        else:
            agg["available"] += row.quantity
            # prefer a loose/tradeable row as the best source
            agg["loose_row_id"] = agg["loose_row_id"] or row.id

    themes = extract_themes(commander)

    pool: list[CandidateCard] = []
    for agg in by_card.values():
        card = agg["card"]
        if card.id == commander.id:
            continue  # commander is added separately, never a candidate
        if not is_commander_legal(card):
            continue
        if not card_in_color_identity(card, commander_colors):
            continue

        cand = CandidateCard(
            card=card,
            owned_quantity=agg["owned"],
            available_quantity=agg["available"],
            best_inventory_row_id=agg["loose_row_id"] or agg["any_row_id"],
            already_in_deck_names=agg["deck_names"],
            tags=deck_service.suggest_card_roles(card, themes),
            theme_matches=[],
        )
        score_candidate(cand, themes, intent)
        pool.append(cand)

    return pool


def extract_themes(commander: Card) -> dict:
    """Commander themes via the existing extractor (wants rows with ``.card``)."""
    return deck_service.extract_commander_themes([_DeckRow(card=commander)])


# --- scoring ------------------------------------------------------------------


def score_candidate(
    cand: CandidateCard,
    themes: dict,
    intent: DeckBuildIntent,
    needs: dict[str, int] | None = None,
) -> float:
    """Deterministically score a candidate and record human-readable reasons.

    ``needs`` (role -> remaining-needed) is supplied during assembly so role
    cards the deck still lacks get boosted ("boosts role cards when deck lacks
    ramp/draw/removal"). Without it, the static base score is computed.
    """
    card = cand.card
    score = 0.0
    reasons: list[str] = []
    oracle = (card.oracle_text or "").lower()
    tl_words = set((card.type_line or "").split())

    # Commander theme fit.
    if deck_service.card_matches_theme(card, themes):
        score += 3.0
        label = (themes.get("signals") or ["theme"])[0]
        reasons.append(f"Matches commander theme: {label}")
        cand.theme_matches = list(themes.get("signals") or [])

    # Tribal subtype match.
    tribal = [st for st in themes.get("subtypes", set()) if st in tl_words]
    for st in tribal:
        score += 2.0
        reasons.append(f"Tribal match: {st}")

    # Role tags.
    for role in cand.tags:
        score += ROLE_WEIGHT.get(role, 0.5)
        reasons.append(f"Role: {role}")

    # Token production.
    if "create" in oracle and "token" in oracle:
        score += 1.0
        reasons.append("Token production")

    # Curve: gentle preference for cheaper non-lands.
    if not _is_land(card) and card.cmc is not None and card.cmc <= 3:
        score += 0.5

    # Availability — a loose copy is better than one already committed.
    if cand.available_quantity > 0:
        score += 1.0
        reasons.append("Loose copy available")

    # Already in another deck — penalize unless the user opted in.
    if cand.already_in_deck_names and not intent.use_cards_in_other_decks:
        score -= 2.0
        reasons.append("In another deck (penalty)")

    # Theme intent nudges.
    if intent.primary_theme and intent.primary_theme.lower() in oracle:
        score += 1.5
        reasons.append(f"Preferred theme: {intent.primary_theme}")
    for avoid in intent.avoid_themes:
        if avoid.lower() in oracle:
            score -= 2.0
            reasons.append(f"Avoided theme: {avoid}")

    # Need-aware boost (assembly phase only).
    if needs:
        for role in cand.tags:
            if needs.get(role, 0) > 0:
                score += NEED_BOOST
                reasons.append(f"Helps deck need: {role}")
                break

    if needs is None:
        # Static pass: persist on the candidate.
        cand.score = score
        cand.reasons = reasons
    return score


# --- assembly -----------------------------------------------------------------


def assemble_deck(
    commander: Card,
    pool: list[CandidateCard],
    intent: DeckBuildIntent,
    themes: dict,
) -> tuple[list[CandidateCard], list[CandidateCard], list[CandidateCard]]:
    """Greedy need-aware assembly into (mainboard-spells, lands, cuts).

    Lands fill to ``LAND_TARGET`` (nonbasic owned first, basics for the rest);
    spells fill the remaining slots, satisfying role minimums first via a
    marginal need-aware score. No duplicate nonbasic names; exactly one of each.
    """
    nonbasic_lands = [c for c in pool if _is_land(c.card) and not _is_basic_land(c.card)]
    basics = [c for c in pool if _is_basic_land(c.card)]
    spells = [c for c in pool if not _is_land(c.card)]

    used_names = {commander.name}

    # --- lands ---
    chosen_lands: list[CandidateCard] = []
    for cand in sorted(nonbasic_lands, key=lambda c: (-c.score, c.card.name)):
        if len(chosen_lands) >= LAND_TARGET:
            break
        if cand.card.name in used_names:
            continue
        used_names.add(cand.card.name)
        chosen_lands.append(cand)

    basic_needed = LAND_TARGET - len(chosen_lands)
    chosen_lands.extend(_pick_basics(commander, basics, basic_needed))

    land_count = sum(c.deck_quantity for c in chosen_lands)

    # --- spells (need-aware greedy) ---
    spell_slots = TARGET_TOTAL - 1 - land_count  # minus commander
    needs = dict(ROLE_TARGETS)
    remaining = [c for c in spells if c.card.name not in used_names]
    chosen_spells: list[CandidateCard] = []

    while remaining and len(chosen_spells) < spell_slots:
        best = max(
            remaining,
            key=lambda c: (score_candidate(c, themes, intent, needs), -_name_key(c)),
        )
        remaining.remove(best)
        used_names.add(best.card.name)
        chosen_spells.append(best)
        # drop any other printing of the same name (nonbasic singleton rule)
        remaining = [c for c in remaining if c.card.name not in used_names]
        for role in best.tags:
            if role in needs and needs[role] > 0:
                needs[role] -= 1

    # leftover high-scorers become explainable "cuts"
    cuts = sorted(remaining, key=lambda c: (-c.score, c.card.name))[:15]

    return chosen_spells, chosen_lands, cuts


def _name_key(cand: CandidateCard) -> int:
    """Stable deterministic tiebreaker from the card name."""
    return cand.card.id


def _pick_basics(commander: Card, basics: list[CandidateCard], count: int) -> list[CandidateCard]:
    """Distribute ``count`` basic lands across the commander's colors, using
    only basic types the user actually owns. Returns one CandidateCard per
    basic type with ``deck_quantity`` set (duplicates allowed for basics)."""
    if count <= 0:
        return []
    commander_colors = commander_color_identity(commander) or set()
    owned_by_name = {c.card.name: c for c in basics}
    # basic types matching the commander's colors that the user owns
    wanted = [
        owned_by_name[name]
        for color in sorted(commander_colors)
        for name in (BASIC_LAND_BY_COLOR.get(color),)
        if name and name in owned_by_name
    ]
    if not wanted:
        return []
    # round-robin distribute
    per = [0] * len(wanted)
    for i in range(count):
        per[i % len(wanted)] += 1
    out = []
    for cand, qty in zip(wanted, per, strict=False):
        if qty:
            cand.deck_quantity = qty
            cand.reasons = [f"Basic land ({qty})"]
            out.append(cand)
    return out


# --- top-level generation + validation ----------------------------------------


def validate_commander(
    session: Session, user_id: int, card_id: int
) -> tuple[Card | None, list[str]]:
    """Resolve and validate a commander selection. Returns (card, warnings);
    card is None when the selection cannot be used as a commander."""
    warnings: list[str] = []
    card = session.query(Card).filter(Card.id == card_id).first()
    if not card:
        return None, ["Card not found."]

    owned = (
        session.query(InventoryRow)
        .filter(InventoryRow.user_id == user_id, InventoryRow.card_id == card_id)
        .first()
    )
    if not owned:
        return None, ["You don't own this card."]

    if not is_commander_legal(card):
        return None, [f"{card.name} is not legal in Commander."]
    if not can_be_commander(card):
        return None, [f"{card.name} can't be used as a commander (not a legendary creature)."]
    if commander_color_identity(card) is None:
        return None, [
            f"Color identity for {card.name} is unknown (metadata not yet fetched); "
            "can't build a legal deck."
        ]
    return card, warnings


def generate_recommendation(
    session: Session, user_id: int, intent: DeckBuildIntent
) -> DeckRecommendation:
    """End-to-end: validate commander, build pool, assemble, validate result."""
    commander, warnings = validate_commander(session, user_id, intent.commander_card_id)
    if commander is None:
        # Return an empty, warning-only recommendation rather than raising —
        # the route shows the warnings instead of a broken deck.
        placeholder = session.query(Card).filter(Card.id == intent.commander_card_id).first()
        return DeckRecommendation(
            commander=placeholder,
            mainboard=[],
            lands=[],
            cuts=[],
            warnings=warnings,
            role_counts={},
            legality={"ok": False},
        )

    themes = extract_themes(commander)
    pool = build_candidate_pool(session, user_id, commander, intent)
    spells, lands, cuts = assemble_deck(commander, pool, intent, themes)

    # commander goes at the head of the mainboard list
    commander_cand = CandidateCard(
        card=commander,
        owned_quantity=1,
        available_quantity=0,
        best_inventory_row_id=None,
        already_in_deck_names=[],
        tags=["commander"],
        theme_matches=list(themes.get("signals") or []),
        score=999.0,
        reasons=["Commander"],
    )
    mainboard = [commander_cand] + spells

    rec = DeckRecommendation(
        commander=commander,
        mainboard=mainboard,
        lands=lands,
        cuts=cuts,
        warnings=list(warnings),
        role_counts={},
        legality={},
    )
    _validate_and_annotate(rec, commander, intent)
    return rec


def _validate_and_annotate(
    rec: DeckRecommendation, commander: Card, intent: DeckBuildIntent
) -> None:
    """Validate the assembled deck and fill role_counts / legality / warnings."""
    all_cands = rec.mainboard + rec.lands
    commander_colors = commander_color_identity(commander) or set()

    total = rec.total_cards
    if total != TARGET_TOTAL:
        rec.warnings.append(
            f"Deck has {total} cards (need {TARGET_TOTAL}) — your collection may "
            "not have enough in-color cards to fill a full deck."
        )

    # nonbasic singleton check
    seen: set[str] = set()
    for cand in all_cands:
        if _is_basic_land(cand.card):
            continue
        if cand.card.name in seen:
            rec.warnings.append(f"Duplicate nonbasic card: {cand.card.name}")
        seen.add(cand.card.name)

    # legality + color identity (defense in depth — pool already filters)
    for cand in all_cands:
        if cand.card.id == commander.id:
            continue
        if not is_commander_legal(cand.card):
            rec.warnings.append(f"Illegal in Commander: {cand.card.name}")
        if not card_in_color_identity(cand.card, commander_colors):
            rec.warnings.append(f"Outside color identity: {cand.card.name}")

    # proxy guard
    if not intent.allow_proxies:
        # pool excluded proxies already; nothing to flag here.
        pass

    # role counts + health (must not crash)
    rows = [
        _DeckRow(
            card=c.card,
            quantity=c.deck_quantity,
            role=("commander" if c.card.id == commander.id else None),
        )
        for c in all_cands
    ]
    try:
        health = deck_service.compute_deck_health(rows)
        rec.role_counts = {
            "lands": sum(c.deck_quantity for c in rec.lands),
            "ramp": health["ramp"]["count"],
            "draw": health["draw"]["count"],
            "removal": health["removal"]["count"],
            "wipes": health["wipes"]["count"],
        }
        rec.legality = {"ok": not rec.warnings, "commander": "legal"}
    except Exception as exc:  # pragma: no cover - defensive
        rec.warnings.append(f"Could not compute deck analytics: {exc}")
        rec.legality = {"ok": False}


# --- Brew creation ------------------------------------------------------------


def create_brew_from_recommendation(
    session: Session,
    user_id: int,
    rec: DeckRecommendation,
    deck_name: str,
):
    """Persist the recommendation as a Brew Mode deck.

    Cards are added as **proxy/planning** InventoryRows in the deck's location
    — NO physical inventory is moved and no existing deck is touched (v1; a
    later "materialize" issue can pull owned copies in). The brew buy-list
    already reads ``is_proxy`` to classify owned-elsewhere vs to-buy.
    """
    deck = deck_service.create_deck(
        session, user_id, deck_name, format_name="commander", is_brew=True
    )
    for cand in rec.mainboard + rec.lands:
        is_cmdr = cand.card.id == rec.commander.id
        session.add(
            InventoryRow(
                user_id=user_id,
                card_id=cand.card.id,
                storage_location_id=deck.storage_location_id,
                finish="normal",
                quantity=cand.deck_quantity,
                is_pending=False,
                is_proxy=True,
                role="commander" if is_cmdr else None,
            )
        )
    session.commit()
    return deck


def list_commander_candidates(session: Session, user_id: int) -> list[Card]:
    """Owned, Commander-legal legendary creatures the user could pick."""
    rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_proxy.is_(False))
        .all()
    )
    seen: dict[int, Card] = {}
    for row in rows:
        card = row.card
        if not card or card.id in seen:
            continue
        if can_be_commander(card) and is_commander_legal(card):
            seen[card.id] = card
    return sorted(seen.values(), key=lambda c: c.name or "")
