from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session, joinedload

from app.audit_service import log_transaction
from app.models import Card, Deck, InventoryRow, StorageLocation
from app.scryfall import fetch_deck_tokens

# Library search for lands. Anchored to "your library" so opponent-search effects
# (Demolition Field, Ghost Quarter, Strip Mine giving the opponent a basic) do NOT
# trigger Ramp. Includes basic-land subtype words so cards like Nature's Lore and
# Three Visits are detected even when "land" doesn't appear directly.
_RAMP_LAND_RE = re.compile(
    r"search your library for .{0,60}\b(?:land|forest|island|plains|mountain|swamp)\b",
    re.IGNORECASE,
)
# Mana acceleration patterns that don't search libraries. Gated to non-land cards at
# call sites so utility lands ("Add {C}") and basic lands don't get tagged Ramp.
_RAMP_NON_LAND_RE = re.compile(
    r"\badds? \{"
    r"|\badds? (?:one|two|three|four|five|six|seven|eight|x|an additional)\b.{0,40}\bmana\b"
    r"|\badds? .{0,40}\bmana of any\b"
    # "Add an amount of {B} equal to..." — Soldevi Adnate, Bubbling Muck. Match
    # "add" followed by a mana symbol within one sentence. The strip of quoted
    # token-grant text upstream prevents Sifter-of-Skulls-style false positives.
    r"|\badds? [^.]{0,60}\{[wubrgcxs\d]\}"
    r"|creates? .{0,30}\btreasure tokens?\b"
    r"|costs? \{\d+\} less to cast"
    r"|play (?:a |an |\w+ )?additional lands?\b"
    r"|put (?:a |an? |up to \w+ )?(?:basic )?lands? cards? from your hand onto the battlefield"
    r"|double the (?:amount of )?mana",
    re.IGNORECASE,
)
_DRAW_RE = re.compile(
    r"\bdraws? (?:a|an|x|\d+|two|three|four|five|six|seven|that many|an additional)\b.{0,30}\bcards?\b"
    r"|exile the top (?:\w+ )?cards?.{0,80}(?:may )?(?:cast|play)"
    r"|each player draws"
    r"|(?:reveal|look at) the top \w+ cards?.{0,80}put .{0,30}into your hand",
    re.IGNORECASE,
)
# Sentence-level trigger references like Sheoldred's "Whenever a player draws a card,
# that player loses 2 life." — these aren't draw effects, they punish drawing.
# Trigger CONDITION mentions drawing (Sheoldred). Restricted to the part of the
# whenever-clause before the comma so that consequence draws (Mangara, Skullclamp)
# still register as real draw effects.
_TRIGGER_DRAW_RE = re.compile(r"whenever [^,.]*\bdraws?\b[^,.]*[,.]", re.IGNORECASE)


def matches_draw(oracle: str) -> bool:
    """True if oracle has a real draw effect (not just a trigger that references drawing)."""
    if not oracle or not _DRAW_RE.search(oracle):
        return False
    if not _TRIGGER_DRAW_RE.search(oracle):
        return True
    stripped = _TRIGGER_DRAW_RE.sub("", oracle)
    return bool(_DRAW_RE.search(stripped))


