from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.audit_service import log_transaction
from app.models import Card, Deck, InventoryRow, StorageLocation
from app.scryfall import fetch_deck_tokens

_RAMP_LAND_RE = re.compile(r"search your library for .{0,60}land", re.IGNORECASE)
_DRAW_RE = re.compile(
    r"\bdraw (?:a|an|x|\d+|two|three|four|five|six|that many) cards?\b", re.IGNORECASE
)
_REMOVAL_RE = re.compile(
    r"(?:destroy|exile) target (?:\w+ ){0,4}(?:creature|artifact|enchantment|planeswalker|permanent)\b",
    re.IGNORECASE,
)
_WIPE_RE = re.compile(
    r"(?:destroy all|exile all (?:creatures?|permanents?)"
    r"|all creatures? (?:get|have) -\d+/-\d+"
    r"|each creature (?:gets?|has) -\d+/-\d+"
    r"|deals \d+ damage to each creature)",
    re.IGNORECASE,
)
_HEALTH_THRESHOLDS = {"ramp": 10, "draw": 10, "removal": 8, "wipes": 2}

CARD_ROLE_TAGS = [
    "Ramp",
    "Draw",
    "Tutor",
    "Removal",
    "Wipe",
    "Protection",
    "Engine",
    "Synergy",
    "Threat",
    "Hate",
]

_TAG_SET = set(CARD_ROLE_TAGS)


def get_row_tags(row) -> list[str]:
    if not row.tags:
        return []
    try:
        raw = json.loads(row.tags)
    except (json.JSONDecodeError, TypeError):
        return []
    return [t for t in raw if t in _TAG_SET]


def set_row_tags(row, tags: list[str]) -> None:
    valid = sorted({t for t in tags if t in _TAG_SET})
    row.tags = json.dumps(valid) if valid else None


def get_card_legality(card, format_name: str) -> str | None:
    """Return legality string for the given format, or None if unknown."""
    if not card.legalities or not format_name:
        return None
    try:
        data = json.loads(card.legalities)
    except (json.JSONDecodeError, TypeError):
        return None
    return data.get(format_name.lower())


def suggest_card_roles(card) -> list[str]:
    """Return auto-detected role tags for a card based on oracle text patterns."""
    oracle = (card.oracle_text or "").lower()
    tl = (card.type_line or "").lower()
    if "basic land" in tl or not oracle:
        return []
    is_land = "land" in tl
    is_land_tutor = bool(_RAMP_LAND_RE.search(oracle))
    suggestions = []
    if not is_land and "add {" in oracle:
        suggestions.append("Ramp")
    elif is_land_tutor:
        suggestions.append("Ramp")
    if _DRAW_RE.search(oracle):
        suggestions.append("Draw")
    if _REMOVAL_RE.search(oracle):
        suggestions.append("Removal")
    if _WIPE_RE.search(oracle):
        suggestions.append("Wipe")
    if "search your library for" in oracle and not is_land_tutor:
        suggestions.append("Tutor")
    return suggestions


_TYPE_ORDER = [
    "Creature",
    "Planeswalker",
    "Battle",
    "Instant",
    "Sorcery",
    "Enchantment",
    "Artifact",
    "Land",
]


