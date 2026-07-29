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

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app import deck_service
from app.models import Card, DeckStrategyProfile, InventoryRow, StorageLocation
from app.timeutil import utc_now

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

# Score adjustment per role-usefulness relevance (issue #60 P2). Supplements —
# never replaces — the existing factors. "very_low" (dead role) additionally
# hard-excludes the card from spell assembly; the -25 keeps it ranked last
# everywhere scores are compared (max legit score is ~15, so a dead card can
# never out-rank a live one).
RELEVANCE_SCORE_ADJUST = {"very_low": -25.0, "low": -2.0, "medium": 0.0, "high": 2.0}

# Basic-land card name by WUBRG color (commander-color fill).
BASIC_LAND_BY_COLOR = {
    "W": "Plains",
    "U": "Island",
    "B": "Swamp",
    "R": "Mountain",
    "G": "Forest",
}

# Colorless commanders fill with Wastes (the only colorless basic).
COLORLESS_BASIC = "Wastes"

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
    # v2 substrate output (issue #60 P2) — set when a strategy profile is in play.
    role_subtype: tuple[str | None, str | None] | None = None  # (broad_role, subtype)
    role_relevance: str | None = None  # one of RELEVANCE_LEVELS
    role_reason: str | None = None


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


def _front_face(type_line: str | None) -> str:
    """The front face of a stored ``type_line``, lowercased.

    ``cards.type_line`` holds Scryfall's ROOT type line, which for every
    multi-face layout is ``"Front // Back"``. Splitting on the literal separator
    is deliberate and does NOT consult ``cards.layout``: layout is NULL on 15
    rows, and front-face-first ordering was verified to hold for every multi-face
    layout present in the data (transform, modal_dfc, flip, adventure, split,
    prepare, reversible_card, art_series, double_faced_token). A single-faced
    type line has no separator and passes through unchanged.
    """
    return (type_line or "").split(" // ")[0].lower()