_REMOVAL_RE = re.compile(
    r"(?:destroy|exile) target (?:\w+ ){0,4}(?:creature|artifact|enchantment|planeswalker|permanent|land|nonbasic land|nonland permanent)\b"
    r"|counter target (?:spell|activated ability|triggered ability)"
    r"|return target (?:\w+ ){0,4}(?:creature|permanent|nonland permanent)\b.{0,40}\bto (?:its )?owner'?s? hand"
    r"|\bdeals? \d+ damage to (?:target|any target)"
    r"|target (?:creature|permanent) gets -[\dXx]+/-[\dXx]+"
    r"|target creature fights"
    # Edicts (single-target and each-player) are removal, not wipes — one creature
    # per opponent reads as targeted answer rather than a sweeper.
    r"|(?:target|each) (?:opponent|player) sacrifices a (?:creature|permanent|nonland permanent)",
    re.IGNORECASE,
)
_WIPE_RE = re.compile(
    r"destroy all (?:creatures?|permanents?|nonland permanents?|attacking creatures?|other creatures?)\b"
    # "Destroy each creature an opponent controls..." — Promise of Loyalty style.
    r"|destroy each (?:creature|permanent|nonland permanent)"
    r"|exile all (?:creatures?|permanents?|nonland permanents?)"
    r"|return all (?:creatures?|permanents?|nonland permanents?)\b.{0,40}\bto\b"
    r"|all creatures? (?:get|have|gets?|has) -[\dXx]+/-[\dXx]+"
    r"|each creature (?:gets?|has) -[\dXx]+/-[\dXx]+"
    # Mass damage is wipe-only when the damage hits creatures. Player damage
    # ("deals 1 damage to each opponent") is a per-trigger ping, not a sweeper.
    r"|deals \d+ damage to each (?:creature|other creature)" r"|\boverload \{"
    # "Each player ... sacrifices the rest" — Promise of Loyalty wording: each
    # player keeps one creature, sacrifices everything else.
    r"|each player.{0,80}sacrifices? .{0,40}(?:the rest|other (?:creatures?|permanents?)|all but)",
    re.IGNORECASE,
)
_PROTECTION_RE = re.compile(
    r"\b(?:hexproof|indestructible|shroud|protection from)\b.{0,40}\byou control\b"
    r"|\byou control\b.{0,40}\b(?:hexproof|indestructible|shroud|protection from)\b"
    r"|\bgains? (?:hexproof|indestructible|shroud|protection from|ward)\b"
    r"|prevent (?:all|the next \d+|all combat) damage"
    r"|(?:can'?t|cannot) be (?:countered|the target)"
    r"|\bwould (?:die|be destroyed|be put into (?:its|their|a) (?:owner'?s? )?graveyard)\b.{0,40}\binstead\b"
    r"|regenerate target",
    re.IGNORECASE,
)
# Sac outlets (activation cost includes sacrifice — colon-delimited) and graveyard
# recursion engines. Colon-only avoids matching Bargain-style cast costs ("sacrifice
# an artifact, enchantment, or token as you cast this spell"). Recursion catches
# Reassembling Skeleton, Junji-style, and Victimize-style ("creature cards in
# your graveyard ... return ... to the battlefield") wordings.
_ENGINE_RE = re.compile(
    r"\bsacrifice (?:a|an|another)\s+(?:[\w'-]+\s+){0,3}(?:creature|permanent|artifact|token|treasure|food|clue|blood)\s*:"
    r"|(?:return|put) [^.]{0,80}from [^.]{0,30}graveyards?[^.]{0,30}(?:to|onto) the battlefield"
    r"|(?:creature|permanent) cards? in your graveyard.{0,80}return [^.]{0,80}(?:to|onto) the battlefield",
    re.IGNORECASE,
)
# Hard threat indicators only. Power/toughness aren't in the Card model so
# soft "this is a big creature" threats are missed by design — manual tagging.
# Per-trigger drains ("each opponent loses 1 life") and free-cast tutors
# ("cast without paying") were dropped — too noisy.
_THREAT_RE = re.compile(
    r"\byou win the game\b"
    r"|(?:target |each |that )?(?:opponents?|players?)\s+loses? the game\b"
    r"|\binfect\b"
    r"|\btoxic \d+\b"
    r"|\bextra (?:combat phase|turn)\b",
    re.IGNORECASE,
)
# Disruption against opponents: graveyard hate, opp-stax, draw hate, stop-effects,
# enter-tapped slowdowns.
_HATE_RE = re.compile(
    r"exile (?:all|any|target|each|that) (?:[\w'-]+\s+){0,2}graveyards?\b"
    r"|if .{0,80}\bwould\b.{0,40}\bgraveyards?\b.{0,80}\bexile\b.{0,40}\binstead\b"
    r"|opponents?\s+(?:can'?t|cannot|may not)\s+(?:cast|draw|gain|search|untap|attack)"
    r"|\bcreatures? (?:and \w+ )?(?:your )?opponents control enter (?:the battlefield )?tapped"
    r"|\bwhenever an opponent draws? a card"
    r"|each opponent skips",
    re.IGNORECASE,
)
# Token-granted abilities embedded in oracle text — Sifter of Skulls' "It has
# 'Sacrifice this creature: Add {C}.'" puts mana production in the TOKEN's text,
# not the parent. Strip quoted granted abilities before checking ramp.
_QUOTED_ABILITY_RE = re.compile(r'"[^"]+"')


def matches_ramp_non_land(oracle: str) -> bool:
    """True if oracle has a non-land-tutor ramp pattern, ignoring quoted token abilities."""
    if not oracle:
        return False
    cleaned = _QUOTED_ABILITY_RE.sub("", oracle)
    return bool(_RAMP_NON_LAND_RE.search(cleaned))


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