def compute_deck_analytics(rows: list) -> dict:
    """Compute mana curve, type breakdown, and color pip counts from a list of InventoryRow ORM objects."""
    curve: dict[int, int] = {i: 0 for i in range(7)}
    curve_ramp: dict[int, int] = {i: 0 for i in range(7)}
    curve_spells: dict[int, int] = {i: 0 for i in range(7)}
    types: dict[str, int] = {}
    pips: dict[str, int] = {}
    total_cmc = 0.0
    non_land_copies = 0
    threat_cmc_total = 0.0
    threat_copies = 0

    for row in rows:
        card = row.card
        qty = row.quantity
        tl = (card.type_line or "").lower()
        oracle = (card.oracle_text or "").lower()

        matched = False
        for t in _TYPE_ORDER:
            if t.lower() in tl:
                types[t] = types.get(t, 0) + qty
                matched = True
                break
        if not matched:
            types["Other"] = types.get("Other", 0) + qty

        is_land = "land" in tl
        is_basic = "basic land" in tl

        if not is_land and card.cmc is not None:
            bucket = min(int(card.cmc), 6)
            curve[bucket] += qty
            total_cmc += card.cmc * qty
            non_land_copies += qty

            is_ramp = not is_basic and (
                "add {" in oracle
                or bool(_RAMP_LAND_RE.search(oracle))
                or "Ramp" in get_row_tags(row)
            )
            if is_ramp:
                curve_ramp[bucket] += qty
            else:
                curve_spells[bucket] += qty
                threat_cmc_total += card.cmc * qty
                threat_copies += qty

        if card.mana_cost:
            for color in ("W", "U", "B", "R", "G"):
                n = card.mana_cost.count("{" + color + "}") * qty
                if n:
                    pips[color] = pips.get(color, 0) + n

    avg_cmc = round(total_cmc / non_land_copies, 2) if non_land_copies else 0.0
    avg_threat_cmc = round(threat_cmc_total / threat_copies, 1) if threat_copies else 0.0

    total_ramp = sum(curve_ramp.values())
    turns_to_play = max(1, round(avg_threat_cmc) - (1 if total_ramp >= 10 else 0))

    high_cmc_spells = sum(curve_spells[i] for i in range(5, 7))
    dead_hand_pct = round(high_cmc_spells / threat_copies * 100) if threat_copies else 0
    dead_hand_risk = "high" if dead_hand_pct > 45 else ("moderate" if dead_hand_pct > 25 else "low")

    ordered_types = {k: types[k] for k in _TYPE_ORDER if k in types}
    if "Other" in types:
        ordered_types["Other"] = types["Other"]

    return {
        "curve": curve,
        "curve_ramp": curve_ramp,
        "curve_spells": curve_spells,
        "curve_max": max(curve.values()) or 1,
        "types": ordered_types,
        "types_max": max(types.values()) if types else 1,
        "pips": {c: pips[c] for c in ("W", "U", "B", "R", "G") if c in pips},
        "pips_max": max(pips.values()) if pips else 1,
        "avg_cmc": avg_cmc,
        "avg_threat_cmc": avg_threat_cmc,
        "turns_to_play": turns_to_play,
        "dead_hand_risk": dead_hand_risk,
        "dead_hand_pct": dead_hand_pct,
        "total_ramp": total_ramp,
    }


def compute_deck_tokens(rows: list) -> list[dict]:
    """Return deduplicated tokens produceable by cards in this deck."""
    scryfall_ids = [row.card.scryfall_id for row in rows if row.card and row.card.scryfall_id]
    if not scryfall_ids:
        return []
    return fetch_deck_tokens(scryfall_ids)


