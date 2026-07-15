"""Bracket Estimator V2 — V1 implementation.

DEPRECATED: This module is targeted for removal in the analytics overhaul
(see docs/analytics_overhaul.md). Do not extend or add new callers. The
replacement is a three-layer composition-signals + play-record + playgroup-
context display anchored in the user's own data rather than a single power
score. Existing callers in deck_detail_page should remain in place until
the overhaul ships.

Per the Bracket Estimator spec (Section 7), V1 covers:
  - Hard rule detection (banned cards, Game Changers, mass land denial, extra turns)
  - Auto-tagging from oracle text rules
  - Findings generation
  - Single-bracket output (mechanics-only; intent + confidence are V2)

The pipeline produces a bracket and a list of findings persisted to
deck_bracket_estimates and deck_bracket_findings. This module does NOT
replace `compute_deck_bracket` in deck_service.py — both run alongside
during the V1 validation window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import Card

RULES_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Auto-tagging rules (oracle text → primary tags)
# ---------------------------------------------------------------------------
#
# Each tuple: (tag, confidence, regex). Confidence levels:
#   certain: derived from unambiguous oracle text
#   high:    derived from common patterns with low false-positive rate
#   medium:  pattern-matched but contextual
#
# The auto-tagger emits at most one (tag, confidence) per rule per card.

_FREE_INTERACTION_NAMES = {
    "Force of Will",
    "Force of Negation",
    "Mana Drain",
    "Fierce Guardianship",
    "Deflecting Swat",
    "Flusterstorm",
    "Mental Misstep",
    "Pact of Negation",
    "Commandeer",
    "Force of Despair",
    "Force of Vigor",
    "Misdirection",
}

# Fast mana — produces 2+ mana for ≤ 1 mana investment, OR is a 0-mana mana rock
_FAST_MANA_NAMES = {
    "Sol Ring",
    "Mana Crypt",
    "Mox Diamond",
    "Chrome Mox",
    "Mox Opal",
    "Jeweled Lotus",
    "Grim Monolith",
    "Mana Vault",
    "Lotus Petal",
    "Ancient Tomb",
}

# "Take an extra turn" detection — covers Time Warp, Temporal Manipulation, etc.
# The regex avoids "extra combat phase" which is a different (lesser) effect.
_EXTRA_TURN_RE = re.compile(r"\btakes? an extra turn\b", re.IGNORECASE)

# Mass land denial — destroys/exiles 4+ lands across all players. The regex
# catches Armageddon-style, Catastrophe-style, and "destroy each land" phrasings.
_MASS_LAND_DENIAL_RE = re.compile(
    r"destroy all (?:non-?\w+ )?lands?\b"
    r"|exile all (?:non-?\w+ )?lands?\b"
    r"|destroy each (?:non-?\w+ )?land\b"
    r"|each player sacrifices (?:two|three|four|five|six|all) lands",
    re.IGNORECASE,
)

# Stax — limits opponents' actions or resources
_STAX_NAMES = {
    "Winter Orb",
    "Static Orb",
    "Stasis",
    "Smokestack",
    "Tangle Wire",
    "Sphere of Resistance",
    "Thalia, Guardian of Thraben",
    "Thorn of Amethyst",
    "Trinisphere",
    "Blood Moon",
    "Magus of the Moon",
    "Null Rod",
    "Stony Silence",
    "Cursed Totem",
    "Drannith Magistrate",
    "Opposition Agent",
    "Aven Mindcensor",
    "Linvala, Keeper of Silence",
}

# Unconditional tutor — searches your library for ANY card
_UNCONDITIONAL_TUTOR_NAMES = {
    "Demonic Tutor",
    "Vampiric Tutor",
    "Imperial Seal",
    "Diabolic Tutor",
    "Grim Tutor",
    "Beseech the Mirror",
    "Wishclaw Talisman",
}

# Restricted tutor — searches for a specific card type
_RESTRICTED_TUTOR_RE = re.compile(
    r"search your library for an? (?:\w+ ){0,3}"
    r"(?:creature|artifact|enchantment|instant|sorcery|planeswalker|battle) card",
    re.IGNORECASE,
)


@dataclass
class AutoTag:
    tag: str
    confidence: str  # 'certain' | 'high' | 'medium' | 'low'


def tag_card_from_oracle(card: Card) -> list[AutoTag]:
    """Return primary-tag suggestions for a single Card based on oracle text + name."""
    name = card.name or ""
    oracle = (card.oracle_text or "").lower()
    type_line = (card.type_line or "").lower()

    if "basic land" in type_line:
        return []

    tags: list[AutoTag] = []

    # Fast mana detection is curated-list only — regex-based detection had too many
    # false positives (bounce lands like Gruul Turf, ETB-tapped duals). Add cards to
    # the seed migration or directly to game_changer_cards instead.
    if name in _FAST_MANA_NAMES:
        tags.append(AutoTag("fast_mana", "high"))

    if name in _FREE_INTERACTION_NAMES:
        tags.append(AutoTag("free_interaction", "high"))
    elif (
        "you may cast" in oracle
        and "without paying" in oracle
        and ("counter target" in oracle or "exile target" in oracle or "destroy target" in oracle)
    ):
        tags.append(AutoTag("free_interaction", "medium"))

    if name in _UNCONDITIONAL_TUTOR_NAMES:
        tags.append(AutoTag("unconditional_tutor", "high"))
    elif (
        "search your library for a card" in oracle
        and "land" not in oracle.split("search your library for a card")[0][-30:]
    ):
        tags.append(AutoTag("unconditional_tutor", "medium"))

    if _RESTRICTED_TUTOR_RE.search(oracle):
        tags.append(AutoTag("restricted_tutor", "medium"))

    if _MASS_LAND_DENIAL_RE.search(oracle):
        tags.append(AutoTag("mass_land_denial", "certain"))

    if _EXTRA_TURN_RE.search(oracle):
        tags.append(AutoTag("extra_turn", "certain"))

    if name in _STAX_NAMES:
        tags.append(AutoTag("stax", "high"))

    return tags


def upsert_card_tags(session: Session, card_id: int, tags: list[AutoTag]) -> None:
    """Insert/update card_tags rows for a single card."""
    for t in tags:
        session.execute(
            text(
                """
                INSERT INTO card_tags (card_id, tag, confidence, source, last_reviewed)
                VALUES (:card_id, :tag, :confidence, 'oracle_text_rule', CURRENT_TIMESTAMP)
                ON CONFLICT (card_id, tag) DO UPDATE SET
                  confidence = excluded.confidence,
                  source = excluded.source,
                  last_reviewed = CURRENT_TIMESTAMP
                """
            ),
            {"card_id": card_id, "tag": t.tag, "confidence": t.confidence},
        )


# ---------------------------------------------------------------------------
# Bracket V1 estimation pipeline
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    finding_type: str
    finding_value: str | None
    severity: str  # 'info' | 'warning' | 'critical'
    message: str
    contributes_to_bracket: int | None = None
    weight: float = 1.0


@dataclass
class BracketEstimate:
    mechanics_bracket: int
    final_bracket: int
    findings: list[Finding] = field(default_factory=list)
    rules_version: str = RULES_VERSION
    score: float | None = None
    intent_bracket: int | None = None
    confidence_tagging_coverage: float | None = None
    confidence_mechanics_clarity: float | None = None
    confidence_intent_alignment: float | None = None
    confidence_combo_detection_depth: float | None = None
    floor_bracket: int | None = None


def _load_rules(session: Session) -> dict[int, dict]:
    rows = session.execute(
        text(
            "SELECT bracket, name, max_game_changers, allows_mass_land_denial, "
            "allows_extra_turn_chains, allows_two_card_combos, allows_combo_as_primary, "
            "competitive FROM commander_bracket_rules WHERE rules_version = :v ORDER BY bracket"
        ),
        {"v": RULES_VERSION},
    ).fetchall()
    return {
        r[0]: {
            "name": r[1],
            "max_game_changers": r[2],
            "allows_mass_land_denial": bool(r[3]),
            "allows_extra_turn_chains": bool(r[4]),
            "allows_two_card_combos": bool(r[5]),
            "allows_combo_as_primary": bool(r[6]),
            "competitive": bool(r[7]),
        }
        for r in rows
    }


def _gather_deck_signals(session: Session, deck_storage_location_id: int, user_id: int) -> dict:
    """Pull signal counts for a deck via card_tags + game_changer_cards joins."""
    rows = session.execute(
        text(
            """
            SELECT c.id, c.name, c.oracle_text, c.type_line
            FROM inventory_rows ir
            JOIN cards c ON ir.card_id = c.id
            WHERE ir.user_id = :uid AND ir.storage_location_id = :loc
            """
        ),
        {"uid": user_id, "loc": deck_storage_location_id},
    ).fetchall()

    card_ids = [r[0] for r in rows]
    if not card_ids:
        return {
            "total_cards": 0,
            "card_ids": [],
            "tag_counts": {},
            "tagged_cards": {},
            "game_changers": [],
            "extra_turns": [],
            "mass_land_denial": [],
        }

    placeholders = ",".join(f":id{i}" for i in range(len(card_ids)))
    params = {f"id{i}": cid for i, cid in enumerate(card_ids)}

    tag_rows = session.execute(
        text(f"SELECT card_id, tag FROM card_tags WHERE card_id IN ({placeholders})"),
        params,
    ).fetchall()
    tagged_cards: dict[str, list[str]] = {}
    name_by_id = {r[0]: r[1] for r in rows}
    for card_id, tag in tag_rows:
        tagged_cards.setdefault(tag, []).append(name_by_id[card_id])

    gc_rows = session.execute(
        text(
            "SELECT card_name FROM game_changer_cards "
            "WHERE active AND rules_version = :v AND card_name IN ("
            + ",".join(f":n{i}" for i in range(len(rows)))
            + ")"
        ),
        {"v": RULES_VERSION, **{f"n{i}": r[1] for i, r in enumerate(rows)}},
    ).fetchall()
    game_changers = sorted({r[0] for r in gc_rows})

    return {
        "total_cards": len(rows),
        "card_ids": card_ids,
        "tagged_cards": tagged_cards,
        "tag_counts": {tag: len(names) for tag, names in tagged_cards.items()},
        "game_changers": game_changers,
        "extra_turns": tagged_cards.get("extra_turn", []),
        "mass_land_denial": tagged_cards.get("mass_land_denial", []),
    }


def estimate_bracket_v1(
    session: Session, deck_storage_location_id: int, user_id: int
) -> BracketEstimate:
    """V1 Mechanics-only bracket estimation. Hard rules + findings."""
    rules = _load_rules(session)
    signals = _gather_deck_signals(session, deck_storage_location_id, user_id)

    findings: list[Finding] = []
    bracket = 1

    gc_count = len(signals["game_changers"])
    fast_mana_count = signals["tag_counts"].get("fast_mana", 0)
    free_interaction_count = signals["tag_counts"].get("free_interaction", 0)
    tutor_count = signals["tag_counts"].get("unconditional_tutor", 0) + signals["tag_counts"].get(
        "restricted_tutor", 0
    )
    stax_count = signals["tag_counts"].get("stax", 0)
    mld_count = len(signals["mass_land_denial"])
    extra_turn_count = len(signals["extra_turns"])

    # ---------- Hard floors ----------
    # Per spec Section 3 Step 2: only Game Changer count, mass land denial, extra
    # turn chains, banned cards, and combo-as-primary push the floor. fast_mana,
    # free_interaction, tutors, and stax are SOFT signals — they generate findings
    # but don't auto-push the bracket. Score (V2) reflects them.

    if mld_count > 0:
        bracket = max(bracket, 4)
        findings.append(
            Finding(
                finding_type="mass_land_denial_detected",
                finding_value=", ".join(signals["mass_land_denial"][:3]),
                severity="critical",
                message=(
                    f"Mass land denial ({mld_count}): "
                    + ", ".join(signals["mass_land_denial"])
                    + ". Pushes deck to Bracket 4+."
                ),
                contributes_to_bracket=4,
                weight=5.0,
            )
        )

    if extra_turn_count >= 3:
        bracket = max(bracket, 4)
        findings.append(
            Finding(
                finding_type="extra_turn_chain_detected",
                finding_value=str(extra_turn_count),
                severity="critical",
                message=f"{extra_turn_count} extra-turn cards form a chain. Bracket 4+.",
                contributes_to_bracket=4,
                weight=5.0,
            )
        )
    elif extra_turn_count > 0:
        bracket = max(bracket, 3)
        findings.append(
            Finding(
                finding_type="extra_turn_detected",
                finding_value=str(extra_turn_count),
                severity="warning",
                message=f"{extra_turn_count} extra-turn card{'s' if extra_turn_count != 1 else ''} present (no chain yet).",
                contributes_to_bracket=3,
                weight=2.0,
            )
        )

    if gc_count > 0:
        # Push to the smallest tier whose max_game_changers >= count.
        for b in sorted(rules.keys()):
            if rules[b]["max_game_changers"] >= gc_count:
                bracket = max(bracket, b)
                break
        else:
            bracket = max(bracket, 5)
        findings.append(
            Finding(
                finding_type="game_changer_detected",
                finding_value=", ".join(signals["game_changers"][:5]),
                severity="warning" if gc_count <= 3 else "critical",
                # #121 — the message is the violation chip's "what do I cut"
                # list, so it names every GC card (message is Text; the capped
                # finding_value stays as the compact form).
                message=(
                    f"{gc_count} Game Changer{'s' if gc_count != 1 else ''}: "
                    + ", ".join(signals["game_changers"])
                ),
                contributes_to_bracket=bracket,
                weight=3.0 + gc_count,
            )
        )

    # ---------- Soft signals (informational, not bracket-pushing) ----------

    if fast_mana_count > 0:
        findings.append(
            Finding(
                finding_type="fast_mana_density",
                finding_value=str(fast_mana_count),
                severity="info" if fast_mana_count <= 1 else "warning",
                message=(
                    f"{fast_mana_count} fast-mana piece{'s' if fast_mana_count != 1 else ''}: "
                    + ", ".join(signals["tagged_cards"].get("fast_mana", [])[:5])
                ),
                contributes_to_bracket=None,
                weight=3.0,
            )
        )

    if free_interaction_count > 0:
        findings.append(
            Finding(
                finding_type="free_interaction_density",
                finding_value=str(free_interaction_count),
                severity="info" if free_interaction_count <= 2 else "warning",
                message=(
                    f"{free_interaction_count} free-interaction piece"
                    f"{'s' if free_interaction_count != 1 else ''}: "
                    + ", ".join(signals["tagged_cards"].get("free_interaction", [])[:5])
                ),
                contributes_to_bracket=None,
                weight=3.0,
            )
        )

    if tutor_count >= 5:
        findings.append(
            Finding(
                finding_type="high_tutor_density",
                finding_value=str(tutor_count),
                severity="warning",
                message=f"{tutor_count} tutors — high end of Bracket 3 range.",
                contributes_to_bracket=None,
                weight=2.0 + tutor_count * 0.2,
            )
        )
    elif tutor_count > 0:
        findings.append(
            Finding(
                finding_type="tutor_density",
                finding_value=str(tutor_count),
                severity="info",
                message=f"{tutor_count} tutor{'s' if tutor_count != 1 else ''}.",
                contributes_to_bracket=None,
                weight=1.0,
            )
        )

    if stax_count > 0:
        findings.append(
            Finding(
                finding_type="stax_pieces_detected",
                finding_value=str(stax_count),
                severity="warning",
                message=(
                    f"{stax_count} stax piece{'s' if stax_count != 1 else ''}: "
                    + ", ".join(signals["tagged_cards"].get("stax", [])[:5])
                ),
                contributes_to_bracket=None,
                weight=2.0,
            )
        )

    has_bracket_pushing_finding = any(f.contributes_to_bracket for f in findings)
    if not has_bracket_pushing_finding:
        findings.append(
            Finding(
                finding_type="no_high_power_signals",
                finding_value=None,
                severity="info",
                message="No mass land denial, extra-turn chains, or Game Changers detected.",
                contributes_to_bracket=2,
                weight=1.0,
            )
        )

    return BracketEstimate(
        mechanics_bracket=bracket,
        final_bracket=bracket,
        findings=findings,
        rules_version=RULES_VERSION,
    )


# ---------------------------------------------------------------------------
# V2: intent survey + multi-dimensional confidence
# ---------------------------------------------------------------------------

# Per Section 3 Step 5. Each intent answer maps to a bracket-like integer 1-5;
# the deck's intent_bracket = round(mean of non-null answers), with overrides.
_INTENT_POD_BRACKET = {
    "precon": 1,
    "casual": 2,
    "upgraded": 3,
    "optimized": 4,
    "cedh": 5,
}
_INTENT_SPEED_BRACKET = {"journey": 2, "eventually": 3, "quickly": 4}
_INTENT_COMBO_BRACKET = {"no": 2, "backup": 3, "plan": 4}
_INTENT_WINNING_BRACKET = {"wild": 2, "balanced": 3, "consistent": 4}
_INTENT_PLAYED_BRACKET = {"fine": 2, "mixed": 3, "groaned": 4}


def derive_intent_bracket(deck) -> int | None:
    """Translate the 5 intent answers on a Deck into a single bracket 1-5.

    Returns None if every intent_* field is null (user skipped survey).
    Hard overrides:
      - intent_pod = 'cedh'        -> 5
      - intent_played = 'groaned'  -> max(result, 4)
    """
    answers = []
    pod = deck.intent_pod
    if pod and pod in _INTENT_POD_BRACKET:
        if pod == "cedh":
            return 5
        answers.append(_INTENT_POD_BRACKET[pod])
    if deck.intent_speed in _INTENT_SPEED_BRACKET:
        answers.append(_INTENT_SPEED_BRACKET[deck.intent_speed])
    if deck.intent_combo in _INTENT_COMBO_BRACKET:
        answers.append(_INTENT_COMBO_BRACKET[deck.intent_combo])
    if deck.intent_winning in _INTENT_WINNING_BRACKET:
        answers.append(_INTENT_WINNING_BRACKET[deck.intent_winning])
    if deck.intent_played in _INTENT_PLAYED_BRACKET:
        answers.append(_INTENT_PLAYED_BRACKET[deck.intent_played])

    if not answers:
        return None

    avg = sum(answers) / len(answers)
    bracket = max(1, min(5, round(avg)))
    if deck.intent_played == "groaned":
        bracket = max(bracket, 4)
    return bracket


def resolve_mechanics_intent(
    mechanics_bracket: int, intent_bracket: int | None
) -> tuple[int, float | None, list[Finding]]:
    """Section 3 Step 6: pick final_bracket and produce alignment confidence.

    Returns (final_bracket, intent_alignment, extra_findings).
    """
    if intent_bracket is None:
        return mechanics_bracket, None, []

    diff = mechanics_bracket - intent_bracket
    if diff == 0:
        return mechanics_bracket, 1.0, []
    if abs(diff) == 1:
        return (
            mechanics_bracket,
            0.7,
            [
                Finding(
                    finding_type="intent_off_by_one",
                    finding_value=f"mech={mechanics_bracket}/intent={intent_bracket}",
                    severity="info",
                    message=(
                        f"Intent says Bracket {intent_bracket}; mechanics show "
                        f"Bracket {mechanics_bracket}. Close — pod expectations should match."
                    ),
                    contributes_to_bracket=None,
                    weight=1.0,
                )
            ],
        )
    if diff >= 2:
        return (
            mechanics_bracket,
            0.3,
            [
                Finding(
                    finding_type="pod_mismatch_warning",
                    finding_value=f"mech={mechanics_bracket}/intent={intent_bracket}",
                    severity="critical",
                    message=(
                        f"This deck plays as Bracket {mechanics_bracket} mechanically but "
                        f"you've indicated Bracket {intent_bracket} intent. The deck may feel "
                        f"oppressive in the intended pod."
                    ),
                    contributes_to_bracket=mechanics_bracket,
                    weight=5.0,
                )
            ],
        )
    return (
        intent_bracket,
        0.3,
        [
            Finding(
                finding_type="intent_above_mechanics",
                finding_value=f"mech={mechanics_bracket}/intent={intent_bracket}",
                severity="info",
                message=(
                    f"You play this as Bracket {intent_bracket} though the cards only "
                    f"signal Bracket {mechanics_bracket}. That's fine — pod expectations "
                    f"matter."
                ),
                contributes_to_bracket=intent_bracket,
                weight=1.0,
            )
        ],
    )


def _compute_tagging_coverage(
    session: Session, deck_storage_location_id: int, user_id: int
) -> float:
    """% of non-basic deck cards that have at least one confident card_tags row."""
    rows = session.execute(
        text(
            """
            SELECT c.id, c.type_line, EXISTS(
                SELECT 1 FROM card_tags ct
                WHERE ct.card_id = c.id AND ct.confidence IN ('certain', 'high', 'medium')
            ) AS tagged
            FROM inventory_rows ir
            JOIN cards c ON ir.card_id = c.id
            WHERE ir.user_id = :uid AND ir.storage_location_id = :loc
            """
        ),
        {"uid": user_id, "loc": deck_storage_location_id},
    ).fetchall()

    relevant = [r for r in rows if "basic land" not in (r[1] or "").lower()]
    if not relevant:
        return 1.0
    tagged = sum(1 for r in relevant if r[2])
    return round(tagged / len(relevant), 3)


# ---------------------------------------------------------------------------
# Soft power score (Section 3 Step 3) — informational, never bracket-pushing
# ---------------------------------------------------------------------------


def compute_soft_score(
    session: Session,
    deck_storage_location_id: int,
    user_id: int,
    combo_role: str,
    pip_strain: dict | None = None,
) -> int:
    """Aggregate 0-100 power score per Section 3 Step 3.

    Does NOT influence the bracket — strictly informational. Lets users see
    why a deck plays harder than its mechanical bracket suggests.
    """
    sig = _gather_deck_signals(session, deck_storage_location_id, user_id)

    fast_mana_n = sig["tag_counts"].get("fast_mana", 0)
    free_int_n = sig["tag_counts"].get("free_interaction", 0)
    uncond_tutor_n = sig["tag_counts"].get("unconditional_tutor", 0)
    restr_tutor_n = sig["tag_counts"].get("restricted_tutor", 0)
    stax_n = sig["tag_counts"].get("stax", 0)

    # Spec weights — see Section 3 Step 3
    score = 0
    score += min(20, fast_mana_n * 5)
    score += min(20, uncond_tutor_n * 5 + restr_tutor_n * 2)
    score += min(15, free_int_n * 5)
    score += min(5, stax_n * 2)

    combo_points = {
        "none": 0,
        "incidental": 3,
        "backup": 8,
        "primary": 12,
        "compact": 15,
    }
    score += combo_points.get(combo_role, 0)

    # Card-draw efficiency: count InventoryRow.tags including "Draw" or "Engine"
    draw_engine_count = (
        session.execute(
            text(
                """
            SELECT COUNT(*) FROM inventory_rows
            WHERE user_id = :uid AND storage_location_id = :loc
              AND tags IS NOT NULL AND (tags LIKE '%Draw%' OR tags LIKE '%Engine%')
            """
            ),
            {"uid": user_id, "loc": deck_storage_location_id},
        ).scalar()
        or 0
    )
    score += min(15, draw_engine_count)

    # Mana base quality — fraction of non-basic lands as a proxy. 0-10.
    land_rows = session.execute(
        text(
            """
            SELECT c.type_line FROM inventory_rows ir
            JOIN cards c ON ir.card_id = c.id
            WHERE ir.user_id = :uid AND ir.storage_location_id = :loc
              AND c.type_line LIKE '%Land%'
            """
        ),
        {"uid": user_id, "loc": deck_storage_location_id},
    ).fetchall()
    if land_rows:
        non_basic = sum(1 for r in land_rows if "basic land" not in (r[0] or "").lower())
        score += round((non_basic / len(land_rows)) * 10)

    # Pip strain penalty — strained colors drag the score
    if pip_strain:
        strained = sum(1 for v in pip_strain.values() if v.get("strained"))
        score -= min(5, strained * 2)

    return max(0, min(100, score))


def derive_combo_role(
    combos: dict | None, tutor_count: int, commander_names: set[str]
) -> tuple[str, list[Finding]]:
    """Section 3 Step 4: classify the deck's combo role.

    Returns (role, findings). Role values:
      none        — no Spellbook combos in deck
      incidental  — 1-2 combos, few tutors, no bracket pressure
      backup      — 1-2 combos, decent tutor support (bracket >= 3)
      primary     — 3+ combos OR 1 combo with strong tutor support (bracket >= 4)
      compact     — commander is part of a combo + 4+ tutors (bracket = 5)
    """
    if combos is None or not combos.get("included"):
        return "none", []

    included = combos.get("included", [])
    combo_count = len(included)
    findings: list[Finding] = []

    commander_in_combo = any(
        any(name in commander_names for name in c.get("card_names", [])) for c in included
    )

    if commander_in_combo and tutor_count >= 4:
        findings.append(
            Finding(
                finding_type="combo_compact_detected",
                finding_value=str(combo_count),
                severity="critical",
                message=(
                    f"Commander is part of a complete combo with {tutor_count} tutors "
                    "to assemble. This is a compact combo deck — Bracket 5."
                ),
                contributes_to_bracket=5,
                weight=10.0,
            )
        )
        return "compact", findings

    if combo_count >= 3 or (combo_count >= 1 and tutor_count >= 4):
        findings.append(
            Finding(
                finding_type="combo_primary_detected",
                finding_value=str(combo_count),
                severity="critical",
                message=(
                    f"{combo_count} complete combo line{'s' if combo_count != 1 else ''} "
                    f"with {tutor_count} tutors — combo is the primary win condition. Bracket 4+."
                ),
                contributes_to_bracket=4,
                weight=8.0,
            )
        )
        return "primary", findings

    if combo_count >= 1 and tutor_count >= 2:
        sample_names = [", ".join(c.get("card_names", [])[:3]) for c in included[:2]]
        findings.append(
            Finding(
                finding_type="combo_backup_detected",
                finding_value=str(combo_count),
                severity="warning",
                message=(
                    f"{combo_count} complete combo line{'s' if combo_count != 1 else ''} "
                    f"({'; '.join(sample_names)}) with {tutor_count} tutors. "
                    "Backup combo line — Bracket 3+."
                ),
                contributes_to_bracket=3,
                weight=4.0,
            )
        )
        return "backup", findings

    findings.append(
        Finding(
            finding_type="combo_incidental_detected",
            finding_value=str(combo_count),
            severity="info",
            message=(
                f"{combo_count} complete combo line{'s' if combo_count != 1 else ''} "
                "but few tutors — combo presence appears incidental, not the plan."
            ),
            contributes_to_bracket=None,
            weight=1.0,
        )
    )
    return "incidental", findings


# ---------------------------------------------------------------------------
# #121: bracket floor — declared vs computed minimum
# ---------------------------------------------------------------------------
# The bracket is what the owner DECLARES (decks.declared_bracket); the deck's
# contents impose a minimum on what may be declared. The floor is a pure
# function over HARD findings only — Game Changer count, mass land denial,
# two-card combos. Advisory findings (tutor / fast-mana / free-interaction
# density, extra turns, combo-role inference) NEVER fold into the floor.
# Every floor derivation cites exact cards. Floor 1 does not exist
# computationally (nothing decklist-decidable separates B1 from B2).

# Finding types whose presence drives the floor — the evidence panel shows
# these as the VERIFIED derivation, everything else as advisory.
FLOOR_FINDING_TYPES = frozenset(
    {"game_changer_detected", "mass_land_denial_detected", "two_card_combo_detected"}
)

# Shared confidence vocabulary (program spec §16) — all #121 surfaces use these.
CONFIDENCE_VERIFIED = "VERIFIED"
CONFIDENCE_ADVISORY = "ADVISORY"

# Combo-earliness rule of record (#121, documented per the release notes
# requirement): Commander Spellbook's find-my-combos DOES expose its own
# bracket annotation (bracketTag: R=Ruthless, S=Spicy, P=Powerful, O=Oddball,
# C=Core, E=Exhibition, B=Banned — verified against their OpenAPI schema
# 2026-07-14), so when the persisted payload carries it, early = tag 'R'.
# Payloads persisted before the tag was carried fall back to the stated
# deterministic proxy: combined mana value of the two pieces <= 6 -> early.
EARLY_COMBO_TAGS = frozenset({"R"})
EARLY_COMBO_MV_PROXY = 6.0


def _combo_is_early(session: Session, combo: dict) -> tuple[bool, str]:
    """(early?, human-readable why) for one two-card combo."""
    tag = combo.get("bracket_tag")
    if tag:
        early = tag in EARLY_COMBO_TAGS
        return early, f"Spellbook bracket tag '{tag}'"
    mv = combo.get("mana_value_needed")
    if mv is None:
        names = combo.get("card_names", [])
        rows = session.execute(
            text("SELECT name, MIN(cmc) FROM cards WHERE name IN :names GROUP BY name").bindparams(
                bindparam("names", expanding=True)
            ),
            {"names": names},
        ).fetchall()
        if len(rows) != len(names):
            return False, "mana values unavailable — not treated as early"
        mv = sum(r[1] or 0 for r in rows)
    early = float(mv) <= EARLY_COMBO_MV_PROXY
    return early, f"combined mana value {float(mv):g} (proxy: <= {EARLY_COMBO_MV_PROXY:g} = early)"


def compute_bracket_floor(
    session: Session, deck, user_id: int, combos: dict | None
) -> tuple[int, list[Finding]]:
    """#121 — the minimum bracket the deck's contents impose on a declaration.

    Pure function of the deck list + persisted combos:
      - Game Changers: count >= 4 -> floor 4; 1-3 -> floor 3
      - mass land denial present -> floor 4
      - two-card combo -> floor 3; EARLY two-card combo -> floor 4
      - otherwise floor 2

    Returns (floor, two_card_combo findings). GC / MLD evidence is already
    emitted by estimate_bracket_v1 in the same estimate (game_changer_detected
    / mass_land_denial_detected cite the exact cards); only the two-card-combo
    findings are new here.
    """
    signals = _gather_deck_signals(session, deck.storage_location_id, user_id)
    floor = 2
    findings: list[Finding] = []

    gc_count = len(signals["game_changers"])
    if gc_count >= 4:
        floor = max(floor, 4)
    elif gc_count >= 1:
        floor = max(floor, 3)

    if signals["mass_land_denial"]:
        floor = max(floor, 4)

    for combo in (combos or {}).get("included", []):
        names = combo.get("card_names", [])
        if len(names) != 2:
            continue
        early, why = _combo_is_early(session, combo)
        contributes = 4 if early else 3
        floor = max(floor, contributes)
        findings.append(
            Finding(
                finding_type="two_card_combo_detected",
                finding_value=", ".join(names)[:255],
                severity="critical" if early else "warning",
                message=(
                    f"Two-card combo: {names[0]} + {names[1]} — "
                    f"{'early' if early else 'not early'} ({why}). "
                    f"Floor {contributes}."
                ),
                contributes_to_bracket=contributes,
                weight=6.0,
            )
        )

    return floor, findings


def estimate_bracket_v2(
    session: Session, deck, user_id: int, combos: dict | None = None
) -> BracketEstimate:
    """V2/V3 estimator: V1 mechanics + intent + confidence + (optional) combo role.

    Takes a full Deck object so it can read the intent_* survey columns and
    the deck.id for downstream persistence. When `combos` is provided (output
    of compute_deck_combos), V3 combo role is layered on top.
    """
    base = estimate_bracket_v1(session, deck.storage_location_id, user_id)
    mechanics_bracket = base.mechanics_bracket
    findings = list(base.findings)
    combo_role = "none"

    # V3: combo role analysis
    if combos is not None:
        commander_rows = session.execute(
            text(
                "SELECT c.name FROM inventory_rows ir JOIN cards c ON ir.card_id = c.id "
                "WHERE ir.user_id = :uid AND ir.storage_location_id = :loc "
                "AND ir.role = 'commander'"
            ),
            {"uid": user_id, "loc": deck.storage_location_id},
        ).fetchall()
        commander_names = {r[0] for r in commander_rows}
        tutor_rows = session.execute(
            text(
                "SELECT COUNT(DISTINCT c.id) FROM inventory_rows ir "
                "JOIN cards c ON ir.card_id = c.id "
                "JOIN card_tags ct ON ct.card_id = c.id "
                "WHERE ir.user_id = :uid AND ir.storage_location_id = :loc "
                "AND ct.tag IN ('unconditional_tutor', 'restricted_tutor')"
            ),
            {"uid": user_id, "loc": deck.storage_location_id},
        ).first()
        tutor_count = tutor_rows[0] if tutor_rows else 0

        combo_role, combo_findings = derive_combo_role(combos, tutor_count, commander_names)
        findings += combo_findings
        for cf in combo_findings:
            if cf.contributes_to_bracket:
                mechanics_bracket = max(mechanics_bracket, cf.contributes_to_bracket)

    # #121 — the floor is computed alongside and persisted with the estimate;
    # its two-card-combo findings join the evidence table.
    floor_bracket, floor_findings = compute_bracket_floor(session, deck, user_id, combos)
    findings += floor_findings

    intent_bracket = derive_intent_bracket(deck)
    final_bracket, intent_alignment, extra_findings = resolve_mechanics_intent(
        mechanics_bracket, intent_bracket
    )

    findings += extra_findings

    tagging_coverage = _compute_tagging_coverage(session, deck.storage_location_id, user_id)
    # The spec's 85% threshold assumes community-curated tags for every card. Our
    # V2 auto-tagger only fires on bracket-relevant cards, so casual decks
    # legitimately show low coverage. We display the coverage in the UI but don't
    # raise a finding until V3+ adds curated tags.

    # mechanics_clarity: every V1 hard-rule finding fires from an unambiguous
    # rule, so clarity is 1.0. Future ambiguous-rule findings will drop this.
    mechanics_clarity = 1.0
    combo_detection_depth = 1.0 if combos is not None else None

    # Soft power score — Section 3 Step 3. Informational, never bracket-pushing.
    # Pip strain is pulled from compute_deck_health; lazy import to avoid cycles.
    try:
        from sqlalchemy.orm import joinedload

        from app.deck_service import compute_deck_health
        from app.models import InventoryRow

        _rows = (
            session.query(InventoryRow)
            .options(joinedload(InventoryRow.card))
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.storage_location_id == deck.storage_location_id,
            )
            .all()
        )
        _health = compute_deck_health(_rows) if _rows else None
        pip_strain = _health.get("pip_strain") if _health else None
    except Exception:
        pip_strain = None
    score = compute_soft_score(session, deck.storage_location_id, user_id, combo_role, pip_strain)

    return BracketEstimate(
        mechanics_bracket=mechanics_bracket,
        final_bracket=final_bracket,
        findings=findings,
        rules_version=base.rules_version,
        score=score,
        intent_bracket=intent_bracket,
        confidence_tagging_coverage=tagging_coverage,
        confidence_mechanics_clarity=mechanics_clarity,
        confidence_intent_alignment=intent_alignment,
        confidence_combo_detection_depth=combo_detection_depth,
        floor_bracket=floor_bracket,
    )


def persist_estimate(session: Session, deck_id: int, estimate: BracketEstimate) -> int:
    """Replace any existing estimate for this deck and write the findings."""
    session.execute(
        text(
            "DELETE FROM deck_bracket_findings WHERE deck_id = :d "
            "AND estimate_id IN (SELECT id FROM deck_bracket_estimates WHERE deck_id = :d)"
        ),
        {"d": deck_id},
    )
    session.execute(
        text("DELETE FROM deck_bracket_estimates WHERE deck_id = :d"),
        {"d": deck_id},
    )
    result = session.execute(
        text(
            """
            INSERT INTO deck_bracket_estimates (
                deck_id, estimated_bracket, mechanics_bracket, intent_bracket,
                final_bracket, floor_bracket, score, rules_version,
                confidence_tagging_coverage, confidence_mechanics_clarity,
                confidence_intent_alignment, confidence_combo_detection_depth
            ) VALUES (
                :d, :bracket, :mech, :intent, :final, :floor, :score, :v,
                :ctc, :cmc, :cia, :ccd
            )
            RETURNING id
            """
        ),
        {
            "d": deck_id,
            "bracket": estimate.final_bracket,
            "mech": estimate.mechanics_bracket,
            "intent": estimate.intent_bracket,
            "final": estimate.final_bracket,
            "floor": estimate.floor_bracket,
            "score": estimate.score,
            "v": estimate.rules_version,
            "ctc": estimate.confidence_tagging_coverage,
            "cmc": estimate.confidence_mechanics_clarity,
            "cia": estimate.confidence_intent_alignment,
            "ccd": estimate.confidence_combo_detection_depth,
        },
    )
    # ``RETURNING id`` (SQLite >= 3.35, and Postgres at v4) is dialect-safe and
    # replaces the SQLite-only ``cursor.lastrowid``, which psycopg does not
    # populate. Single-row insert -> exactly one returned row.
    estimate_id = result.scalar_one()
    for f in estimate.findings:
        session.execute(
            text(
                """
                INSERT INTO deck_bracket_findings (
                    deck_id, estimate_id, finding_type, finding_value,
                    severity, message, contributes_to_bracket, weight
                ) VALUES (
                    :d, :e, :ft, :fv, :sev, :msg, :ctb, :w
                )
                """
            ),
            {
                "d": deck_id,
                "e": estimate_id,
                "ft": f.finding_type,
                "fv": f.finding_value,
                "sev": f.severity,
                "msg": f.message,
                "ctb": f.contributes_to_bracket,
                "w": f.weight,
            },
        )
    session.commit()
    return estimate_id


def load_persisted_estimate(session: Session, deck_id: int) -> dict | None:
    """Read the latest persisted estimate for a deck as a template-ready dict.

    Read-only (no compute, no write) — the #82 bracket page uses this on GET so
    the request path never triggers the estimator. Returns None when no estimate
    has been persisted yet (the page shows its empty state). Shape matches the
    dormant `bracket_v2` panel context: `.bracket`, `.confidence.*`, `.findings`.
    persist_estimate keeps only one row per deck, but order defensively anyway.
    """
    est = session.execute(
        text(
            "SELECT id, final_bracket, mechanics_bracket, intent_bracket, score, "
            "rules_version, generated_at, confidence_tagging_coverage, "
            "confidence_mechanics_clarity, confidence_intent_alignment, "
            "confidence_combo_detection_depth, floor_bracket "
            "FROM deck_bracket_estimates WHERE deck_id = :d "
            "ORDER BY generated_at DESC, id DESC LIMIT 1"
        ),
        {"d": deck_id},
    ).first()
    if est is None:
        return None
    findings = session.execute(
        text(
            "SELECT finding_type, finding_value, severity, message "
            "FROM deck_bracket_findings WHERE estimate_id = :e ORDER BY id"
        ),
        {"e": est[0]},
    ).fetchall()
    all_findings = [
        {"type": f[0], "value": f[1], "severity": f[2], "message": f[3]} for f in findings
    ]
    return {
        "bracket": est[1],
        "mechanics_bracket": est[2],
        "intent_bracket": est[3],
        "score": est[4],
        "rules_version": est[5],
        "generated_at": est[6],
        "confidence": {
            "tagging_coverage": est[7],
            "mechanics_clarity": est[8],
            "intent_alignment": est[9],
            "combo_detection_depth": est[10],
        },
        # #121 — floor + split findings. Estimates persisted before the floor
        # column report floor None; surfaces show "not yet evaluated".
        "floor_bracket": est[11],
        "findings": all_findings,
        "floor_findings": [f for f in all_findings if f["type"] in FLOOR_FINDING_TYPES],
        "advisory_findings": [f for f in all_findings if f["type"] not in FLOOR_FINDING_TYPES],
    }