def suggest_card_roles(card, themes: dict | None = None) -> list[str]:
    """Return auto-detected role tags for a card based on oracle text patterns.

    When `themes` is provided (output of extract_commander_themes), Synergy is
    suggested for cards that match the deck's strategy via card_matches_theme.
    """
    oracle = (card.oracle_text or "").lower()
    tl = (card.type_line or "").lower()
    if "basic land" in tl or not oracle:
        return []
    is_land = "land" in tl
    is_land_tutor = bool(_RAMP_LAND_RE.search(oracle))
    suggestions = []
    if (not is_land and matches_ramp_non_land(oracle)) or is_land_tutor:
        suggestions.append("Ramp")
    if matches_draw(oracle):
        suggestions.append("Draw")
    if _REMOVAL_RE.search(oracle):
        suggestions.append("Removal")
    if _WIPE_RE.search(oracle):
        suggestions.append("Wipe")
    if _PROTECTION_RE.search(oracle):
        suggestions.append("Protection")
    if _ENGINE_RE.search(oracle):
        suggestions.append("Engine")
    if _THREAT_RE.search(oracle):
        suggestions.append("Threat")
    if _HATE_RE.search(oracle):
        suggestions.append("Hate")
    if "search your library for" in oracle and not is_land_tutor:
        suggestions.append("Tutor")
    if themes:
        synergy_match = card_matches_theme(card, themes)
        if not synergy_match:
            mechanics = themes.get("mechanics") or set()
            # In death-trigger decks, sac outlets and graveyard recursion are
            # synergistic. Gating on Engine ensures we only catch real sac outlets
            # ("Sacrifice a creature:") and recursion ("from your graveyard to
            # the battlefield") — not self-sac lands (Myriad Landscape) or
            # Bargain-cost cards (Beseech the Mirror).
            if "death_triggers" in mechanics and "Engine" in suggestions:
                synergy_match = True
        if synergy_match:
            suggestions.append("Synergy")
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


# Valid values for the deck-list view's group-by axis. The deck_detail page
# accepts these from the URL query (?group=X) and the user's persisted
# preference. Anything outside this set falls back to "type".
DECK_GROUP_BY_OPTIONS = ("type", "cmc", "color", "role", "subtype")

DECK_VIEW_MODES = ("grid", "list")


def _primary_card_type(card) -> str:
    """Return the dominant card type ('Creature', 'Instant', etc.) used for
    type-grouping and other type-aware analytics. Matches the existing
    ``_TYPE_ORDER`` priority: Creature > Planeswalker > Battle > Instant >
    Sorcery > Enchantment > Artifact > Land. Falls back to 'Other' when no
    known type word appears in ``type_line``.
    """
    tl = card.type_line or ""
    # Type line shape: "Supertype Type — Subtype" (em-dash). Strip everything
    # after the em-dash so subtypes don't poison the type match (e.g. "Land
    # — Forest" should still match "Land", and a creature subtype "Wizard"
    # shouldn't accidentally match anything).
    head = tl.split("—")[0]
    for t in _TYPE_ORDER:
        if t.lower() in head.lower():
            return t
    return "Other"


def _card_subtypes(card) -> list[str]:
    """Subtypes after the em-dash. Empty list when none present."""
    tl = card.type_line or ""
    if "—" not in tl:
        return []
    tail = tl.split("—", 1)[1].strip()
    return [s.strip() for s in tail.split() if s.strip()]


def _card_color_bucket(card) -> str:
    """Color bucket for grouping: 'White', 'Blue', etc. for monocolor;
    'Multicolor' when two or more colors are present; 'Colorless' otherwise.
    """
    colors = (card.colors or "").strip().upper()
    if not colors:
        return "Colorless"
    letters = [c for c in colors.split() if c in {"W", "U", "B", "R", "G"}]
    if len(letters) >= 2:
        return "Multicolor"
    if len(letters) == 1:
        return {
            "W": "White",
            "U": "Blue",
            "B": "Black",
            "R": "Red",
            "G": "Green",
        }[letters[0]]
    return "Colorless"


def _cmc_bucket(card) -> str:
    """CMC bucket label. 0-5 own buckets, 6+ pooled."""
    cmc = card.cmc
    if cmc is None:
        return "Unknown CMC"
    try:
        n = int(cmc)
    except (TypeError, ValueError):
        return "Unknown CMC"
    if n >= 6:
        return "6+"
    return str(n)


_COLOR_BUCKET_ORDER = [
    "White",
    "Blue",
    "Black",
    "Red",
    "Green",
    "Multicolor",
    "Colorless",
]