def compute_consistency(rows: list) -> dict:
    """Compute a 0-100 consistency score from draw density, ramp, tutors, curve smoothness, and role coverage."""
    seen_draw: set[str] = set()
    seen_ramp: set[str] = set()
    seen_tutor: set[str] = set()
    seen_removal: set[str] = set()
    spell_cmcs: list[float] = []

    for row in rows:
        card = row.card
        if not card:
            continue
        name = card.name or ""
        oracle = (card.oracle_text or "").lower()
        tl = (card.type_line or "").lower()
        is_land = "land" in tl
        is_basic = "basic land" in tl
        tags = get_row_tags(row)

        if not is_land and card.cmc is not None:
            spell_cmcs.extend([card.cmc] * row.quantity)

        if is_basic:
            continue

        is_land_tutor = bool(oracle and _RAMP_LAND_RE.search(oracle))
        ramp_oracle = bool(oracle) and ((not is_land and "add {" in oracle) or is_land_tutor)
        if (ramp_oracle or "Ramp" in tags) and name not in seen_ramp:
            seen_ramp.add(name)

        if ((oracle and _DRAW_RE.search(oracle)) or "Draw" in tags) and name not in seen_draw:
            seen_draw.add(name)

        tutor_oracle = bool(oracle) and "search your library for" in oracle and not is_land_tutor
        if (tutor_oracle or "Tutor" in tags) and name not in seen_tutor:
            seen_tutor.add(name)

        if (
            (oracle and _REMOVAL_RE.search(oracle)) or "Removal" in tags
        ) and name not in seen_removal:
            seen_removal.add(name)

    draw_n = len(seen_draw)
    ramp_n = len(seen_ramp)
    tutor_n = len(seen_tutor)
    removal_n = len(seen_removal)

    if spell_cmcs:
        mean = sum(spell_cmcs) / len(spell_cmcs)
        variance = sum((c - mean) ** 2 for c in spell_cmcs) / len(spell_cmcs)
        std_dev = round(variance**0.5, 1)
    else:
        std_dev = 0.0

    draw_score = min(25, round(draw_n / 10 * 25))
    ramp_score = min(20, round(ramp_n / 10 * 20))
    tutor_score = min(15, round(tutor_n / 5 * 15))
    smooth_score = 20 if std_dev < 1.5 else (12 if std_dev < 2.5 else 5)
    coverage_raw = min(1.0, ramp_n / 10) + min(1.0, draw_n / 10) + min(1.0, removal_n / 8)
    coverage_score = round(coverage_raw / 3 * 20)
    total = draw_score + ramp_score + tutor_score + smooth_score + coverage_score

    if total >= 80:
        label = "Consistent engine"
    elif total >= 65:
        label = "Stable midrange"
    elif total >= 50:
        label = "Moderate consistency"
    elif total >= 35:
        label = "High variance"
    else:
        label = "Glass cannon"

    if tutor_n >= 5:
        descriptor = "tutor-driven"
    elif draw_n >= 12 and ramp_n >= 10:
        descriptor = "well-oiled"
    elif ramp_n >= 12 and draw_n < 7:
        descriptor = "ramp-heavy"
    elif draw_n >= 10 and ramp_n < 7:
        descriptor = "card-advantage-reliant"
    elif std_dev > 2.5:
        descriptor = "spikey curve"
    else:
        descriptor = None

    tier = "ok" if total >= 65 else ("warn" if total >= 40 else "low")

    return {
        "score": total,
        "label": label,
        "descriptor": descriptor,
        "tier": tier,
        "breakdown": {
            "draw": {"score": draw_score, "max": 25, "count": draw_n},
            "ramp": {"score": ramp_score, "max": 20, "count": ramp_n},
            "tutors": {"score": tutor_score, "max": 15, "count": tutor_n},
            "smoothness": {"score": smooth_score, "max": 20, "std_dev": std_dev},
            "coverage": {"score": coverage_score, "max": 20, "pct": round(coverage_raw / 3 * 100)},
        },
    }


def compute_deck_health(rows: list) -> dict:
    """Compute ramp/draw/removal/wipe density and pip strain from InventoryRow ORM objects."""
    ramp_cards: list[str] = []
    draw_cards: list[str] = []
    removal_cards: list[str] = []
    wipe_cards: list[str] = []
    pip_demand: dict[str, int] = {}
    land_sources: dict[str, int] = {}

    for row in rows:
        card = row.card
        if not card:
            continue
        name = card.name or ""
        oracle = (card.oracle_text or "").lower()
        type_line = (card.type_line or "").lower()
        is_land = "land" in type_line
        is_basic = "basic land" in type_line
        qty = row.quantity
        tags = get_row_tags(row)

        if not is_land and card.mana_cost:
            for color in ("W", "U", "B", "R", "G"):
                n = card.mana_cost.count("{" + color + "}") * qty
                if n:
                    pip_demand[color] = pip_demand.get(color, 0) + n

        if is_land and card.color_identity is not None:
            for color in ("W", "U", "B", "R", "G"):
                if color in card.color_identity:
                    land_sources[color] = land_sources.get(color, 0) + qty

        if is_basic:
            continue

        ramp_oracle = bool(oracle) and (
            (not is_land and "add {" in oracle) or bool(_RAMP_LAND_RE.search(oracle))
        )
        if ramp_oracle or "Ramp" in tags:
            ramp_cards.append(name)

        if (oracle and _DRAW_RE.search(oracle)) or "Draw" in tags:
            draw_cards.append(name)

        if (oracle and _REMOVAL_RE.search(oracle)) or "Removal" in tags:
            removal_cards.append(name)

        if (oracle and _WIPE_RE.search(oracle)) or "Wipe" in tags:
            wipe_cards.append(name)

    pip_strain: dict[str, dict] = {}
    for color in ("W", "U", "B", "R", "G"):
        demand = pip_demand.get(color, 0)
        if demand == 0:
            continue
        sources = land_sources.get(color, 0)
        ratio = round(demand / sources, 1) if sources else None
        pip_strain[color] = {
            "demand": demand,
            "sources": sources,
            "ratio": ratio,
            "strained": ratio is None or ratio > 2.5,
        }

    def _metric(cards: list[str], key: str) -> dict:
        unique = sorted(set(cards))
        return {"count": len(unique), "cards": unique, "threshold": _HEALTH_THRESHOLDS[key]}

    return {
        "ramp": _metric(ramp_cards, "ramp"),
        "draw": _metric(draw_cards, "draw"),
        "removal": _metric(removal_cards, "removal"),
        "wipes": _metric(wipe_cards, "wipes"),
        "pip_strain": pip_strain,
    }


