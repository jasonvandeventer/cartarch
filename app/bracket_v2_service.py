"""Bracket Estimator V2 — V1 implementation.

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

from sqlalchemy import text
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
            "WHERE active = 1 AND rules_version = :v AND card_name IN ("
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
                message=f"Mass land denial detected ({mld_count}). Pushes deck to Bracket 4+.",
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
                message=(
                    f"{gc_count} Game Changer{'s' if gc_count != 1 else ''}: "
                    f"{', '.join(signals['game_changers'][:5])}"
                    + (f" (+ {gc_count - 5} more)" if gc_count > 5 else "")
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
                deck_id, estimated_bracket, mechanics_bracket, final_bracket,
                score, rules_version
            ) VALUES (
                :d, :bracket, :mech, :final, :score, :v
            )
            """
        ),
        {
            "d": deck_id,
            "bracket": estimate.final_bracket,
            "mech": estimate.mechanics_bracket,
            "final": estimate.final_bracket,
            "score": estimate.score,
            "v": estimate.rules_version,
        },
    )
    estimate_id = result.lastrowid
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