def group_deck_items(items: list[dict], group_by: str) -> list[dict]:
    """Bucket deck-detail items by the chosen axis and return ordered groups.

    Each group dict carries:
      - ``label``: human-readable group title
      - ``count``: sum of item quantities in the group (total cards, not
        unique-name count, so "4 Lightning Bolt" contributes 4)
      - ``unique``: count of distinct items (rows) in the group
      - ``subgroups``: list of {label, count} dicts for the inline breakdown
        line shown beneath the group header. Only meaningful for the type
        group when the bucket is 'Creature' (subtype counts). Empty list
        for everything else in v1.
      - ``rows``: the items themselves, preserving the caller's input order.
        Named ``rows`` rather than ``items`` to dodge Jinja2's attribute
        lookup priority — ``group.items`` on a dict resolves to the
        ``dict.items()`` method, not the value at key "items".
    """
    if group_by not in DECK_GROUP_BY_OPTIONS:
        group_by = "type"

    buckets: dict[str, list[dict]] = {}
    for item in items:
        card = item.get("card")
        if not card:
            continue
        if group_by == "type":
            key = _primary_card_type(card)
        elif group_by == "cmc":
            key = _cmc_bucket(card)
        elif group_by == "color":
            key = _card_color_bucket(card)
        elif group_by == "role":
            tags = item.get("tags") or []
            key = tags[0] if tags else "Untagged"
        elif group_by == "subtype":
            subtypes = _card_subtypes(card)
            key = subtypes[0] if subtypes else "No subtype"
        else:
            key = "Other"
        buckets.setdefault(key, []).append(item)

    # Stable ordering per group_by axis.
    if group_by == "type":
        ordered_keys = [k for k in _TYPE_ORDER if k in buckets]
        ordered_keys += sorted(k for k in buckets if k not in _TYPE_ORDER)
    elif group_by == "cmc":
        # Numeric order: 0, 1, 2, 3, 4, 5, 6+, Unknown
        def _cmc_sort(k: str) -> tuple[int, str]:
            if k == "6+":
                return (6, "")
            if k == "Unknown CMC":
                return (99, "")
            try:
                return (int(k), "")
            except ValueError:
                return (100, k)

        ordered_keys = sorted(buckets.keys(), key=_cmc_sort)
    elif group_by == "color":
        ordered_keys = [k for k in _COLOR_BUCKET_ORDER if k in buckets]
        ordered_keys += sorted(k for k in buckets if k not in _COLOR_BUCKET_ORDER)
    else:
        ordered_keys = sorted(buckets.keys())

    groups: list[dict] = []
    for key in ordered_keys:
        group_items = buckets[key]
        total_count = sum(int(i.get("quantity") or 0) for i in group_items)
        subgroups: list[dict] = []
        # Sub-group breakdown: only for type=Creature when group_by='type'.
        # Creature subtypes (Human, Elf, etc.) get individual counts; sorted
        # by count desc so the most-prevalent subtype is surfaced first.
        if group_by == "type" and key == "Creature":
            sub_counts: dict[str, int] = {}
            for it in group_items:
                qty = int(it.get("quantity") or 0)
                for st in _card_subtypes(it["card"]):
                    sub_counts[st] = sub_counts.get(st, 0) + qty
            subgroups = [
                {"label": label, "count": count}
                for label, count in sorted(sub_counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ]
        groups.append(
            {
                "label": key,
                "count": total_count,
                "unique": len(group_items),
                "subgroups": subgroups,
                "rows": group_items,
            }
        )

    return groups


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
                matches_ramp_non_land(oracle)
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
    ramp_acceleration = 1 if total_ramp >= 10 else 0
    turns_to_play = max(1, round(avg_threat_cmc) - ramp_acceleration)

    # Peak turn = the turn at which the most threats become castable. The CMC bucket
    # with the largest non-ramp count, ramp-adjusted by 1 if there are 10+ ramp pieces.
    if threat_copies:
        peak_cmc = max(range(7), key=lambda k: curve_spells[k])
        peak_turn = max(1, peak_cmc - ramp_acceleration) if curve_spells[peak_cmc] else None
        peak_threat_count = curve_spells[peak_cmc] if peak_cmc is not None else 0
    else:
        peak_turn = None
        peak_threat_count = 0

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
        "peak_turn": peak_turn,
        "peak_threat_count": peak_threat_count,
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
        ramp_oracle = bool(oracle) and (
            (not is_land and matches_ramp_non_land(oracle)) or is_land_tutor
        )
        if (ramp_oracle or "Ramp" in tags) and name not in seen_ramp:
            seen_ramp.add(name)

        if (matches_draw(oracle) or "Draw" in tags) and name not in seen_draw:
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
            (not is_land and matches_ramp_non_land(oracle)) or bool(_RAMP_LAND_RE.search(oracle))
        )
        if ramp_oracle or "Ramp" in tags:
            ramp_cards.append(name)

        if matches_draw(oracle) or "Draw" in tags:
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
    if "tokens" in themes["mechanics"] and (
        ("create" in oracle and "token" in oracle)
        or "becomes a token" in oracle
        or "tokens you control" in oracle
    ):
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


# Tier priority for ordering reconciliation matches. Lower number = preferred
# source for the recommended move. "drawer" is most "loose" / fungible
# inventory; "pending" is last-resort since those rows haven't been physically
# placed yet. Empty tiers are skipped naturally — most users have no drawer
# locations, so binder becomes the effective first tier for them.
_RECONCILE_TIER_PRIORITY: dict[str, int] = {
    "drawer": 0,
    "binder": 1,
    "box": 2,
    "other": 3,
    "pending": 4,
}