def compute_deck_combos(all_rows: list) -> dict:
    """Fetch win conditions and near-combos from CommanderSpellbook for this deck."""
    from app.spellbook import fetch_deck_combos

    commander_names = [r.card.name for r in all_rows if r.card and r.role == "commander"]
    main_names = [r.card.name for r in all_rows if r.card and r.role != "commander"]
    if not main_names and not commander_names:
        return {"included": [], "almost": []}
    return fetch_deck_combos(main_names, commander_names)


_CARE_ABOUT_PATTERNS = [
    r"whenever you cast [^.;]*\b{t}",
    r"\b{t}s? you control",
    r"\beach [^.;]*\b{t}",
    r"\b{t} spells?",
    r"your {t}s?",
    r"other {t}s?",
    r"noncreature {t}",
    r"\b{t}s? (?:and|or) \w+",
    r"\w+ (?:and|or) {t}s?\b",
]
_REMOVAL_PREFIX_RE = re.compile(r"(?:destroy|exile|counter|return) target [^.;]*$", re.IGNORECASE)
_CARD_TYPES_TO_DETECT = [
    "enchantment",
    "artifact",
    "instant",
    "sorcery",
    "planeswalker",
]
_CMC_MIN_RE = re.compile(r"mana value (?:of )?(\d+) or greater")
_CMC_MAX_RE = re.compile(r"mana value (?:of )?(\d+) or less")
_NON_SUBTYPE_RE = re.compile(r"\bnon-([A-Z][a-z]+)")


def extract_commander_themes(commander_rows: list) -> dict:
    """Parse commander oracle text to extract what the deck is built to care about."""
    card_types: set[str] = set()
    excluded_subtypes: set[str] = set()
    cmc_gate: dict = {}
    mechanics: set[str] = set()
    subtypes: set[str] = set()
    signals: list[str] = []

    for row in commander_rows:
        card = row.card
        if not card:
            continue
        oracle_raw = card.oracle_text or ""
        oracle = oracle_raw.lower()
        tl = card.type_line or ""

        # Tribal: only add subtypes that also appear in oracle text (commander cares about them)
        if "—" in tl:
            for word in tl.split("—", 1)[1].split():
                word = word.strip(".,/")
                if word and word[0].isupper() and word.lower() in oracle:
                    subtypes.add(word)
                    signals.append(f"tribal: {word}")

        # Card types the commander cares about (positive patterns only)
        for ct in _CARD_TYPES_TO_DETECT:
            for pat in _CARE_ABOUT_PATTERNS:
                m = re.search(pat.format(t=ct), oracle)
                if m:
                    # Reject if "destroy/exile/counter target" immediately precedes the match
                    prefix = oracle[: m.start()]
                    if not _REMOVAL_PREFIX_RE.search(prefix[-40:]):
                        card_types.add(ct)
                        signals.append(f"cares about {ct}s")
                        break

        # Non-X exclusions: "non-Aura enchantment" → Auras excluded from theme
        for match in _NON_SUBTYPE_RE.finditer(oracle_raw):
            excluded_subtypes.add(match.group(1))

        # CMC gates
        m_min = _CMC_MIN_RE.search(oracle)
        if m_min:
            cmc_gate["min"] = int(m_min.group(1))
            signals.append(f"mana value ≥{m_min.group(1)}")
        m_max = _CMC_MAX_RE.search(oracle)
        if m_max:
            cmc_gate["max"] = int(m_max.group(1))
            signals.append(f"mana value ≤{m_max.group(1)}")

        # Mechanics
        if "+1/+1 counter" in oracle:
            mechanics.add("counters")
            signals.append("counters")
        if "create" in oracle and "token" in oracle:
            mechanics.add("tokens")
            signals.append("tokens")
        elif "token" in oracle and re.search(r"\btokens? you control\b", oracle):
            mechanics.add("tokens")
            signals.append("tokens (caring)")
        if "your graveyard" in oracle or "from a graveyard" in oracle:
            mechanics.add("graveyard")
            signals.append("graveyard")
        if "sacrifice" in oracle:
            mechanics.add("sacrifice")
            signals.append("sacrifice")
        if "discard" in oracle:
            mechanics.add("discard")
            signals.append("discard")
        if "dying" in oracle or re.search(r"when(?:ever)?[^.;]*\bdies\b", oracle):
            mechanics.add("death_triggers")
            signals.append("death triggers")

    return {
        "card_types": card_types,
        "excluded_subtypes": excluded_subtypes,
        "cmc_gate": cmc_gate,
        "mechanics": mechanics,
        "subtypes": subtypes,
        "signals": sorted(set(signals)),
    }