def can_be_commander(card: Card) -> bool:
    """Commander eligibility from local metadata. Multi-face cards are judged on
    their FRONT face only: outside the game a card has the characteristics of its
    front face (CR 712.4a), so a back-face legendary creature does not make the
    card a legal commander (CR 903.3a).

    The old predicate substring-matched the COMBINED type line, so anything with a
    legendary creature on the back qualified — Invasion of Fiora (a Battle), The
    Legend of Kyoshi (a Saga), Westvale Abbey (a Land), Nezumi Graverobber (a flip
    card). Eight owned cards across six accounts were affected.

    KNOWN GAP, deliberately not closed here: the ``can be your commander`` branch
    is NOT face-scoped. ``_normalize_card_payload`` joins multi-face oracle text
    with ``\\n\\n``, not ``//``, so per-face text is unrecoverable from the stored
    column. No card currently has that phrase together with a multi-face type
    line, so the branch is harmless today; closing it properly needs a per-face
    column.
    """
    front = _front_face(card.type_line)
    oracle = (card.oracle_text or "").lower()
    if "legendary" in front and "creature" in front:
        return True
    if "can be your commander" in oracle:
        return True
    # Grist-class: a legendary card that is a CREATURE CARD everywhere except the
    # battlefield, which makes it a legal commander despite a non-creature front
    # type line. Grist, the Hunger Tide is the only card in the corpus matching.
    return "legendary" in front and "isn't on the battlefield" in oracle and "creature" in oracle


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
    profile: dict | None = None,
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
        score_candidate(cand, themes, intent, profile=profile)
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
    profile: dict | None = None,
) -> float:
    """Deterministically score a candidate and record human-readable reasons.

    ``needs`` (role -> remaining-needed) is supplied during assembly so role
    cards the deck still lacks get boosted ("boosts role cards when deck lacks
    ramp/draw/removal"). Without it, the static base score is computed.

    ``profile`` (a strategy profile from ``seed_strategy_profile``) turns on
    the v2 role-usefulness adjustment (issue #60 P2): the card's
    (broad_role, subtype) classification and relevance are computed once,
    cached on the candidate, and folded into the score via
    ``RELEVANCE_SCORE_ADJUST``. Without a profile, scoring is unchanged.
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

    # Already committed to another deck — penalize ONLY when there's no loose
    # copy to draw from (all owned copies are in decks). A user who owns a loose
    # copy AND a committed one must not be penalized for the duplicate; the loose
    # copy is what the brew would use.
    if (
        cand.already_in_deck_names
        and cand.available_quantity == 0
        and not intent.use_cards_in_other_decks
    ):
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

    # Role-usefulness adjustment (issue #60 P2). Classification is
    # deterministic per (card, profile), so cache it on the candidate — the
    # assembly loop re-scores candidates O(n²) times.
    if profile is not None:
        if cand.role_relevance is None:
            colors = _profile_colors(profile)
            broad, subtype, _ = classify_role_subtype(card, colors)
            relevance, reason = score_role_usefulness(card, broad, subtype, colors, profile)
            cand.role_subtype = (broad, subtype)
            cand.role_relevance = relevance
            cand.role_reason = reason
        score += RELEVANCE_SCORE_ADJUST[cand.role_relevance]
        if cand.role_reason and cand.role_reason not in reasons:
            reasons.append(cand.role_reason)

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
    profile: dict | None = None,
) -> tuple[list[CandidateCard], list[CandidateCard], list[CandidateCard]]:
    """Greedy need-aware assembly into (mainboard-spells, lands, cuts).

    Lands fill to ``LAND_TARGET`` (nonbasic owned first, basics for the rest);
    spells fill the remaining slots, satisfying role minimums first via a
    marginal need-aware score. No duplicate nonbasic names; exactly one of each.

    Dead-role cards (``role_relevance == "very_low"``, e.g. a color-specific
    cost reducer outside the deck's identity) are never selected as spells —
    they go straight to cuts with their reason. The deck may come up short
    instead (the 100-card validation surfaces that as a warning).
    """
    nonbasic_lands = [c for c in pool if _is_land(c.card) and not _is_basic_land(c.card)]
    basics = [c for c in pool if _is_basic_land(c.card)]
    spells = [c for c in pool if not _is_land(c.card)]
    dead = [c for c in spells if c.role_relevance == "very_low"]
    spells = [c for c in spells if c.role_relevance != "very_low"]

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
            key=lambda c: (score_candidate(c, themes, intent, needs, profile), -_name_key(c)),
        )
        remaining.remove(best)
        used_names.add(best.card.name)
        chosen_spells.append(best)
        # drop any other printing of the same name (nonbasic singleton rule)
        remaining = [c for c in remaining if c.card.name not in used_names]
        # Persist the need-aware reason on the card that filled the need (the
        # static reasons were recorded at pool build; this is additive), reading
        # `needs` BEFORE decrementing so the role it actually filled is recorded.
        reason_recorded = False
        for role in best.tags:
            if role in needs and needs[role] > 0:
                if not reason_recorded:
                    reason = f"Helps deck need: {role}"
                    if reason not in best.reasons:
                        best.reasons.append(reason)
                    reason_recorded = True
                needs[role] -= 1

    # leftover high-scorers become explainable "cuts"; dead-role cards always
    # appear here (sorted last by their penalized score) so the user sees why.
    cuts = sorted(remaining + dead, key=lambda c: (-c.score, c.card.name))[:15]

    return chosen_spells, chosen_lands, cuts


def _name_key(cand: CandidateCard) -> int:
    """Stable deterministic tiebreaker (the card id)."""
    return cand.card.id


def _pick_basics(commander: Card, basics: list[CandidateCard], count: int) -> list[CandidateCard]:
    """Distribute ``count`` basic lands across the commander's colors, using
    only basic types the user actually owns. Returns one CandidateCard per
    basic type with ``deck_quantity`` set (duplicates allowed for basics)."""
    if count <= 0:
        return []
    commander_colors = commander_color_identity(commander) or set()
    # A real collection fragments basics across many sets/printings — each
    # printing is a distinct card_id and thus a distinct CandidateCard. Sum the
    # owned copies across ALL printings of each basic type (keeping one
    # representative card), else collapsing by name would discard every printing
    # but one and under-fill the deck.
    totals: dict[str, int] = {}
    reps: dict[str, CandidateCard] = {}
    for c in basics:
        totals[c.card.name] = totals.get(c.card.name, 0) + c.owned_quantity
        reps.setdefault(c.card.name, c)
    # Basic types matching the commander's colors that the user owns. A
    # colorless commander (empty identity) fills with Wastes.
    if commander_colors:
        names = [
            BASIC_LAND_BY_COLOR[color]
            for color in sorted(commander_colors)
            if color in BASIC_LAND_BY_COLOR
        ]
    else:
        names = [COLORLESS_BASIC]
    names = [n for n in names if n in reps]
    if not names:
        return []
    # Round-robin distribute, but NEVER assign more copies of a basic than the
    # user actually owns (across all printings) — an "impossible owned-card
    # count" must not pass silently. The deck may fall short of LAND_TARGET (the
    # 100-card validation surfaces that as a warning) rather than fabricate
    # basics nobody owns.
    per = {name: 0 for name in names}
    placed = 0
    progressed = True
    while placed < count and progressed:
        progressed = False
        for name in names:
            if placed >= count:
                break
            if per[name] < totals[name]:
                per[name] += 1
                placed += 1
                progressed = True
    out = []
    for name in names:
        qty = per[name]
        if qty:
            cand = reps[name]
            cand.deck_quantity = qty
            cand.owned_quantity = totals[name]  # credit the full owned total so
            # the line-100-validation owned-count cap reflects all printings
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
    profile = seed_strategy_profile(commander, commander_color_identity(commander) or set())
    pool = build_candidate_pool(session, user_id, commander, intent, profile=profile)
    spells, lands, cuts = assemble_deck(commander, pool, intent, themes, profile=profile)

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

    # impossible owned-card count — the deck must never use more copies of a
    # card than the user owns (basics are capped at owned in _pick_basics; this
    # is the validation backstop the acceptance criteria require).
    for cand in all_cands:
        if cand.card.id == commander.id:
            continue
        if cand.deck_quantity > cand.owned_quantity:
            rec.warnings.append(
                f"Needs {cand.deck_quantity} copies of {cand.card.name} "
                f"but you own {cand.owned_quantity}."
            )

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


# --- Deckbuilder v2 substrate (issue #60, P1) -----------------------------------
#
# Pure deterministic functions over local Card data: role-subtype classification,
# role-usefulness scoring against a strategy profile, dead-role detection, and
# deck-plan coverage evaluation. No LLM, no external calls, no persistence
# (strategy profiles are in-memory seeds until P3). score_candidate integration
# is P2 — nothing below is wired into the generator yet.

RELEVANCE_LEVELS = ("very_low", "low", "medium", "high")

_COLOR_WORDS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}

# "Blue spells you cast cost {1} less" / "Red creature spells you cast cost..."
_COLOR_REDUCER_RE = re.compile(
    r"\b(white|blue|black|red|green)\b(?: creature)? spells you cast cost", re.IGNORECASE
)
_ANY_COLOR_FIXER_RE = re.compile(r"\badds? one mana of any color\b", re.IGNORECASE)
_SAC_TO_DRAW_RE = re.compile(r"sacrifice [^:.]{0,40}:\s*draw a card", re.IGNORECASE)
_TOPDECK_RE = re.compile(r"look at the top|\bscry\b|\bsurveil\b", re.IGNORECASE)
# Repeatable, opponent-turn-capable draw: untaps on other players' turns, or an
# activated ability that makes each player draw (Mikokoro / Geier Reach shape).
_OPP_UNTAP_RE = re.compile(r"each other player'?s untap step", re.IGNORECASE)
_ACTIVATED_GROUP_DRAW_RE = re.compile(r"\{[^}]+\}[^:.]*:\s*each player draws", re.IGNORECASE)
_SYMMETRICAL_DRAW_RE = re.compile(r"each player draws", re.IGNORECASE)
_TRIBAL_SUPPORT_RE = re.compile(r"choose a creature type|of the chosen type", re.IGNORECASE)

# --- issue #88 supplemental patterns -------------------------------------------
# The v1 tagger (deck_service.suggest_card_roles) misses whole families of cards
# that clearly belong to existing categories. Rather than widen the app-wide
# regexes (blast radius: every collection re-tags), these read the SAME oracle
# text one level deeper and only run when the v1 tagger returns nothing — so the
# base tagger and its tests are untouched. New patterns map into existing broad
# roles (Protection / Removal / Engine / Threat) with new subtypes; no new
# top-level categories (issue #88 constraint).

# Protection: grant a defensive keyword to *another* permanent, or hard "can't
# lose". Deliberately anchored on "equipped/enchanted creature has …" and
# "protection from" so a bare "Indestructible" keyword line on the card itself
# (e.g. Stuffy Doll) does NOT read as protection — that card is redirection.
_GRANT_PROTECTION_RE = re.compile(
    r"(?:equipped|enchanted) creature (?:has|gains?)[^.]{0,40}"
    r"\b(?:hexproof|shroud|ward|indestructible|protection from)\b"
    r"|(?:creatures?|permanents?|other permanents?) you control (?:have|has|gain|gains?)"
    r"[^.]{0,10}\b(?:hexproof|shroud|indestructible|protection from)\b"
    r"|\bhas protection from\b"
    r"|(?:can'?t|cannot) lose the game",
    re.IGNORECASE,
)
# Damage redirection: the deck's player-protection lock. Three shapes —
# "…damage…is dealt to <creature> instead" (Pariah/Pariah's Shield/Saving
# Grace/Martyrdom), "prevent that damage…deals that much damage to" (Phyrexian
# Vindicator), and "whenever ~ is dealt damage, it deals that much damage to"
# (Stuffy Doll).
_DAMAGE_REDIRECT_RE = re.compile(
    r"damage[^.]{0,80}is dealt to [^.]{0,40}(?:creature|permanent) instead"
    # Prevent-then-reflect (Phyrexian Vindicator) crosses a sentence boundary
    # ("prevent that damage. When damage is prevented this way…"), so this
    # branch spans periods where the others clamp to one sentence.
    r"|prevent(?:s|ed)? (?:that|this) damage[\s\S]{0,90}deals? that much damage to"
    r"|whenever [^.]{0,40}is dealt damage,? [^.]{0,20}deals? that much damage to",
    re.IGNORECASE,
)
# Removal the v1 regex misses: "exile up to one target nonland permanent"
# (Grasp of Fate — words between "exile" and "target" defeat _REMOVAL_RE), and
# multiplayer exile edicts ("exile … each opponent").
_EXILE_REMOVAL_RE = re.compile(
    r"exile[^.]{0,40}\bnonland permanent\b"
    r"|exile[^.]{0,60}\btarget\b[^.]{0,40}\beach opponent\b",
    re.IGNORECASE,
)
# Engine: counter multipliers (Lae'zel, Doubling Season shape).
_COUNTER_DOUBLER_RE = re.compile(
    r"if [^.]{0,60}(?:would put|would be placed|put)[^.]{0,20}one or more[^.]{0,20}counters?"
    r"|double the number of[^.]{0,30}counters?",
    re.IGNORECASE,
)
# Type-scoped (non-color) cost reduction — Flowering-style "legendary spells you
# cast cost {N} less", "Aura and Equipment spells … cost {N} less". These are
# real ramp but must NOT read as color_specific_reducer (that fires only on WUBRG
# color words, so this is already safe; the pattern exists to give them a
# cost_reduction subtype instead of falling to early/expensive_ramp).
_TYPE_SCOPED_REDUCER_RE = re.compile(
    r"\b(?:legendary|artifact|creature|enchantment|aura|equipment|historic|"
    r"noncreature|colorless|multicolored)\b[^.]{0,40}spells you cast cost \{?\d",
    re.IGNORECASE,
)
# Voltron buffs live on Equipment/Auras only — gate on type_line so anthems and
# board pumps don't false-positive. Power bonus, double strike / extra combat,
# creature-copy, or the deathtouch+lifelink combo all read as "this is the kill".
_VOLTRON_BUFF_RE = re.compile(
    r"(?:equipped|enchanted) creature (?:gets|has|gains?)[^.]{0,40}[+][\dxX]+/[+-]?[\dxX]+"
    r"|power and toughness [^.]{0,20}equal to"
    r"|(?:equipped|enchanted) creature (?:has|gains?)[^.]{0,30}double strike"
    r"|additional combat phase"
    r"|copy of (?:equipped|that) creature"
    r"|\b(?:deathtouch and lifelink|lifelink and deathtouch)\b",
    re.IGNORECASE,
)


def _supplemental_role(card: Card) -> tuple[str, str] | None:
    """Second-pass classification for cards the v1 tagger leaves untagged
    (issue #88). Returns (broad_role, subtype) mapped into an EXISTING category,
    or None. Priority order resolves overlaps: redirection (a card that is both
    indestructible and a damage lock, e.g. Stuffy Doll, is redirection first) →
    protection → removal → engine → ramp → voltron."""
    oracle = card.oracle_text or ""
    tl = (card.type_line or "").lower()
    if _DAMAGE_REDIRECT_RE.search(oracle):
        return ("Protection", "damage_redirection")
    if _GRANT_PROTECTION_RE.search(oracle):
        return ("Protection", "keyword_grant")
    if _EXILE_REMOVAL_RE.search(oracle):
        return ("Removal", "exile")
    if _COUNTER_DOUBLER_RE.search(oracle):
        return ("Engine", "counter_multiplier")
    if _TYPE_SCOPED_REDUCER_RE.search(oracle):
        return ("Ramp", "cost_reduction")
    if ("equipment" in tl or "aura" in tl) and _VOLTRON_BUFF_RE.search(oracle):
        return ("Threat", "voltron_buff")
    return None


def classify_role_subtype(
    card: Card, deck_color_identity: set[str]
) -> tuple[str | None, str | None, str]:
    """Classify a card into (broad_role, subtype, confidence).

    Broad role comes from the existing ``deck_service.suggest_card_roles``
    heuristics (first match wins — the suggestion order is already
    priority-ordered). Subtypes are finer-grained oracle-text reads scoped to
    the broad role. An unrecognized card degrades to ``(broad_role, None)``
    and MUST never be treated as dead downstream; a card with no detected
    role at all returns ``(None, None, "low")``.
    """
    oracle = card.oracle_text or ""
    roles = deck_service.suggest_card_roles(card)
    if not roles:
        # Pure topdeck manipulation (Scry/Surveil-only cards) has no broad
        # role in the v1 tagger but is a first-class plan category here.
        if _TOPDECK_RE.search(oracle):
            return ("Draw", "topdeck_manipulation", "medium")
        if _TRIBAL_SUPPORT_RE.search(oracle):
            return ("Synergy", "tribal_support", "medium")
        # issue #88: families the v1 tagger misses entirely (indestructible
        # grants, damage redirection, Voltron equipment, exile edicts,
        # counter-doublers) map into existing categories with new subtypes.
        supplemental = _supplemental_role(card)
        if supplemental is not None:
            return (*supplemental, "medium")
        return (None, None, "low")
    broad = roles[0]

    if broad == "Ramp":
        m = _COLOR_REDUCER_RE.search(oracle)
        if m:
            color = _COLOR_WORDS[m.group(1).lower()]
            if color not in deck_color_identity:
                return ("Ramp", "color_specific_reducer", "high")
            return ("Ramp", "cost_reduction", "high")
        if _ANY_COLOR_FIXER_RE.search(oracle):
            return ("Ramp", "color_fixer", "high")
        if _SAC_TO_DRAW_RE.search(oracle):
            return ("Ramp", "sacrifice_to_draw", "high")
        if card.cmc is not None and card.cmc <= 3:
            return ("Ramp", "early_ramp", "medium")
        if card.cmc is not None and card.cmc >= 5:
            return ("Ramp", "expensive_ramp", "medium")
        return ("Ramp", None, "medium")

    if broad == "Draw":
        if _OPP_UNTAP_RE.search(oracle) or _ACTIVATED_GROUP_DRAW_RE.search(oracle):
            return ("Draw", "opponent_turn_draw", "high")
        if _TOPDECK_RE.search(oracle):
            return ("Draw", "topdeck_manipulation", "high")
        if _SAC_TO_DRAW_RE.search(oracle):
            return ("Draw", "sacrifice_to_draw", "high")
        if _SYMMETRICAL_DRAW_RE.search(oracle):
            return ("Draw", "symmetrical_draw", "medium")
        return ("Draw", None, "medium")

    return (broad, None, "medium")


_RELEVANCE_REASON = {
    "high": "High: {label} is a priority role in this deck's plan",
    "medium": "Medium: {label} supports the deck plan",
    "low": "Low value: {label} is low priority for this deck's plan",
}


def _role_label(broad_role: str, subtype: str | None) -> str:
    return (subtype or broad_role).replace("_", " ").lower()


def score_role_usefulness(
    card: Card,
    broad_role: str | None,
    subtype: str | None,
    deck_color_identity: set[str],
    strategy_profile: dict,
) -> tuple[str, str]:
    """Score how useful a card's role is *in this deck*: (relevance, reason).

    relevance is one of RELEVANCE_LEVELS; reason is a deterministic template
    string (no LLM). Hard dead-role rules (issue #60 section 4) run first,
    then the strategy profile's high/medium/low lists (subtype match wins over
    broad-role match), then heuristic downgrades. Unknown subtypes fall
    through to medium — never dead.
    """
    if broad_role is None:
        return ("medium", "No detected role; not scored against the deck plan")
    oracle = card.oracle_text or ""

    # Hard dead-role rules.
    if subtype == "color_specific_reducer":
        m = _COLOR_REDUCER_RE.search(oracle)
        color = m.group(1).lower() if m else "off-color"
        return ("very_low", f"Dead: reduces cost of {color} spells, but deck has no {color}")
    if subtype == "color_fixer" and not deck_color_identity:
        # ponytail: colorless identity is the proxy for "no colored activation
        # costs"; per-deck cost scanning can arrive with the P3 analyzer.
        return ("low", "Low value: color fixing in a colorless deck with no colored costs")

    label = _role_label(broad_role, subtype)
    for level in ("high", "medium", "low"):
        keys = strategy_profile.get(level) or []
        if (subtype is not None and subtype in keys) or broad_role.lower() in keys:
            return (level, _RELEVANCE_REASON[level].format(label=label))

    # Profile is silent — heuristic downgrades for known-weak subtypes.
    if subtype == "symmetrical_draw":
        return ("low", "Low value: symmetrical draw the deck can't exploit better than opponents")
    if subtype == "expensive_ramp" and "early_ramp" in (
        (strategy_profile.get("high") or []) + (strategy_profile.get("medium") or [])
    ):
        return ("low", "Low value: expensive ramp in a deck that wants early acceleration")
    if subtype == "tribal_support":
        return ("low", "Low value: creature-type support without a matching tribal payoff")
    return ("medium", _RELEVANCE_REASON["medium"].format(label=label))


# Generic coverage targets used by the heuristic profile seed. Commander-specific
# profiles (e.g. the Molecule Man test fixture) override these wholesale.
_DEFAULT_PLAN_TARGETS = {
    "lands": (36, 38),
    "ramp": (10, 14),
    "topdeck_manipulation": (0, 12),
    "opponent_turn_draw": (0, 10),
    "large_payoffs": (8, 14),
    "removal_wipes": (8, 12),
    "protection": (4, 8),
    "win_conditions": (3, 6),
}


def seed_strategy_profile(commander_card: Card, deck_color_identity: set[str]) -> dict:
    """Heuristically seed a strategy profile from color identity + commander
    themes. This is a starting point, not an authority — profiles become
    user-editable (and persisted) in P3.
    """
    themes = extract_themes(commander_card)
    oracle = (commander_card.oracle_text or "").lower()
    colorless = not deck_color_identity

    high: list[str] = []
    if "draw" in oracle:
        # Commander cares about drawing — repeatable/opponent-turn draw and
        # topdeck control feed it.
        high += ["opponent_turn_draw", "topdeck_manipulation", "sacrifice_to_draw"]
    low = ["symmetrical_draw"]
    if colorless:
        low = ["color_fixer", "color_specific_reducer"] + low
    if themes.get("subtypes"):
        high.append("tribal_support")
    else:
        low.append("tribal_support")

    return {
        "color_identity": "colorless" if colorless else "".join(sorted(deck_color_identity)),
        "high": high,
        "medium": ["early_ramp", "ramp", "draw", "removal", "wipe", "protection", "engine"],
        "low": low,
        "targets": dict(_DEFAULT_PLAN_TARGETS),
    }


def _profile_colors(strategy_profile: dict) -> set[str]:
    ci = strategy_profile.get("color_identity") or ""
    return set() if ci == "colorless" else set(ci)


def _coverage_categories(card: Card, deck_colors: set[str]) -> list[str]:
    """Plan-coverage categories a card counts toward. ponytail: uses the
    primary (broad, subtype) classification only; multi-role credit can come
    with the P3 analyzer if targets prove too coarse."""
    cats: list[str] = []
    if _is_land(card):
        cats.append("lands")
    broad, subtype, _ = classify_role_subtype(card, deck_colors)
    if broad == "Ramp":
        cats.append("ramp")
    if subtype == "topdeck_manipulation":
        cats.append("topdeck_manipulation")
    if subtype == "opponent_turn_draw":
        cats.append("opponent_turn_draw")
    if broad in ("Removal", "Wipe"):
        cats.append("removal_wipes")
    if broad == "Protection":
        cats.append("protection")
    if broad == "Threat":
        cats.append("win_conditions")
    if not _is_land(card) and (card.cmc or 0) >= 6:
        cats.append("large_payoffs")
    return cats


# A plan-coverage category maps to one or more profile priority KEYS (the
# high/medium/low lists are keyed by role/subtype names). Most categories match
# their own name; the two compound categories carry aliases (issue #88 P4).
_CATEGORY_PRIORITY_KEYS = {
    "removal_wipes": ("removal_wipes", "removal", "wipe"),
    "win_conditions": ("win_conditions", "threat", "voltron_buff"),
}


def _category_priority(category: str, strategy_profile: dict) -> str:
    """Resolve a coverage category's priority tier from the strategy profile
    (issue #88 P4). Returns 'high' | 'medium' | 'low'; medium when the category
    isn't listed in either the high or low tier."""
    keys = _CATEGORY_PRIORITY_KEYS.get(category, (category,))
    for tier in ("high", "low"):
        listed = strategy_profile.get(tier) or []
        if any(k in listed for k in keys):
            return tier
    return "medium"


def _widen_target(lo: int, hi: int, priority: str) -> tuple[int, int]:
    """Adjust a (min, max) target by priority (issue #88 P4). High widens the
    ceiling ×1.5 (so a Voltron deck's Protection isn't flagged Over for doing
    exactly what it means to); Low narrows it ×0.75. Min is untouched, and a
    narrowed max never drops below min."""
    if priority == "high":
        return (lo, math.ceil(hi * 1.5))
    if priority == "low":
        return (lo, max(lo, math.floor(hi * 0.75)))
    return (lo, hi)


def evaluate_plan_coverage(cards: list[Card], strategy_profile: dict) -> dict:
    """Evaluate a card list against the profile's per-category targets.

    Returns {category: {"count", "min", "max", "status"}} where status is
    "under" | "ok" | "over". Categories without a target in the profile are
    not reported. Target ranges are widened/narrowed by the category's profile
    priority (issue #88 P4) before status is judged.
    """
    targets = strategy_profile.get("targets") or {}
    deck_colors = _profile_colors(strategy_profile)
    counts = {cat: 0 for cat in targets}
    for card in cards:
        for cat in _coverage_categories(card, deck_colors):
            if cat in counts:
                counts[cat] += 1
    report = {}
    for cat, (lo, hi) in targets.items():
        lo, hi = _widen_target(lo, hi, _category_priority(cat, strategy_profile))
        n = counts[cat]
        status = "under" if n < lo else ("over" if n > hi else "ok")
        report[cat] = {"count": n, "min": lo, "max": hi, "status": status}
    return report


# --- Deck analyzer (issue #60, P3) ----------------------------------------------
#
# Grades an EXISTING deck against its strategy profile: per-card keep / cut /
# placeholder / upgrade-target statuses with the P1 substrate's deterministic
# reasons, plus the plan-coverage report. Profiles persist per-deck
# (deck_strategy_profiles): auto-seeded on first analysis, user-editable after.

# Default status per relevance. very_low = dead role → cut; low = acceptable
# filler → placeholder; medium/high → keep. Role-less cards are upgrade
# targets (unknown contribution to the plan) — except lands, which are mana
# base, not plan slots.
_STATUS_BY_RELEVANCE = {
    "very_low": "cut",
    "low": "placeholder",
    "medium": "keep",
    "high": "keep",
}

ANALYSIS_STATUSES = ("cut", "placeholder", "upgrade_target", "keep")


@dataclass
class AnalyzedCard:
    card: Card
    quantity: int
    finish: str
    broad_role: str | None
    subtype: str | None
    relevance: str
    reason: str
    status: str


@dataclass
class UpgradeSuggestion:
    """An OWNED card outside this deck that fills an under-filled plan
    category (issue #60 P4). Informational only — moving it stays the
    existing pull-to-deck flow. Never suggests unowned cards or purchases."""

    card: Card
    inventory_row_id: int
    quantity_available: int  # copies owned outside this deck
    finish: str
    location: str
    in_other_deck: bool  # committed to another deck (shown, flagged distinctly)
    fills_need: str  # the under-filled coverage category
    broad_role: str | None
    subtype: str | None
    relevance: str
    reason: str


@dataclass
class DeckAnalysis:
    commander: Card | None
    profile: dict
    cards: list[AnalyzedCard]
    coverage: dict
    summary: dict
    upgrades: list[UpgradeSuggestion] = field(default_factory=list)
    # keyed by under-filled category name; empty list = "no owned cards match"
    upgrades_by_need: dict[str, list[UpgradeSuggestion]] = field(default_factory=dict)


def get_or_seed_profile(session: Session, deck, commander: Card) -> dict:
    """Load the deck's persisted strategy profile, seeding (and persisting,
    ``is_custom=False``) one from the commander on first use. Caller commits."""
    row = session.query(DeckStrategyProfile).filter(DeckStrategyProfile.deck_id == deck.id).first()
    if row:
        return json.loads(row.profile_data)
    profile = seed_strategy_profile(commander, commander_color_identity(commander) or set())
    session.add(
        DeckStrategyProfile(deck_id=deck.id, profile_data=json.dumps(profile), is_custom=False)
    )
    session.flush()
    return profile


def save_profile(session: Session, deck_id: int, profile_data: dict | str) -> DeckStrategyProfile:
    """Upsert the deck's strategy profile as user-edited (``is_custom=True``)."""
    payload = profile_data if isinstance(profile_data, str) else json.dumps(profile_data)
    row = session.query(DeckStrategyProfile).filter(DeckStrategyProfile.deck_id == deck_id).first()
    if row:
        row.profile_data = payload
        row.is_custom = True
        row.updated_at = utc_now()
    else:
        row = DeckStrategyProfile(deck_id=deck_id, profile_data=payload, is_custom=True)
        session.add(row)
    session.flush()
    return row


def reset_profile(session: Session, deck_id: int) -> None:
    """Delete the persisted profile; the next analysis re-seeds from the commander.

    ``synchronize_session="fetch"`` evicts the deleted row from the session's
    identity map so a same-session re-seed doesn't collide on the recycled PK.
    """
    session.query(DeckStrategyProfile).filter(DeckStrategyProfile.deck_id == deck_id).delete(
        synchronize_session="fetch"
    )


def analyze_deck(session: Session, deck, user_id: int) -> DeckAnalysis:
    """Grade an existing deck against its strategy profile.

    Uses the shared-inclusive decklist (``resolved_deck_rows``, same set the
    deck analytics read). The commander row is classified into coverage but not
    graded — it isn't a cuttable slot. First analysis auto-seeds and persists
    the profile (caller commits); a commander-less deck gets an in-memory
    generic profile and nothing is persisted.

    Note: the P1 substrate classifies by PRIMARY role only, so multi-role
    cards are under-credited in both status and coverage. Acceptable for P3.
    """
    rows = deck_service.resolved_deck_rows(session, deck, user_id)
    commander_row = next((r for r in rows if (r.role or "") == "commander" and r.card), None)
    commander = commander_row.card if commander_row else None

    if commander is not None:
        profile = get_or_seed_profile(session, deck, commander)
    else:
        # No commander to seed from: analyze against a neutral profile scoped
        # to the union of the deck's card identities (so in-identity reducers
        # aren't falsely flagged dead). Not persisted.
        colors: set[str] = set()
        for r in rows:
            if r.card and r.card.color_identity:
                colors |= set(r.card.color_identity)
        profile = {
            "color_identity": "".join(sorted(colors)) if colors else "colorless",
            "high": [],
            "medium": ["early_ramp", "ramp", "draw", "removal", "wipe", "protection", "engine"],
            "low": ["symmetrical_draw"]
            + ([] if colors else ["color_fixer", "color_specific_reducer"]),
            "targets": dict(_DEFAULT_PLAN_TARGETS),
        }

    deck_colors = _profile_colors(profile)
    analyzed: list[AnalyzedCard] = []
    coverage_cards: list[Card] = []
    total_cards = 0

    for row in rows:
        card = row.card
        if not card:
            continue
        qty = max(1, row.quantity)
        total_cards += qty
        coverage_cards.extend([card] * qty)
        if commander_row is not None and row.id == commander_row.id:
            continue  # the commander isn't a gradable slot
        broad, subtype, _confidence = classify_role_subtype(card, deck_colors)
        relevance, reason = score_role_usefulness(card, broad, subtype, deck_colors, profile)
        if broad is None and _is_land(card):
            # ponytail: role-less lands are mana base, not upgrade slots.
            status, relevance, reason = "keep", "medium", "Mana base"
        elif broad is None:
            status = "upgrade_target"
        else:
            status = _STATUS_BY_RELEVANCE[relevance]
        analyzed.append(
            AnalyzedCard(
                card=card,
                quantity=qty,
                finish=row.finish or "normal",
                broad_role=broad,
                subtype=subtype,
                relevance=relevance,
                reason=reason,
                status=status,
            )
        )

    coverage = evaluate_plan_coverage(coverage_cards, profile)

    counts = {status: 0 for status in ANALYSIS_STATUSES}
    for ac in analyzed:
        counts[ac.status] += ac.quantity
    under = [cat for cat, entry in coverage.items() if entry["status"] == "under"]
    over = [cat for cat, entry in coverage.items() if entry["status"] == "over"]
    if under and (counts["cut"] or over):
        verdict = (
            f"Role-complete but not plan-complete: {len(under)} "
            f"{'category is' if len(under) == 1 else 'categories are'} under target "
            "while dead or redundant roles fill slots"
        )
    elif under:
        verdict = f"Under-built: {len(under)} categories under target"
    else:
        verdict = "Plan-complete: every category within its target range"

    analysis = DeckAnalysis(
        commander=commander,
        profile=profile,
        cards=analyzed,
        coverage=coverage,
        summary={
            "total_cards": total_cards,
            "keeps": counts["keep"],
            "cuts": counts["cut"],
            "placeholders": counts["placeholder"],
            "upgrade_targets": counts["upgrade_target"],
            "verdict": verdict,
        },
    )
    # P4 — owned-collection upgrade suggestions for the under-filled needs.
    analysis.upgrades = suggest_upgrades(session, deck, user_id, analysis)
    analysis.upgrades_by_need = {cat: [] for cat in under}
    for suggestion in analysis.upgrades:
        analysis.upgrades_by_need[suggestion.fills_need].append(suggestion)
    return analysis


# Cap per under-filled category so the UI stays scannable.
UPGRADES_PER_NEED = 5

_RELEVANCE_SORT = {"high": 0, "medium": 1}

# issue #88 P3 thresholds.
_BULK_PRICE_FLOOR = 0.25  # below this = bulk/draft chaff
_CONSTRUCTED_AVG_PRICE = 1.00  # deck-category avg above this = constructed quality
_CMC_OVERSHOOT = 2.0  # a suggestion this far above the category's avg CMC is off-curve


def _category_quality_stats(
    analysis: DeckAnalysis, deck_colors: set[str], under: list[str], effective_price
) -> dict:
    """Per-under-category baselines drawn from the deck's OWN cards in that
    category (issue #88 P3): average effective price, average CMC, whether any
    priced card exists, and whether the deck already runs a common there. These
    scale the quality bar to the deck instead of a fixed global threshold."""
    prices = {cat: [] for cat in under}
    cmcs = {cat: [] for cat in under}
    has_common = {cat: False for cat in under}
    for ac in analysis.cards:
        cats = [c for c in _coverage_categories(ac.card, deck_colors) if c in under]
        for cat in cats:
            prices[cat].append(effective_price(ac.card, ac.finish))
            if ac.card.cmc is not None:
                cmcs[cat].append(ac.card.cmc)
            if (ac.card.rarity or "").lower() == "common":
                has_common[cat] = True

    def _avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    return {
        cat: {
            "avg_price": _avg(prices[cat]),
            "avg_cmc": _avg(cmcs[cat]) if cmcs[cat] else None,
            "has_price_data": any(p > 0 for p in prices[cat]),
            "runs_common": has_common[cat],
        }
        for cat in under
    }


def _passes_quality(card: Card, finish: str, stats: dict, effective_price) -> bool:
    """True if ``card`` clears the category's quality bar (issue #88 P3).

    Price floor (4b): drop sub-$0.25 cards only when the deck's category average
    is constructed-quality (>$1); budget categories skip it. CMC (4c): drop
    suggestions more than 2 above the category's average CMC. Rarity fallback
    (4e): when the category has no price signal at all, exclude commons unless
    the deck already runs a common in that role."""
    price = effective_price(card, finish) or 0.0

    # 4c — CMC off-curve.
    avg_cmc = stats["avg_cmc"]
    if avg_cmc is not None and card.cmc is not None and card.cmc > avg_cmc + _CMC_OVERSHOOT:
        return False

    if stats["has_price_data"]:
        # 4b — price floor, only for constructed-quality categories.
        if stats["avg_price"] > _CONSTRUCTED_AVG_PRICE and price < _BULK_PRICE_FLOOR:
            return False
    else:
        # 4e — no prices to judge by: fall back to rarity.
        if (card.rarity or "").lower() == "common" and not stats["runs_common"]:
            return False
    return True


def suggest_upgrades(session: Session, deck, user_id: int, analysis: DeckAnalysis) -> list:
    """Owned cards OUTSIDE this deck that fill the analysis's under-filled
    coverage categories (issue #60 P4).

    Scope is strictly the user's own collection: no unowned cards, no external
    sources, no purchase suggestions. Loose cards (drawers/boxes/unassigned)
    are the primary pool; cards committed to OTHER decks are still suggested
    but flagged (``in_other_deck``) since moving them is the user's call.
    Filters: commander-legal, inside the deck's color identity, not already in
    the deck (by name — any printing counts), medium/high relevance only.

    Quality filtering (issue #88 P3) runs per under-filled category, gauged
    against the deck's OWN cards in that category so the bar scales with the
    deck: a $0.10 common isn't an upgrade for a category of $5 staples, and a
    6-mana spell isn't an upgrade for a 2-mana curve. ``edhrec_rank`` isn't in
    the Card model, so that filter is skipped; price/CMC/rarity are used.
    Sorted per category: high relevance first, then price high→low (a proxy for
    quality among owned cards), then name. Capped at ``UPGRADES_PER_NEED``.
    """
    from app.inventory_service import get_location_label
    from app.pricing import effective_price

    under = [cat for cat, entry in analysis.coverage.items() if entry["status"] == "under"]
    if not under:
        return []
    profile = analysis.profile
    deck_colors = _profile_colors(profile)
    deck_names = {ac.card.name for ac in analysis.cards}
    if analysis.commander:
        deck_names.add(analysis.commander.name)

    # Per-category quality baselines from the deck's own cards in that category.
    cat_stats = _category_quality_stats(analysis, deck_colors, under, effective_price)

    rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_proxy.is_(False))
        .all()
    )

    # Aggregate per card outside this deck; prefer a loose row over one
    # committed to another deck.
    by_card: dict[int, dict] = {}
    for row in rows:
        card = row.card
        if not card or card.name in deck_names:
            continue
        loc = row.storage_location
        if loc and deck.storage_location_id and loc.id == deck.storage_location_id:
            continue  # this deck's own rows
        agg = by_card.setdefault(
            card.id, {"card": card, "qty": 0, "loose": None, "committed": None}
        )
        agg["qty"] += row.quantity
        if loc and loc.type == "deck":
            agg["committed"] = agg["committed"] or row
        else:
            agg["loose"] = agg["loose"] or row

    suggestions: list[UpgradeSuggestion] = []
    for agg in by_card.values():
        card = agg["card"]
        if not is_commander_legal(card):
            continue
        if not card_in_color_identity(card, deck_colors):
            continue
        needs = [cat for cat in _coverage_categories(card, deck_colors) if cat in under]
        if not needs:
            continue
        broad, subtype, _confidence = classify_role_subtype(card, deck_colors)
        relevance, reason = score_role_usefulness(card, broad, subtype, deck_colors, profile)
        if relevance not in ("medium", "high"):
            continue
        row = agg["loose"] or agg["committed"]
        finish = row.finish or "normal"
        in_other_deck = agg["loose"] is None
        for cat in needs:
            if not _passes_quality(card, finish, cat_stats[cat], effective_price):
                continue  # bulk/off-curve chaff for a constructed-quality slot
            suggestions.append(
                UpgradeSuggestion(
                    card=card,
                    inventory_row_id=row.id,
                    quantity_available=agg["qty"],
                    finish=finish,
                    location=get_location_label(row),
                    in_other_deck=in_other_deck,
                    fills_need=cat,
                    broad_role=broad,
                    subtype=subtype,
                    relevance=relevance,
                    reason=reason,
                )
            )

    def _sort_key(s: UpgradeSuggestion):
        # issue #88 P3 4d: relevance, then price high→low (quality proxy among
        # owned cards; edhrec_rank unavailable), then name. Role-presence stays
        # a tiebreak so role-less big drops don't jump detected-role answers.
        return (
            _RELEVANCE_SORT[s.relevance],
            0 if s.broad_role is not None else 1,
            -(effective_price(s.card, s.finish) or 0.0),
            s.card.name or "",
        )

    out: list[UpgradeSuggestion] = []
    for cat in under:
        group = sorted((s for s in suggestions if s.fills_need == cat), key=_sort_key)
        out.extend(group[:UPGRADES_PER_NEED])
    return out


def list_commander_candidates(session: Session, user_id: int) -> list[Card]:
    """Owned, Commander-legal legendary creatures the user could pick — **one row
    per unique NAME**, not per printing (#170).

    Deduping on ``card.id`` rendered a legend once per printing, and the picker
    shows only name and type line, so the extra rows were textually IDENTICAL —
    three lines reading *"Merry, Esquire of Rohan — Legendary Creature — Halfling
    Soldier"*, each linking to a different preview. Alt-art and showcase
    treatments inside ONE set produce distinct ``cards`` rows with identical text,
    which is what makes them indistinguishable rather than merely redundant.
    Measured on prod 2026-07-28: 539 rows for 502 commanders on the largest
    account (6.9% waste), three others between 1.8% and 3.8%.

    **Safe because the brew is printing-invariant.** ``generate_recommendation``
    reads the commander only for oracle-level properties — colour identity, theme
    extraction, legality — so two printings of Atraxa produce byte-identical
    output. The chosen ``card_id`` decides just two things: the #169 hover preview
    image, and which physical copy ``create_brew_from_recommendation`` references.

    **Representative printing follows #119's `resolve_add_printing` rule 2 —
    prefer an owned LOOSE, non-proxy copy** (not deck-resident), resolved to that
    copy's exact printing. Its other two rules do not apply and are deliberately
    not adapted: rule 1 keys on a deck's variant group and no deck exists yet, and
    rule 3 picks the cheapest printing in all of Magic, whereas every candidate
    here is owned by construction. A commander that is ONLY deck-resident still
    qualifies as a candidate — you may well want to brew around it again — so it
    falls back to that copy rather than dropping off the list.

    Ties break on lowest ``InventoryRow.id`` (the order ``_owned_loose_row`` uses),
    so the same account renders the same printing every time. ``card.printing_count``
    is attached for the template, which says "· N printings" rather than hiding
    the collapse silently.
    """
    rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_proxy.is_(False))
        .order_by(InventoryRow.id)
        .all()
    )
    # Which locations are decks — one query, so "is this copy loose" costs nothing
    # per row. `_owned_loose_row` asks the same question with an outer join.
    deck_location_ids = {
        loc_id
        for (loc_id,) in session.query(StorageLocation.id).filter(
            StorageLocation.user_id == user_id, StorageLocation.type == "deck"
        )
    }

    eligible: dict[int, bool] = {}
    best: dict[str, tuple[bool, int, Card]] = {}
    printings: dict[str, set[int]] = {}
    for row in rows:
        card = row.card
        if not card:
            continue
        ok = eligible.get(card.id)
        if ok is None:
            ok = can_be_commander(card) and is_commander_legal(card)
            eligible[card.id] = ok
        if not ok:
            continue

        name = card.name or ""
        printings.setdefault(name, set()).add(card.id)
        is_loose = row.storage_location_id not in deck_location_ids
        # Rank: a loose copy beats a deck-resident one; within a rank the first
        # row wins, and rows arrive in id order.
        candidate = (is_loose, row.id, card)
        current = best.get(name)
        if current is None or (is_loose and not current[0]):
            best[name] = candidate

    out = []
    for name, (_loose, _row_id, card) in best.items():
        card.printing_count = len(printings[name])
        out.append(card)
    return sorted(out, key=lambda c: c.name or "")