def find_inventory_matches_for_deck_import(
    session: Session,
    user_id: int,
    deck_id: int,
    parsed_rows: list[dict],
) -> list[dict]:
    """Read-only reconciliation lookup for the deck-import flow.

    For each parsed import row, find existing inventory the user owns that
    could be moved to the destination deck instead of importing new copies.
    This is the data layer behind the upcoming import-preview reconciliation
    UI (design doc: docs/deck_collection_model.md §3.3).

    Pure read function — does not mutate state. Callers (the eventual
    Session 3 commit handler) consume the recommendation by either:
      - calling ``pull_card_to_deck`` for each match in order until
        ``recommended_move_qty`` is reached, OR
      - falling through to ``persist_import_rows`` + ``place_imported_rows``
        for ``recommended_new_qty`` copies.

    Args:
        session:       SQLAlchemy session.
        user_id:       Owner of the deck and inventory. Per-user scoped;
                       this function never returns matches from other users.
        deck_id:       Target deck. Raises ``ValueError("deck not found")``
                       if the deck doesn't exist or belongs to another user.
        parsed_rows:   List of dicts matching the shape produced by
                       ``parse_scanner_csv`` / ``parse_text_list`` in
                       ``app/import_service.py``. Each must have at least
                       ``line_number``, ``scryfall_id``, ``finish``,
                       ``quantity``.

    Returns:
        One dict per parsed row, preserving input order. Each output dict::

            {
                "line_number": int,
                "card_id": int | None,        # None if scryfall_id not in catalog
                "scryfall_id": str,
                "finish": str,
                "quantity_needed": int,
                "matches": [                  # non-deck rows, eligible to MOVE
                    {
                        "inventory_row_id": int,
                        "location_name": str,
                        "location_type": str,  # drawer|binder|box|other|pending
                        "quantity_available": int,
                        "tags": list[str],     # source-row role tags (informational)
                    },
                    ...
                ],
                "target_deck_matches": [...],  # rows in the destination deck
                "other_deck_matches":  [...],  # rows in any OTHER deck
                "total_available":       int,  # sum of `matches`
                "total_in_target_deck":  int,  # sum of `target_deck_matches`
                "total_in_other_decks":  int,  # sum of `other_deck_matches`
                "recommended_action": str,
                    # "move_existing" | "move_existing_plus_new" | "import_new"
                "recommended_move_qty": int,
                "recommended_new_qty": int,
            }

    Match selection / bucketing rules:
      - Match same ``(user_id, card_id, finish)``.
      - **Non-deck rows** (drawer/binder/box/other/pending) go into
        ``matches`` — these are the movable inventory for the move
        recommendation.
      - **Target-deck rows** (``storage_location_id == deck.storage_location_id``)
        go into ``target_deck_matches``. They're informational for the UI
        ("Already in this deck: N") and used by the commit handler to merge
        new imports into the existing deck row rather than duplicate it.
      - **Other-deck rows** (``type == "deck"`` but a different deck) go
        into ``other_deck_matches``. Informational only — surfaced to the
        user but not auto-moved (design doc §3.3 — don't silently
        cannibalize another deck).
      - Pending rows (``is_pending=True``, no ``storage_location_id``) are
        included in ``matches`` with synthetic ``location_name="Pending"``
        and ``location_type="pending"``.

      The ``recommended_action`` is driven by ``total_available`` (non-deck
      rows) only — deck-located rows never auto-shift the recommendation.

    Match ordering (callers consume in order until ``move_qty`` is hit):
      1. ``type=="drawer"`` — drawer-sorter slots; loosest inventory.
         Only one user account in this app has drawers configured.
      2. ``type=="binder"``
      3. ``type=="box"``
      4. ``type=="other"``
      5. ``type=="pending"``
      Within tier: ordered by ``inventory_row_id`` ASC (oldest first) for
      stable, deterministic output.

    Recommended action — pure function of ``total_available`` vs
    ``quantity_needed``::

        total_available >= quantity_needed
            -> "move_existing", move=needed, new=0
        0 < total_available < quantity_needed
            -> "move_existing_plus_new",
               move=total_available, new=needed - total_available
        total_available == 0
            -> "import_new", move=0, new=needed

    For cards whose ``scryfall_id`` isn't yet in the local ``Card`` catalog,
    the output row has ``card_id=None``, empty ``matches``, and
    ``recommended_action="import_new"``. The existing import flow will
    resolve+create the ``Card`` during commit.

    Performance: one query for Card-id resolution + one tuple-IN query for
    inventory matches + one StorageLocation outerjoin in the same query.
    No N+1 — ~100 parsed rows touch the DB twice total.

    Precursor verification notes (captured during Session 1 for future
    readers):

    Drawer-sorter excludes deck rows.
        ``app/inventory_service.py::resort_collection`` (line 1164-1168)
        applies an outerjoin + ``or_(is None, type != 'deck')`` filter that
        keeps deck-located rows out of the auto-sort. The same exclusion is
        mirrored in ``list_pending_rows`` (line 840-853). Reconciliation
        moves into a deck are therefore safe — the next auto-sort run will
        not pull them back. Both rules date to v3.11.3 along with a
        migration that scrubbed any rows previously stuck in the wrong
        state.

    Source-row tags are dropped by ``pull_card_to_deck`` today (two flavors,
    tracked separately):
        1. Pull-then-delete (``app/deck_service.py`` line 1168-1169): when
           the source ``InventoryRow.quantity`` reaches zero, the row is
           ``session.delete``'d. Its ``tags`` JSON goes with it. Total
           data loss for the moved copies.
        2. Pull-with-remainder (``app/deck_service.py`` line 1150-1163):
           when a new deck-side row is created, ``InventoryRow(...)``
           assigns ``user_id``, ``card_id``, ``storage_location_id``,
           ``finish``, ``quantity``, ``drawer``, ``slot``, ``is_pending``,
           and timestamps — but never ``tags``. The destination row starts
           with ``tags=NULL`` even if the source row had a meaningful tag
           set. Tag discontinuity between source and destination.

        This function reports the source row's ``tags`` in each match's
        payload so the eventual fix (separate ticket) has all the data it
        needs at the moment of the move. The reconciliation function itself
        performs no moves — those go through ``pull_card_to_deck`` (or its
        successor) in Session 3.
    """
    # Validate the deck exists and belongs to this user. Match the style of
    # other deck_service functions that raise ValueError for ownership /
    # not-found conditions.
    deck = session.query(Deck).filter(Deck.id == deck_id, Deck.user_id == user_id).first()
    if deck is None:
        raise ValueError("deck not found")
    target_storage_location_id = deck.storage_location_id

    if not parsed_rows:
        return []

    # Resolve scryfall_ids → card_ids in one query. Skip rows with no
    # scryfall_id (the import preview shouldn't pass these through but the
    # function is defensive).
    scryfall_ids = sorted({r.get("scryfall_id") for r in parsed_rows if r.get("scryfall_id")})
    card_by_sid: dict[str, int] = {}
    if scryfall_ids:
        for card_row in (
            session.query(Card.id, Card.scryfall_id)
            .filter(Card.scryfall_id.in_(scryfall_ids))
            .all()
        ):
            card_by_sid[card_row.scryfall_id] = card_row.id

    # Build the set of (card_id, finish) tuples we need to look up.
    lookup_keys: set[tuple[int, str]] = set()
    for r in parsed_rows:
        sid = r.get("scryfall_id")
        if not sid:
            continue
        card_id = card_by_sid.get(sid)
        if card_id is None:
            continue
        finish = (r.get("finish") or "normal").strip().lower()
        lookup_keys.add((card_id, finish))

    # One tuple-IN query for all matching inventory rows + their storage
    # locations. Outerjoin so pending rows (storage_location_id IS NULL)
    # come through with loc=None.
    #
    # Rows are bucketed into three lists per (card_id, finish):
    #   matches             — non-deck rows. Eligible to MOVE into the target
    #                         deck (this is the existing "movable inventory"
    #                         list that drives recommended_action).
    #   target_deck_matches — rows in the destination deck itself. Used by
    #                         the commit handler to auto-merge import_new
    #                         copies into the existing deck row instead of
    #                         creating a duplicate.
    #   other_deck_matches  — rows in any OTHER deck-type location. Surfaced
    #                         in the UI as informational ("In another deck"),
    #                         not auto-moved by default (per §3.3 — don't
    #                         silently cannibalize a different deck).
    matches_by_key: dict[tuple[int, str], list[dict]] = {key: [] for key in lookup_keys}
    target_deck_by_key: dict[tuple[int, str], list[dict]] = {key: [] for key in lookup_keys}
    other_deck_by_key: dict[tuple[int, str], list[dict]] = {key: [] for key in lookup_keys}
    if lookup_keys:
        rows = (
            session.query(InventoryRow, StorageLocation)
            .outerjoin(
                StorageLocation,
                InventoryRow.storage_location_id == StorageLocation.id,
            )
            .filter(
                InventoryRow.user_id == user_id,
                tuple_(InventoryRow.card_id, InventoryRow.finish).in_(list(lookup_keys)),
            )
            .all()
        )
        for row, loc in rows:
            if loc is None:
                location_name = "Pending"
                location_type = "pending"
            else:
                location_name = loc.name
                location_type = loc.type
            entry = {
                "inventory_row_id": row.id,
                "location_name": location_name,
                "location_type": location_type,
                "quantity_available": row.quantity,
                "tags": get_row_tags(row),
            }
            key = (row.card_id, row.finish)
            if loc is not None and loc.type == "deck":
                if row.storage_location_id == target_storage_location_id:
                    target_deck_by_key[key].append(entry)
                else:
                    other_deck_by_key[key].append(entry)
            else:
                matches_by_key[key].append(entry)

    # Sort each per-key match list by tier then row id.
    for match_list in matches_by_key.values():
        match_list.sort(
            key=lambda m: (
                _RECONCILE_TIER_PRIORITY.get(m["location_type"], 99),
                m["inventory_row_id"],
            )
        )
    # Deck-located match lists need only stable-by-id ordering (tier is always
    # "deck", so the tier comparison is a no-op).
    for match_list in target_deck_by_key.values():
        match_list.sort(key=lambda m: m["inventory_row_id"])
    for match_list in other_deck_by_key.values():
        match_list.sort(key=lambda m: m["inventory_row_id"])

    # Build per-parsed-row output in input order.
    output: list[dict] = []
    for r in parsed_rows:
        sid = r.get("scryfall_id") or ""
        card_id = card_by_sid.get(sid) if sid else None
        finish = (r.get("finish") or "normal").strip().lower()
        quantity_needed = max(1, int(r.get("quantity") or 1))
        line_number = r.get("line_number")

        if card_id is None:
            output.append(
                {
                    "line_number": line_number,
                    "card_id": None,
                    "scryfall_id": sid,
                    "finish": finish,
                    "quantity_needed": quantity_needed,
                    "matches": [],
                    "target_deck_matches": [],
                    "other_deck_matches": [],
                    "total_available": 0,
                    "total_in_target_deck": 0,
                    "total_in_other_decks": 0,
                    "recommended_action": "import_new",
                    "recommended_move_qty": 0,
                    "recommended_new_qty": quantity_needed,
                }
            )
            continue

        key = (card_id, finish)
        matches = matches_by_key.get(key, [])
        target_deck_matches = target_deck_by_key.get(key, [])
        other_deck_matches = other_deck_by_key.get(key, [])
        total_available = sum(m["quantity_available"] for m in matches)
        total_in_target_deck = sum(m["quantity_available"] for m in target_deck_matches)
        total_in_other_decks = sum(m["quantity_available"] for m in other_deck_matches)

        # recommended_action / move_qty / new_qty are driven by `matches`
        # (non-deck rows) ONLY. Deck-located rows never auto-move by
        # default — they're informational. The commit handler uses
        # target_deck_matches separately to merge new imports into an
        # existing deck row rather than create a duplicate.
        if total_available >= quantity_needed:
            action = "move_existing"
            move_qty = quantity_needed
            new_qty = 0
        elif total_available > 0:
            action = "move_existing_plus_new"
            move_qty = total_available
            new_qty = quantity_needed - total_available
        else:
            action = "import_new"
            move_qty = 0
            new_qty = quantity_needed

        output.append(
            {
                "line_number": line_number,
                "card_id": card_id,
                "scryfall_id": sid,
                "finish": finish,
                "quantity_needed": quantity_needed,
                "matches": matches,
                "target_deck_matches": target_deck_matches,
                "other_deck_matches": other_deck_matches,
                "total_available": total_available,
                "total_in_target_deck": total_in_target_deck,
                "total_in_other_decks": total_in_other_decks,
                "recommended_action": action,
                "recommended_move_qty": move_qty,
                "recommended_new_qty": new_qty,
            }
        )

    return output