def card_matches_theme(card, themes: dict) -> bool:
    """Return True if a card matches the commander's extracted themes."""
    tl = card.type_line or ""
    oracle = (card.oracle_text or "").lower()
    cmc = card.cmc or 0
    tl_words = set(tl.split())

    # Tribal subtype match
    if any(st in tl_words for st in themes["subtypes"]):
        return True

    # Card type match with exclusion + CMC gate checks
    for ct in themes["card_types"]:
        if ct.lower() not in tl.lower():
            continue
        if any(ex in tl_words for ex in themes["excluded_subtypes"]):
            continue
        if "min" in themes["cmc_gate"] and cmc < themes["cmc_gate"]["min"]:
            continue
        if "max" in themes["cmc_gate"] and cmc > themes["cmc_gate"]["max"]:
            continue
        return True

    # Mechanic matches
    if "counters" in themes["mechanics"] and "+1/+1 counter" in oracle:
        return True
    if "tokens" in themes["mechanics"] and "create" in oracle and "token" in oracle:
        return True
    if "graveyard" in themes["mechanics"] and "graveyard" in oracle:
        return True
    if "sacrifice" in themes["mechanics"] and "sacrifice" in oracle:
        return True
    if "discard" in themes["mechanics"] and "discard" in oracle:
        return True
    if "death_triggers" in themes["mechanics"] and re.search(
        r"when(?:ever)?[^.;]*\bdies\b", oracle
    ):
        return True

    return False


def compute_deck_synergy(all_rows: list, combos: dict) -> dict | None:
    """Classify each non-commander card as direct synergy, supporting, or unrelated."""
    commander_rows = [r for r in all_rows if r.role == "commander"]
    main_rows = [r for r in all_rows if r.role != "commander"]

    if not commander_rows or not main_rows:
        return None

    themes = extract_commander_themes(commander_rows)

    # All card names that appear in complete combos
    combo_card_names: set[str] = set()
    for combo in combos.get("included", []):
        for name in combo.get("card_names", []):
            combo_card_names.add(name)

    direct_cards: list[str] = []
    supporting_cards: list[str] = []
    unrelated_cards: list[str] = []

    for row in main_rows:
        card = row.card
        if not card:
            continue
        name = card.name or ""
        tags = get_row_tags(row)
        tl = card.type_line or ""

        is_direct = (
            name in combo_card_names
            or "Synergy" in tags
            or "Threat" in tags
            or card_matches_theme(card, themes)
        )
        is_supporting = not is_direct and (
            bool(
                set(tags)
                & {"Ramp", "Draw", "Removal", "Wipe", "Tutor", "Protection", "Engine", "Hate"}
            )
            or "Land" in tl
        )

        if is_direct:
            direct_cards.append(name)
        elif is_supporting:
            supporting_cards.append(name)
        else:
            unrelated_cards.append(name)

    total = len(main_rows)
    d_pct = round(len(direct_cards) / total * 100)
    s_pct = round(len(supporting_cards) / total * 100)
    u_pct = 100 - d_pct - s_pct

    return {
        "direct": len(direct_cards),
        "supporting": len(supporting_cards),
        "unrelated": len(unrelated_cards),
        "total": total,
        "direct_pct": d_pct,
        "supporting_pct": s_pct,
        "unrelated_pct": u_pct,
        "direct_cards": sorted(direct_cards),
        "supporting_cards": sorted(supporting_cards),
        "unrelated_cards": sorted(unrelated_cards),
        "themes": themes,
    }


_WIN_MORE_RE = re.compile(
    r"for each (?:creature|token|permanent) you control",
    re.IGNORECASE,
)
_BOARD_DEPENDENT_RE = re.compile(
    r"sacrifice (?:a|an|another) (?:creature|artifact|permanent|token)"
    r"|\btap (?:an? untapped|X untapped) creatures? you control\b"
    r"|\bconvoke\b",
    re.IGNORECASE,
)


def compute_dead_cards(all_rows: list, synergy: dict | None) -> list[dict] | None:
    """Identify upgrade targets: unrelated cards the user hasn't manually tagged.

    A card is a dead card candidate when the synergy engine classifies it as
    Unrelated (no commander theme match, no engine role, not in a combo, not a
    land) AND the user has assigned no role tag to it.  Oracle text patterns
    add a specific sub-reason (win-more or board-state-dependent) when present.
    """
    if not synergy:
        return None

    unrelated_names: set[str] = set(synergy.get("unrelated_cards", []))
    if not unrelated_names:
        return []

    results: list[dict] = []
    for row in all_rows:
        if row.role == "commander":
            continue
        card = row.card
        if not card or card.name not in unrelated_names:
            continue
        if get_row_tags(row):
            continue

        oracle = (card.oracle_text or "").lower()
        sub: list[str] = []
        if _WIN_MORE_RE.search(oracle):
            sub.append("win-more")
        if _BOARD_DEPENDENT_RE.search(oracle):
            sub.append("board-dependent")

        results.append(
            {
                "name": card.name,
                "sub": sub,
            }
        )

    return sorted(results, key=lambda x: x["name"])


_FAST_MANA = frozenset(
    [
        "Mana Crypt",
        "Mox Diamond",
        "Chrome Mox",
        "Mox Opal",
        "Jeweled Lotus",
        "Grim Monolith",
        "Mana Vault",
        "Lotus Petal",
        "Ancient Tomb",
    ]
)

_FREE_INTERACTION = frozenset(
    [
        "Force of Will",
        "Force of Negation",
        "Mana Drain",
        "Fierce Guardianship",
        "Deflecting Swat",
        "Flusterstorm",
        "Mental Misstep",
        "Pact of Negation",
        "Commandeer",
    ]
)

_MASS_LAND_DENIAL = frozenset(
    [
        "Armageddon",
        "Ravages of War",
        "Jokulhaups",
        "Devastation",
        "Obliterate",
        "Decree of Annihilation",
        "Catastrophe",
        "Ruination",
        "Boom // Bust",
    ]
)


def compute_deck_bracket(all_rows: list, combos: dict) -> dict:
    """Estimate Commander bracket (1-5) from deck signals."""
    fast_mana: list[str] = []
    free_interaction: list[str] = []
    mass_land_denial: list[str] = []
    extra_turns: list[str] = []
    tutors: list[str] = []

    for row in all_rows:
        card = row.card
        if not card:
            continue
        name = card.name or ""
        oracle = (card.oracle_text or "").lower()

        if name in _FAST_MANA:
            fast_mana.append(name)
        if name in _FREE_INTERACTION:
            free_interaction.append(name)
        if name in _MASS_LAND_DENIAL:
            mass_land_denial.append(name)
        if "take an extra turn" in oracle:
            extra_turns.append(name)
        if (
            "search your library for a card" in oracle
            and "land" not in oracle.split("search your library for a card")[0][-20:]
        ):
            tutors.append(name)

    combo_count = len(combos.get("included", []))

    bracket = 1
    reasons: list[str] = []

    # Bracket 2 floor
    if tutors:
        bracket = max(bracket, 2)
        reasons.append(f"{len(tutors)} tutor{'s' if len(tutors) != 1 else ''}")

    # Bracket 3 floors
    if combo_count >= 1:
        bracket = max(bracket, 3)
        reasons.append(f"{combo_count} infinite combo{'s' if combo_count != 1 else ''}")
    if mass_land_denial:
        bracket = max(bracket, 3)
        reasons.append(f"mass land denial ({mass_land_denial[0]})")
    if extra_turns:
        bracket = max(bracket, 3)
        reasons.append(f"extra turn spells ({extra_turns[0]})")
    if len(tutors) >= 3:
        bracket = max(bracket, 3)

    # Bracket 4 floors
    if fast_mana:
        bracket = max(bracket, 4)
        reasons.append(f"fast mana ({', '.join(fast_mana[:3])})")
    if free_interaction:
        bracket = max(bracket, 4)
        reasons.append(f"free interaction ({', '.join(free_interaction[:2])})")
    if combo_count >= 3:
        bracket = max(bracket, 4)

    # Bracket 5: multiple cEDH signals together
    if len(fast_mana) >= 2 and free_interaction and combo_count >= 2:
        bracket = 5
        reasons.append("multiple cEDH staples")

    if not reasons:
        reasons.append(
            "no tutors, fast mana, free interaction, combos, mass land denial, or extra turn spells detected"
        )

    return {
        "bracket": bracket,
        "reasons": reasons,
        "signals": {
            "fast_mana": fast_mana,
            "free_interaction": free_interaction,
            "mass_land_denial": mass_land_denial,
            "extra_turns": extra_turns,
            "tutors": tutors,
            "combo_count": combo_count,
        },
    }