def list_user_printings_for_card(session: Session, user_id: int, card_name: str) -> list[dict]:
    """Aggregate the user's owned rows for a given card name.

    Used by the Switch Printing modal — the "In your collection" section
    sorts owned printings to the top so the user picks what they actually
    have first (the source-of-truth positioning the roadmap calls out).

    Returns one entry per (set_code, collector_number, finish) combo, with
    aggregate `quantity` summed across however many rows hold that exact
    printing+finish (deck rows, drawer rows, pending rows all count). Each
    entry also carries a brief `locations` list — short labels describing
    where the user has those copies, used by the modal to show "2 in
    deck, 1 in Binder A". Sorted with deck rows first (most likely to
    swap right back into the deck) then non-deck rows, both by set_code +
    collector_number for stable presentation.
    """
    cleaned = (card_name or "").strip()
    if not cleaned:
        return []

    rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
        .join(Card)
        .filter(
            InventoryRow.user_id == user_id,
            Card.name == cleaned,
        )
        .all()
    )

    buckets: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (
            (r.card.set_code or "").lower(),
            (r.card.collector_number or ""),
            (r.finish or "normal").lower(),
        )
        entry = buckets.get(key)
        if entry is None:
            entry = {
                "set_code": key[0],
                "set_name": r.card.set_name,
                "collector_number": key[1],
                "finish": key[2],
                "scryfall_id": r.card.scryfall_id,
                "image_url": r.card.image_url,
                "quantity": 0,
                "locations": [],
                "in_deck": False,
            }
            buckets[key] = entry
        entry["quantity"] += int(r.quantity or 0)
        loc = r.storage_location
        if loc is not None:
            label = loc.name
            if loc.type == "deck":
                entry["in_deck"] = True
                label = f"Deck: {loc.name}"
            entry["locations"].append(label)
        elif r.is_pending:
            entry["locations"].append("Pending")

    # Stable order: deck-located rows first (a deck swap to a printing the
    # user already has in *another* deck is the very-most-common case), then
    # everything else by set_code + collector_number.
    def _sort_key(entry: dict) -> tuple:
        return (
            0 if entry["in_deck"] else 1,
            entry["set_code"],
            entry["collector_number"],
            entry["finish"],
        )

    return sorted(buckets.values(), key=_sort_key)