def create_deck(
    session: Session,
    user_id: int,
    name: str,
    format_name: str = "",
    notes: str = "",
) -> Deck:
    deck_name = name.strip()

    location = StorageLocation(
        user_id=user_id,
        name=deck_name,
        type="deck",
        parent_id=None,
        sort_order=0,
    )
    session.add(location)
    session.flush()

    deck = Deck(
        user_id=user_id,
        storage_location_id=location.id,
        name=deck_name,
        format=format_name.strip() or None,
        notes=notes.strip() or None,
    )
    session.add(deck)
    session.commit()
    session.refresh(deck)
    return deck


def update_deck(
    session: Session,
    deck_id: int,
    user_id: int,
    name: str,
    format_name: str = "",
    notes: str = "",
) -> Deck:
    deck = get_deck(session, deck_id=deck_id, user_id=user_id)
    if not deck:
        raise ValueError("Deck not found.")

    name = name.strip()
    if not name:
        raise ValueError("Deck name is required.")

    existing = (
        session.query(Deck)
        .filter(Deck.user_id == user_id, Deck.name == name, Deck.id != deck_id)
        .first()
    )
    if existing:
        raise ValueError(f"A deck named '{name}' already exists.")

    deck.name = name
    deck.format = format_name.strip() or None
    deck.notes = notes.strip() or None

    if deck.storage_location_id:
        location = (
            session.query(StorageLocation)
            .filter(
                StorageLocation.id == deck.storage_location_id,
                StorageLocation.user_id == user_id,
            )
            .first()
        )
        if location:
            location.name = name

    session.commit()
    return deck


def list_decks(session: Session, user_id: int) -> list[Deck]:
    decks = (
        session.query(Deck)
        .options(joinedload(Deck.storage_location))
        .filter(Deck.user_id == user_id)
        .order_by(Deck.name.asc())
        .all()
    )

    for deck in decks:
        if not deck.storage_location_id:
            deck.card_count = 0
            continue

        deck.card_count = (
            session.query(func.sum(InventoryRow.quantity))
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.storage_location_id == deck.storage_location_id,
            )
            .scalar()
            or 0
        )

        commander_rows = (
            session.query(InventoryRow)
            .join(Card)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.storage_location_id == deck.storage_location_id,
                InventoryRow.role == "commander",
            )
            .all()
        )
        seen: set[str] = set()
        for row in commander_rows:
            for letter in (row.card.color_identity or "").split():
                seen.add(letter)
        deck.color_identity = " ".join(p for p in ["W", "U", "B", "R", "G"] if p in seen)

        all_rows = (
            session.query(InventoryRow)
            .join(Card)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.storage_location_id == deck.storage_location_id,
            )
            .all()
        )
        combos = compute_deck_combos(all_rows)
        deck.bracket = compute_deck_bracket(all_rows, combos)
        deck.consistency = compute_consistency(all_rows) if all_rows else None

    return decks


def get_deck(session: Session, deck_id: int, user_id: int) -> Deck | None:
    return (
        session.query(Deck)
        .options(joinedload(Deck.storage_location))
        .filter(
            Deck.id == deck_id,
            Deck.user_id == user_id,
        )
        .first()
    )