def switch_deck_row_printing(
    session: Session,
    user_id: int,
    deck_id: int,
    row_id: int,
    new_scryfall_id: str,
    new_finish: str,
) -> bool:
    """Swap the printing on an existing deck row in place.

    Preserves the row's id, quantity, tags, role, and notes — only the
    `card_id` and `finish` change. The card's place in the deck stays
    fixed and downstream analytics that key on row id (e.g. cached panel
    fragments) continue to address the same row.

    `new_scryfall_id` must already exist as a Card in the local DB. The
    caller is responsible for fetching+upserting it from Scryfall first
    (the route handler does this via `get_or_create_card`).

    Returns True on success, False if the row, deck, or new card can't
    be resolved or if the deck doesn't belong to this user.
    """
    from app.inventory_service import get_or_create_card  # local import: avoid cycle

    deck = session.query(Deck).filter(Deck.id == deck_id, Deck.user_id == user_id).first()
    if not deck or not deck.storage_location_id:
        return False

    row = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.id == row_id,
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id == deck.storage_location_id,
        )
        .first()
    )
    if not row:
        return False

    new_card = get_or_create_card(session, new_scryfall_id)
    if not new_card:
        return False

    finish_clean = (new_finish or "normal").strip().lower()
    if finish_clean not in {"normal", "foil", "etched"}:
        finish_clean = "normal"

    row.card_id = new_card.id
    row.finish = finish_clean
    row.updated_at = datetime.utcnow()
    session.commit()
    return True