def pull_card_to_deck(
    session: Session,
    user_id: int,
    deck_id: int,
    inventory_row_id: int,
    quantity: int,
) -> bool:
    if quantity < 1:
        return False

    deck = (
        session.query(Deck)
        .filter(
            Deck.id == deck_id,
            Deck.user_id == user_id,
        )
        .first()
    )

    row = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.id == inventory_row_id,
            InventoryRow.user_id == user_id,
        )
        .first()
    )

    if not row or not deck or not deck.storage_location_id or row.quantity < quantity:
        return False

    existing_deck_row = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.card_id == row.card_id,
            InventoryRow.finish == row.finish,
            InventoryRow.storage_location_id == deck.storage_location_id,
            InventoryRow.is_pending.is_(False),
        )
        .first()
    )

    if existing_deck_row:
        existing_deck_row.quantity += quantity
        existing_deck_row.updated_at = datetime.utcnow()
    else:
        existing_deck_row = InventoryRow(
            user_id=user_id,
            card_id=row.card_id,
            storage_location_id=deck.storage_location_id,
            finish=row.finish,
            quantity=quantity,
            drawer=None,
            slot=None,
            is_pending=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(existing_deck_row)
        session.flush()

    row.quantity -= quantity
    row.updated_at = datetime.utcnow()

    if row.quantity <= 0:
        session.delete(row)

    log_transaction(
        session=session,
        user_id=user_id,
        event_type="pull_to_deck",
        card_id=existing_deck_row.card_id,
        finish=existing_deck_row.finish,
        quantity_delta=-quantity,
        source_location="collection",
        destination_location=f"deck:{deck.name}",
        inventory_row_id=existing_deck_row.id,
        note=f"Pulled into deck {deck.name}",
    )

    session.commit()
    return True


def return_card_from_deck(
    session: Session,
    user_id: int,
    deck_row_id: int,
    drawer: str = "",
    slot: str = "",
) -> bool:
    deck_row = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .join(
            Deck,
            Deck.storage_location_id == InventoryRow.storage_location_id,
        )
        .filter(
            Deck.user_id == user_id,
            InventoryRow.id == deck_row_id,
            InventoryRow.user_id == user_id,
        )
        .first()
    )

    if not deck_row:
        return False

    deck = (
        session.query(Deck)
        .filter(
            Deck.user_id == user_id,
            Deck.storage_location_id == deck_row.storage_location_id,
        )
        .first()
    )

    if not deck:
        return False

    normalized_drawer = drawer.strip() or None
    normalized_slot = slot.strip() or None

    existing_row = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.card_id == deck_row.card_id,
            InventoryRow.finish == deck_row.finish,
            InventoryRow.drawer == normalized_drawer,
            InventoryRow.slot == normalized_slot,
            InventoryRow.is_pending.is_(True),
        )
        .first()
    )

    if existing_row:
        existing_row.quantity += deck_row.quantity
        existing_row.storage_location_id = None
        existing_row.is_pending = True
        existing_row.updated_at = datetime.utcnow()
    else:
        existing_row = InventoryRow(
            user_id=user_id,
            card_id=deck_row.card_id,
            finish=deck_row.finish,
            quantity=deck_row.quantity,
            drawer=normalized_drawer,
            slot=normalized_slot,
            storage_location_id=None,
            is_pending=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(existing_row)
        session.flush()

    log_transaction(
        session=session,
        user_id=user_id,
        event_type="return_from_deck",
        card_id=deck_row.card_id,
        finish=deck_row.finish,
        quantity_delta=deck_row.quantity,
        source_location=f"deck:{deck.name}",
        destination_location="collection",
        inventory_row_id=existing_row.id,
        note=f"Returned from deck {deck.name}",
    )

    session.delete(deck_row)
    session.commit()
    return True


def delete_deck(session: Session, deck_id: int, user_id: int) -> bool:
    deck = get_deck(session, deck_id=deck_id, user_id=user_id)
    if not deck:
        return False

    if deck.storage_location_id:
        # Delete all inventory rows in this deck
        deck_rows = (
            session.query(InventoryRow)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.storage_location_id == deck.storage_location_id,
            )
            .all()
        )

        for row in deck_rows:
            session.delete(row)

        # Delete the storage location itself
        location = (
            session.query(StorageLocation)
            .filter(
                StorageLocation.id == deck.storage_location_id,
                StorageLocation.user_id == user_id,
            )
            .first()
        )

        if location:
            session.delete(location)

    # Delete the deck
    session.delete(deck)
    session.commit()
    return True