def bump_deck_row_quantity(
    session: Session, user_id: int, deck_id: int, row_id: int, delta: int
) -> dict:
    """Bump (or decrement) a deck row's quantity by ``delta`` (±1).

    Powers the basic-land +/- controls on the deck-detail page. Restricted
    to deck-located rows owned by this user. When the new quantity reaches
    zero the row is deleted entirely; the caller's UI is expected to
    re-render the deck card list after.

    Returns ``{"row_id": id, "quantity": new_qty, "deleted": bool}`` so
    the caller can decide whether to swap the row out of the rendered
    list. Empty dict on validation failure (row not found, not in deck,
    not owned by user) so the caller can 404.

    v1 keeps this simple — no cross-row inventory accounting. A `+` just
    bumps the deck row's quantity by 1; user is responsible for physically
    placing the additional copy.
    """
    if delta not in (-1, 1):
        return {}

    deck = session.query(Deck).filter(Deck.id == deck_id, Deck.user_id == user_id).first()
    if not deck or not deck.storage_location_id:
        return {}

    row = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.id == row_id,
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id == deck.storage_location_id,
        )
        .first()
    )
    if not row:
        return {}

    new_qty = (row.quantity or 0) + delta
    if new_qty <= 0:
        session.delete(row)
        session.commit()
        return {"row_id": row_id, "quantity": 0, "deleted": True}

    row.quantity = new_qty
    row.updated_at = datetime.utcnow()
    session.commit()
    return {"row_id": row_id, "quantity": new_qty, "deleted": False}


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
        # Existing deck row keeps its own tags — that's the established
        # per-deck role context. Source tags (if any) reflect a different
        # location's context and shouldn't pollute it.
        existing_deck_row.quantity += quantity
        existing_deck_row.updated_at = datetime.utcnow()
    else:
        # No deck row yet — inherit the source row's tags so role context
        # carries into the deck (closes the v3.16.13/14-era tag-loss bug
        # documented in `pull_card_to_deck` source: both pull-then-delete
        # and pull-with-remainder dropped tags before this fix). User can
        # edit on the new deck row if the role differs in this deck.
        existing_deck_row = InventoryRow(
            user_id=user_id,
            card_id=row.card_id,
            storage_location_id=deck.storage_location_id,
            finish=row.finish,
            quantity=quantity,
            drawer=None,
            slot=None,
            is_pending=False,
            tags=row.tags,
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
