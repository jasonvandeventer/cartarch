from __future__ import annotations

import json
import re

from sqlalchemy import func, text
from sqlalchemy.orm import Session, joinedload

from app.audit_service import log_transaction
from app.models import (
    Card,
    Deck,
    DeckCardShare,
    Game,
    GameSeat,
    InventoryRow,
    StorageLocation,
    VariantGroup,
)
from app.scryfall import _cache_get_by_ids, extract_token_stubs, fetch_deck_tokens
from app.timeutil import utc_now

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
    # "Draw cards equal to that creature's power" — Greater Good, Life's Legacy.
    # No quantifier between "draw" and "cards"; the existing pattern requires one.
    r"|\bdraws? cards? equal to\b"
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
    r"|deals \d+ damage to each (?:creature|other creature)"
    r"|\boverload \{"
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
# Engine recognizers. The first three were the original v3.x set:
#   1) Sac outlets — activation cost includes sacrifice, colon-delimited. The
#      colon avoids matching Bargain-style cast costs ("sacrifice X as you cast
#      this spell").
#   2) Graveyard recursion — Reanimate / Sun Titan / Phyrexian Reclamation
#      shape.
#   3) Recursion via "creature cards in your graveyard" subject — Victimize,
#      Twilight's Call, Living Death.
# v3.23.4 audit-driven additions (#3 in docs/tag_audit_findings.md):
#   4) Cast-trigger token engines — Primeval Bounty / Mondrak ("Whenever you
#      cast a … spell, create a … token").
#   5) ETB-trigger token engines — Tireless Provisioner / Hangarback Walker
#      ("Whenever a land enters under your control, create a Treasure token").
#   6) Free-cast effects — Sunbird's Invocation / Bolas's Citadel ("you may
#      cast a … without paying its mana cost"). The subject restriction
#      (`that|a|an|target|the chosen|those|them`) avoids matching Force of
#      Will-style alternate costs where the subject is the spell itself.
#   7) Trigger-based draw on creature death — Grim Haruspex / Dark Prophecy
#      ("Whenever … creature … dies … draw a card"). Distinct from Skullclamp
#      (one-time "When" trigger on a specific creature, already gets Draw).
#   8) Non-graveyard recursion — Luminous Broodmoth / Adarkar Valkyrie
#      ("Whenever … creature … dies … return … to the battlefield"). One-shot
#      reanimate spells (Reanimate, Animate Dead) use "Return target creature
#      card from your graveyard" wording — those match (2) above, not (8).
#   9) Conditional repeating tap-to-draw — Idol of Oblivion ("{T}: Draw a card.
#      Activate this ability only if …"). v3.36.10 fix: docs/tag_audit_findings
#      Pattern C #3 was recommended but never shipped (only 5 of 6 landed). The
#      gated "Activate … only if" reading is what distinguishes a repeatable
#      engine from a one-shot "{T}: Draw a card" cantrip.
_ENGINE_RE = re.compile(
    r"\bsacrifice (?:a|an|another)\s+(?:[\w'-]+\s+){0,3}(?:creature|permanent|artifact|token|treasure|food|clue|blood)\s*:"
    r"|(?:return|put) [^.]{0,80}from [^.]{0,30}graveyards?[^.]{0,30}(?:to|onto) the battlefield"
    r"|(?:creature|permanent) cards? in your graveyard.{0,80}return [^.]{0,80}(?:to|onto) the battlefield"
    r"|\bwhenever you cast (?:a|an|your)[^.]{0,40}\bcreate\b"
    r"|\bwhenever (?:a|an|another)[^.]{0,40}\benters\b[^.]{0,40}\bcreate\b"
    r"|\b(?:cast|play) (?:that|a|an?|the chosen|target|those|them)[^.]{0,80}without paying (?:its|their) mana costs?\b"
    r"|\bwhenever [^.]{0,40}creatures?[^.]{0,40}\bdies\b[^.]{0,60}draw (?:a card|\w+ cards?|cards? equal)"
    r"|\bwhen(?:ever)? [^.]{0,40}\bcreature[^.]{0,40}\bdies\b[^.]{0,60}return [^.]{0,40}to the battlefield"
    r"|\{[t0-9wubrgcxs,/\s]+\}:\s*draw a card\.\s*activate (?:this ability )?only if",
    re.IGNORECASE,
)
# Hard threat indicators only. Power/toughness aren't in the Card model so
# soft "this is a big creature" threats are missed by design — manual tagging.
# Per-trigger drains ("each opponent loses 1 life") and free-cast tutors
# ("cast without paying") were dropped — too noisy.
# v3.23.4 audit-driven additions (#4 in docs/tag_audit_findings.md):
#   - Damage doublers — Gratuitous Violence, Furnace of Rath, Fiery
#     Emancipation, Wound Reflection ("deals double that damage", "double the
#     damage").
#   - P/T doublers — Unnatural Growth ("double the power and toughness").
#     The "(?:power and toughness|power|toughness)" suffix excludes
#     "double the amount of mana" (Doubling Cube — ramp, not threat).
#   - ETB damage equal to power — Terror of the Peaks / Warstorm Surge. The real
#     oracle wording is "deals damage equal to <its / that creature's> power to
#     any target", so the clause anchors on `enters … deals damage equal to …
#     power` (v3.36.10 fix: the prior `damage to <target> … equal to … power`
#     ordering had the words in the wrong order and matched NEITHER card it was
#     added for). The `enters` + `equal to … power` shape keeps Lightning-Bolt-
#     style fixed direct-damage spells from false-positiving.
_THREAT_RE = re.compile(
    r"\byou win the game\b"
    r"|(?:target |each |that )?(?:opponents?|players?)\s+loses? the game\b"
    r"|\binfect\b"
    r"|\btoxic \d+\b"
    r"|\bextra (?:combat phase|turn)\b"
    r"|\bdeals? double that (?:much )?damage\b"
    r"|\b(?:double|triple) (?:the |that )?(?:amount of )?damage\b"
    r"|\bdouble the (?:power and toughness|power|toughness)\b"
    r"|\bwhenever [^.]{0,80}\benters\b[^.]{0,80}\bdeals? damage equal to [^.]{0,40}\bpower\b",
    re.IGNORECASE,
)
# Death-trigger drain payoffs — Blood Artist, Zulaport Cutthroat, Syr Konrad,
# Vindictive Vampire, Bastion of Remembrance, Mirkwood Bats, etc. These are the
# deliberate wincons in aristocrats decks: every creature death pings opponents
# until they die. Pattern: "whenever <anything> creature <anything> dies
# <anything> <drain consequence>". Trigger must contain both "creature" and
# "dies" so ETB pingers (Impact Tremors), attack/combat triggers, and life-gain
# triggers (Marauding Blight-Priest) don't false-positive into Threat. The
# `[^.]` clamp keeps matches within a single sentence.
_DEATH_TRIGGER_DRAIN_RE = re.compile(
    r"whenever [^.]{0,80}\bcreature[^.]{0,40}\bdies\b[^.]{0,80}"
    r"(?:deals? \d+ damage to (?:each opponent|target|any target)"
    r"|each opponent loses \d+ life"
    r"|target opponent loses \d+ life"
    r"|target player loses \d+ life)",
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

# Tag-detail schema (v3.22.0 onward):
#   InventoryRow.tags stores a JSON list of dicts:
#     [{"tag": "Ramp", "confidence": "medium", "source": "auto"}, ...]
#   Older rows store the legacy shape (list[str]) until the migration runs,
#   and `get_row_tags` / `get_row_tag_details` read both shapes transparently.
#
# Valid confidence values:
#   - "certain"  — unambiguous oracle-text rule (e.g. "Search your library
#                  for any card" → Tutor). Reserved for pattern-specific
#                  high-confidence rules (Session 2 will assign these).
#   - "high"     — user-confirmed, OR a community-consensus pattern.
#   - "medium"   — auto-tagger guess, context-dependent.
#   - "low"      — weak match; downstream consumers should ignore by default.
#
# Valid source values:
#   - "user"     — explicit edit via the tag editor (highest authority).
#   - "auto"     — produced by `suggest_card_roles`.
TAG_CONFIDENCE_VALUES = ("certain", "high", "medium", "low")
TAG_SOURCE_VALUES = ("user", "auto")

# Numeric rank for threshold comparisons (v3.23.2). Used by
# `get_row_tags_at_or_above()` so downstream consumers can require
# tags ≥ a given confidence — filters out low-confidence noise without
# requiring user-confirmation of every auto-suggestion.
_CONFIDENCE_RANK: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "certain": 3,
}

# Per-pattern auto-tagger confidence (v3.23.2). The intrinsic regex patterns
# (Ramp via _RAMP_*, Draw via _DRAW_RE, Tutor via "search your library for",
# Removal/Wipe/Protection/Engine/Threat/Hate) are unambiguous oracle-text
# rules — when they match, the classification is essentially certain.
# Synergy via `card_matches_theme` is a heuristic over the commander themes
# and carries a meaningful false-positive risk (a tangential mention of
# "treasure" doesn't make a card a treasure-deck payoff), so it lands as
# medium until a user confirms it.
_AUTO_TAG_CONFIDENCE: dict[str, str] = {
    "Ramp": "certain",
    "Draw": "certain",
    "Tutor": "certain",
    "Removal": "certain",
    "Wipe": "certain",
    "Protection": "certain",
    "Engine": "certain",
    "Threat": "certain",
    "Hate": "certain",
    "Synergy": "medium",
}


def get_row_tags(row) -> list[str]:
    """Backward-compatible tag-name list. Reads either the legacy
    `["Ramp", "Draw"]` shape or the v3.22.0 structured shape and returns
    just the tag names. All existing consumers keep working unchanged.
    """
    return [entry["tag"] for entry in get_row_tag_details(row)]


def get_row_tags_at_or_above(row, min_confidence: str = "medium") -> list[str]:
    """Return tag names whose confidence meets or exceeds the threshold.

    Default `medium` matches the current downstream-consumer behavior
    (synergy/health/dead-cards all treat any auto-tag as load-bearing).
    Future low-confidence patterns (Session E expansions) will land at
    `low` so they don't poison analytics by default; callers can pass
    `high` to require user-confirmed (or pattern-certain) tags only.
    """
    threshold = _CONFIDENCE_RANK.get(min_confidence, 1)
    return [
        d["tag"]
        for d in get_row_tag_details(row)
        if _CONFIDENCE_RANK.get(d.get("confidence", "medium"), 1) >= threshold
    ]


def get_row_tag_details(row) -> list[dict]:
    """Full structured tag list: each entry is `{tag, confidence, source}`.

    Reads either the legacy list-of-strings shape (defaulting to
    user/high — the safest assumption for pre-migration data the user
    has been seeing in the UI without complaint) or the new dict shape.
    Unknown tag names are skipped silently.
    """
    if not row.tags:
        return []
    try:
        raw = json.loads(row.tags)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            if entry in _TAG_SET:
                out.append({"tag": entry, "confidence": "high", "source": "user"})
        elif isinstance(entry, dict):
            tag = entry.get("tag")
            if isinstance(tag, str) and tag in _TAG_SET:
                confidence = entry.get("confidence") or "medium"
                source = entry.get("source") or "auto"
                if confidence not in TAG_CONFIDENCE_VALUES:
                    confidence = "medium"
                if source not in TAG_SOURCE_VALUES:
                    source = "auto"
                out.append({"tag": tag, "confidence": confidence, "source": source})
    return out


def set_row_tags(
    row,
    tags,
    *,
    source: str = "user",
    confidence: str = "high",
) -> None:
    """Write tags to a row in the v3.22.0 structured shape.

    Accepts either:
      - list[str]    — legacy callers; each tag is wrapped with the kwargs'
        `source` + `confidence`. Default user/high matches the explicit-edit
        flow (the existing tag editor form, which still sends `list[str]`).
        Auto-tagger callers should pass `source="auto", confidence="medium"`.
      - list[dict]   — new callers; each entry's own `confidence`/`source`
        wins, falling back to the kwargs defaults if a field is missing.

    Duplicate tag names are deduplicated keeping the FIRST occurrence (so
    a caller passing a mix of dict + string forms gets a deterministic
    result). Output is sorted by tag name for stable JSON.
    """
    if source not in TAG_SOURCE_VALUES:
        source = "user"
    if confidence not in TAG_CONFIDENCE_VALUES:
        confidence = "high"

    seen: set[str] = set()
    structured: list[dict] = []
    for entry in tags or []:
        if isinstance(entry, str):
            if entry in _TAG_SET and entry not in seen:
                seen.add(entry)
                structured.append({"tag": entry, "confidence": confidence, "source": source})
        elif isinstance(entry, dict):
            tag = entry.get("tag")
            if isinstance(tag, str) and tag in _TAG_SET and tag not in seen:
                seen.add(tag)
                c = entry.get("confidence") or confidence
                s = entry.get("source") or source
                if c not in TAG_CONFIDENCE_VALUES:
                    c = confidence
                if s not in TAG_SOURCE_VALUES:
                    s = source
                structured.append({"tag": tag, "confidence": c, "source": s})

    structured.sort(key=lambda d: d["tag"])
    row.tags = json.dumps(structured) if structured else None


def add_auto_tags(row, suggested) -> bool:
    """Union new auto-tagger suggestions into a row's existing tag list.

    Preserves existing tag entries (including their confidence/source) so
    user-confirmed tags don't get downgraded on a retag pass. Newly-added
    tags use per-pattern confidence when `suggested` is `list[dict]` (the
    v3.23.2+ `suggest_card_roles_with_confidence` output); legacy `list[str]`
    callers get the old `source="auto", confidence="medium"` default for
    every entry (matches pre-v3.23.2 behavior — no regression).

    Returns True when the row's tags actually changed, False when every
    suggested tag was already present.
    """
    existing = get_row_tag_details(row)
    existing_names = {entry["tag"] for entry in existing}
    new_entries: list[dict] = []
    for entry in suggested or []:
        if isinstance(entry, str):
            if entry in _TAG_SET and entry not in existing_names:
                new_entries.append({"tag": entry, "confidence": "medium", "source": "auto"})
                existing_names.add(entry)
        elif isinstance(entry, dict):
            tag = entry.get("tag")
            if isinstance(tag, str) and tag in _TAG_SET and tag not in existing_names:
                confidence = entry.get("confidence") or "medium"
                source = entry.get("source") or "auto"
                if confidence not in TAG_CONFIDENCE_VALUES:
                    confidence = "medium"
                if source not in TAG_SOURCE_VALUES:
                    source = "auto"
                new_entries.append({"tag": tag, "confidence": confidence, "source": source})
                existing_names.add(tag)
    if not new_entries:
        return False
    set_row_tags(row, existing + new_entries)
    return True


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
    elif _DEATH_TRIGGER_DRAIN_RE.search(oracle):
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


def suggest_card_roles_with_confidence(card, themes: dict | None = None) -> list[dict]:
    """Return auto-tagger suggestions as structured `{tag, confidence, source}` dicts.

    Each tag's confidence is looked up in `_AUTO_TAG_CONFIDENCE` — intrinsic
    patterns (Ramp / Draw / Tutor / Removal / Wipe / Protection / Engine /
    Threat / Hate) are `certain` because they fire on unambiguous oracle
    text. Synergy is `medium` because it's a heuristic over commander themes
    and can produce false positives on tangential keyword mentions.

    Source is always `auto`. Callers pass the result directly to
    `set_row_tags(row, result)` — per-entry confidence/source wins over
    the function's default kwargs.

    Use this instead of `suggest_card_roles` when writing tags. Reads (the
    tag-editor suggestion checklist, audit script, etc.) keep using
    `suggest_card_roles` for just the names.
    """
    names = suggest_card_roles(card, themes=themes)
    return [
        {
            "tag": name,
            "confidence": _AUTO_TAG_CONFIDENCE.get(name, "medium"),
            "source": "auto",
        }
        for name in names
    ]


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


def get_deck_produced_tokens_for_goldfish(deck_id: int, session) -> list[dict]:
    """v3.30.21 — auto-detect produced tokens for the goldfish quick-add panel.

    Reads ``scryfall_cards.produced_tokens`` (the v3.30.11 daemon-populated
    22nd column; v3.30.19 made consumers read it locally) for every card
    in the deck. No ``DeckTokenRequirement`` curation needed. The goldfish
    quick-add panel becomes automatic for any deck whose cards have been
    backfilled by the bulk-data daemon — the common case on prod.

    Three local reads, **zero external calls** (the request-path network
    invariant from v3.25.0/v3.27.9/v3.27.13/v3.30.19 holds):

      Pass 1: collect ``Card.scryfall_id`` for every InventoryRow whose
              storage_location_id matches this deck's. The deck's cards
              live as InventoryRow rows pointing at the deck's paired
              StorageLocation per the v3.3 deck-as-StorageLocation model.
      Pass 2: read ``produced_tokens`` for each deck card via the existing
              ``_cache_get_by_ids`` helper (same primitive v3.30.19 uses).
              NULL / "[]" produced_tokens skip silently per the v3.30.11
              NULL-vs-"[]" contract.
      Pass 3: resolve token-card full details (image_url, set_code,
              collector_number, canonical name/type_line) by looking up
              each token's own ``scryfall_id``.

    Returns a sorted-by-name list of dicts in the **exact shape** the
    goldfish.py quick-add payload assembly expects, so the template
    consumes the produced-tokens output identically to the existing
    DeckTokenRequirement-derived output:

      {
        "requirement_id": None,           # not a curated requirement
        "token_name": str,
        "quantity_needed": 1,             # produced_tokens has no qty
        "type_line": str | None,
        "image_url": str | None,
        "scryfall_id": str,
        "set_code": str | None,
        "collector_number": str | None,
      }

    Deduplicated by token name; if multiple printings of the same token
    surface, prefer the entry with a non-empty image_url. Canonical
    name/type_line from the token card's own row wins over the all_parts
    stub (the stub can lag set releases).

    Pure deck_service layer: no caching (the function is called once per
    goldfish page load; profiling can add caching later if needed), no
    external calls, no database writes. ``_cache_get_by_ids`` does carry
    its own defensive try/except + degrade-to-empty pattern — a missing
    column or schema drift returns an empty list rather than 500.

    Missing prerequisites (deck doesn't exist, no paired StorageLocation,
    no cards in the deck, no producers among the cards, daemon hasn't
    backfilled the relevant cards yet) all degrade to ``[]`` — same
    graceful posture as ``fetch_deck_tokens``. The route layer combines
    this result with the existing DeckTokenRequirement-derived fallback
    so manually-tracked tokens still surface for edge cases.
    """
    deck = session.query(Deck).filter(Deck.id == deck_id).first()
    if deck is None or deck.storage_location_id is None:
        return []

    # Pass 1 — collect scryfall_ids of cards in this deck.
    scryfall_id_rows = (
        session.query(Card.scryfall_id)
        .join(InventoryRow, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.storage_location_id == deck.storage_location_id)
        .filter(Card.scryfall_id.isnot(None))
        .all()
    )
    deck_scryfall_ids = [r[0] for r in scryfall_id_rows if r[0]]
    if not deck_scryfall_ids:
        return []

    # Pass 2 — read produced_tokens for the deck cards. _cache_get_by_ids
    # batches internally and degrades gracefully on schema drift. Stub
    # parse/dedup is the shared extract_token_stubs helper.
    deck_card_payloads = _cache_get_by_ids(deck_scryfall_ids)
    token_stubs = extract_token_stubs(deck_card_payloads)

    if not token_stubs:
        return []

    # Pass 3 — resolve full token card details (image_url, set_code,
    # collector_number, canonical name/type_line). A token id missing from
    # scryfall_cards (rare — would mean the bulk export hasn't propagated
    # this token yet) falls back to the stub data with empty image_url;
    # goldfish.js's text card-face fallback handles render-time.
    token_payloads = _cache_get_by_ids([t["id"] for t in token_stubs])

    # Build per-token dicts in the goldfish quick-add shape, dedup by name
    # (multiple cards producing the same token name surface once; if
    # multiple printings exist, prefer the entry with non-empty image_url).
    by_name: dict[str, dict] = {}
    for stub in token_stubs:
        meta = token_payloads.get(stub["id"], {})
        name = (meta.get("name") or stub["name"]).strip()
        if not name:
            continue
        type_line = (meta.get("type_line") or stub["type_line"]).strip()
        entry = {
            "requirement_id": None,
            "token_name": name,
            "quantity_needed": 1,
            "type_line": type_line or None,
            "image_url": meta.get("image_url") or None,
            "scryfall_id": stub["id"],
            "set_code": meta.get("set_code") or None,
            "collector_number": meta.get("collector_number") or None,
        }
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = entry
        elif not existing.get("image_url") and entry.get("image_url"):
            by_name[name] = entry

    return sorted(by_name.values(), key=lambda t: t["token_name"])


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
        tags = get_row_tags_at_or_above(row, "medium")

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
        tags = get_row_tags_at_or_above(row, "medium")

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

# === Data-driven theme detectors (v3.23.1) — per docs/tag_system_overhaul.md §2.4 ===
#
# Each theme entry carries:
#   commander_pattern — fired against commander oracle. When it matches, the
#       theme name is added to `themes["mechanics"]` and `signal_label` is
#       appended to `themes["signals"]`.
#   card_include — fired against candidate card oracle in `card_matches_theme`.
#       Looser than commander_pattern: the commander needs a strong cue
#       ("Whenever you gain life"), but a candidate card just needs to
#       participate in the theme ("gain 1 life", "lifelink").
#   card_exclude — optional regex; when it matches, the card is rejected as
#       Synergy even if card_include matched. Per §2.5 precision work, this
#       blocks removal-on-opponent wording from polluting sacrifice/lifegain
#       synergy classification.
#   signal_label — human-readable string shown in the "Detected:" panel.
#
# Existing six mechanics (counters / tokens / graveyard / sacrifice / discard /
# death_triggers) stay as inline branches further down. They're not migrated
# here — the audit baseline is built on them, and rewriting them risks
# regression. New themes go through the data-driven path; future additions
# only need a new dict entry.
_THEME_DETECTORS: dict[str, dict] = {
    "lifegain": {
        # Commander must have an explicit lifegain-payoff trigger. Bare
        # "lifelink" on tokens (Teysa) is NOT a deckbuilding-around-lifegain
        # signal — it's an incidental keyword grant. The lifegain theme should
        # only fire for Soul-Sisters / Karlov / Trelasarra-style commanders
        # that deliberately reward life gained as a recurring effect.
        "commander_pattern": re.compile(
            r"\bwhenever you gain (?:any amount of )?life\b"
            r"|\byou gained \d+ or more life\b"
            r"|\bfor each \d+ life you gained\b",
            re.IGNORECASE,
        ),
        "card_include": re.compile(
            r"\bgains? \d+ life\b"
            r"|\bwhenever you gain (?:any amount of )?life\b"
            r"|\bif you would gain life\b"
            r"|\blifelink\b"
            r"|\bgain life equal to\b",
            re.IGNORECASE,
        ),
        # Pure opponent-life-loss effects ("each opponent loses N life") aren't
        # lifegain synergy — they're drain wincons captured by Threat already.
        # Allow co-mention (Blood Artist style: "target player loses 1 life
        # AND you gain 1 life") via the include pattern still matching.
        "card_exclude": None,
        "signal_label": "lifegain",
    },
    "food": {
        "commander_pattern": re.compile(r"\bfoods?\b", re.IGNORECASE),
        "card_include": re.compile(r"\bfoods?\b", re.IGNORECASE),
        "card_exclude": None,
        "signal_label": "food",
    },
    "treasure": {
        "commander_pattern": re.compile(r"\btreasures?\b", re.IGNORECASE),
        "card_include": re.compile(r"\btreasures?\b", re.IGNORECASE),
        "card_exclude": None,
        "signal_label": "treasure",
    },
    "clue": {
        # Plural form matters for commanders like Lonis ("Sacrifice X clues").
        # Card-side also catches "investigate" since investigate creates a Clue
        # token and is the canonical signal for Clue-theme participation.
        "commander_pattern": re.compile(r"\bclues?\b|\binvestigate\b", re.IGNORECASE),
        "card_include": re.compile(r"\bclues?\b|\binvestigate\b", re.IGNORECASE),
        "card_exclude": None,
        "signal_label": "clue",
    },
    "blood": {
        # "Blood token" specifically — "blood" alone is too noisy
        # (Vampires / "blood counter" / lore text "blood of his enemies").
        "commander_pattern": re.compile(r"\bblood tokens?\b", re.IGNORECASE),
        "card_include": re.compile(r"\bblood tokens?\b", re.IGNORECASE),
        "card_exclude": None,
        "signal_label": "blood",
    },
    "ring": {
        "commander_pattern": re.compile(
            r"\bthe ring tempts you\b|\bring-bearer\b|\byour ring-bearer\b",
            re.IGNORECASE,
        ),
        "card_include": re.compile(
            r"\bthe ring tempts you\b|\bring-bearer\b",
            re.IGNORECASE,
        ),
        "card_exclude": None,
        "signal_label": "ring",
    },
    "minus_one_counters": {
        "commander_pattern": re.compile(r"-1/-1 counter", re.IGNORECASE),
        "card_include": re.compile(r"-1/-1 counter", re.IGNORECASE),
        "card_exclude": None,
        "signal_label": "-1/-1 counters",
    },
    "spells_cast": {
        "commander_pattern": re.compile(
            r"\bwhenever you cast (?:a|an|your)\b"
            r"|\bspells you cast cost\b"
            r"|\bstorm\b"
            r"|\bprowess\b"
            r"|\bmagecraft\b",
            re.IGNORECASE,
        ),
        "card_include": re.compile(
            r"\bwhenever you cast (?:a|an|your)\b"
            r"|\bspells you cast cost\b"
            r"|\bstorm\b"
            r"|\bprowess\b"
            r"|\bmagecraft\b"
            r"|\bcopy that spell\b"
            r"|\bwhenever you cast (?:a|an|your)[^.]{0,40}spell\b",
            re.IGNORECASE,
        ),
        "card_exclude": None,
        "signal_label": "spells you cast",
    },
    "landfall": {
        "commander_pattern": re.compile(
            r"\blandfall\b"
            r"|\bwhenever a land enters\b"
            r"|\bwhenever a land you control enters\b"
            r"|\blands you control enter\b",
            re.IGNORECASE,
        ),
        "card_include": re.compile(
            r"\blandfall\b"
            r"|\bwhenever a land (?:you control )?enters\b"
            r"|\blands you control enter\b"
            r"|\bplay an additional land\b"
            r"|\bput[^.]{0,30}\bland[^.]{0,40}onto the battlefield\b",
            re.IGNORECASE,
        ),
        "card_exclude": None,
        "signal_label": "landfall",
    },
    "attack": {
        # Wider than "Whenever a creature attacks" — commanders also use:
        #   "Whenever <name> attacks" (Magda)
        #   "When <name> enters or attacks" (Sam, Loyal Attendant)
        #   "Whenever you attack" (newer wording)
        # The 0-80 char window between when(ever) and "attacks" allows any
        # subject phrasing while staying inside a single sentence.
        "commander_pattern": re.compile(
            r"\bwhen(?:ever)? [^.;]{0,80}\battacks?\b"
            r"|\battacking creatures? (?:you control )?(?:gets?|have)\b"
            r"|\battacking creatures? you control\b"
            r"|\bcreatures? you control have menace\b",
            re.IGNORECASE,
        ),
        "card_include": re.compile(
            r"\bwhen(?:ever)? [^.;]{0,80}\battacks?\b"
            r"|\battacking creatures? (?:you control )?gets?\b"
            r"|\bcreatures? you control (?:have menace|have trample|have double strike|gain trample)\b"
            r"|\b(?:can'?t|cannot) be blocked\b",
            re.IGNORECASE,
        ),
        "card_exclude": None,
        "signal_label": "attack triggers",
    },
    "blocking": {
        "commander_pattern": re.compile(
            r"\bwhenever (?:a |\w+ )?creatures? (?:you control )?blocks?\b"
            r"|\bwhenever (?:a |\w+ )?creatures? (?:you control )?is blocked\b",
            re.IGNORECASE,
        ),
        "card_include": re.compile(
            r"\bwhenever (?:a |\w+ )?creatures? (?:you control )?blocks?\b"
            r"|\b(?:can(?: also)?|may) block (?:an additional|two|any number)\b",
            re.IGNORECASE,
        ),
        "card_exclude": None,
        "signal_label": "blocking",
    },
    "equip": {
        "commander_pattern": re.compile(
            r"\bequipped creature\b"
            r"|\bequipment you control\b"
            r"|\battached creature\b"
            r"|\bwhenever equipment becomes attached\b",
            re.IGNORECASE,
        ),
        "card_include": re.compile(
            r"\bequipment\b" r"|\bequipped creature\b" r"|\bequip[ —]\{",
            re.IGNORECASE,
        ),
        "card_exclude": None,
        "signal_label": "equip",
    },
    "aura": {
        "commander_pattern": re.compile(
            r"\benchanted creature\b"
            r"|\bauras? you control\b"
            r"|\bwhenever an aura\b"
            r"|\benchant creature\b",
            re.IGNORECASE,
        ),
        "card_include": re.compile(
            r"\bauras?\b" r"|\benchanted creature\b" r"|\benchant creature\b",
            re.IGNORECASE,
        ),
        "card_exclude": None,
        "signal_label": "aura",
    },
    "energy": {
        # Energy counters use the {E} mana-symbol-style notation. Match either
        # the spelled-out form or the symbol.
        "commander_pattern": re.compile(r"\benergy counter\b|\{e\}", re.IGNORECASE),
        "card_include": re.compile(r"\benergy counter\b|\{e\}", re.IGNORECASE),
        "card_exclude": None,
        "signal_label": "energy",
    },
    "saga": {
        # "Saga" appears in the type line of Saga cards, but commanders that
        # care about sagas mention them in oracle text.
        "commander_pattern": re.compile(r"\bsagas?\b|\blore counters?\b", re.IGNORECASE),
        "card_include": re.compile(r"\bsagas?\b|\blore counters?\b", re.IGNORECASE),
        "card_exclude": None,
        "signal_label": "saga",
    },
}


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

        # Counter-type specificity (v3.23.5). Generic "counters" above stays for
        # back-compat with non-P/T counter themes (charge / loyalty / time / etc.)
        # — these specific themes add a parallel classification so cluster
        # matching in card_matches_theme can hit related keyword mechanics.
        if re.search(r"\+1/\+1 counters?\b", oracle):
            mechanics.add("plus_one_plus_one_counters")
            signals.append("+1/+1 counters")
        if re.search(r"-1/-1 counters?\b", oracle):
            mechanics.add("minus_one_minus_one_counters")
            signals.append("-1/-1 counters")

        # Counter-related keywords (v3.23.5). Each implies a specific counter
        # cluster: proliferate touches any existing counter (broad);
        # wither/infect/blight write -1/-1 counters; undying writes +1/+1
        # counters; persist writes -1/-1 (and synergizes with +1/+1 via
        # cancellation, hence cross-cluster matching in card_matches_theme).
        if re.search(r"\bproliferate\b", oracle):
            mechanics.add("proliferate")
            signals.append("proliferate")
        if re.search(r"\bwither\b", oracle):
            mechanics.add("wither")
            signals.append("wither")
        if re.search(r"\binfect\b", oracle):
            mechanics.add("infect")
            signals.append("infect")
        if re.search(r"\bblight \d+\b", oracle):
            mechanics.add("blight")
            signals.append("blight")
        if re.search(r"\bpersist\b", oracle):
            mechanics.add("persist")
            signals.append("persist")
        if re.search(r"\bundying\b", oracle):
            mechanics.add("undying")
            signals.append("undying")

        # === v3.23.5: narrow Gorma-style lifegain rule (Option C) ===
        # Adds the `lifegain` theme ONLY when the commander has BOTH:
        #   (1) "lifelink" as a bare keyword on itself (not a grant to tokens
        #       or creatures — Teysa Karlov says "tokens have lifelink", which
        #       must NOT trigger this).
        #   (2) a self-referential death or sacrifice trigger (Whenever a
        #       creature you control dies / Whenever you sacrifice).
        # The pair is the structural signature of a Gorma / Karlov of the
        # Ghost Council / Trostani Discordant-style lifegain payoff deck.
        # Bare lifelink alone (vanilla beater) is not enough; a self-death
        # trigger alone (aristocrats) is not enough.
        bare_lifelink_re = re.compile(
            r"(?<!have )(?<!gain )(?<!with )(?<!give )(?<!gains )(?<!have\n)\blifelink\b",
            re.IGNORECASE,
        )
        # Manual word-before check covers variable-length prefixes ("creatures
        # you control have lifelink", "tokens have lifelink", etc.) that
        # fixed-width lookbehinds can't express.
        grant_prefixes = {
            "have",
            "gain",
            "gains",
            "with",
            "give",
            "creatures",
            "tokens",
            "permanents",
            "they",
            "and",
        }
        has_bare_lifelink = False
        for m in bare_lifelink_re.finditer(oracle):
            before = oracle[: m.start()].rstrip()
            words = re.split(r"\W+", before) if before else []
            last_word = words[-1].lower() if words else ""
            if last_word not in grant_prefixes:
                has_bare_lifelink = True
                break
        self_death_or_sac_re = re.compile(
            r"\bwhenever (?:another |a )?(?:nontoken )?(?:creature|permanent)[^.]{0,40}you control[^.]{0,30}\bdies\b"
            r"|\bwhenever you sacrifice\b",
            re.IGNORECASE,
        )
        if has_bare_lifelink and self_death_or_sac_re.search(oracle):
            mechanics.add("lifegain")
            signals.append("lifegain (Gorma-style)")

        # Data-driven theme detection (v3.23.1) — see _THEME_DETECTORS docstring.
        for theme_name, det in _THEME_DETECTORS.items():
            if det["commander_pattern"].search(oracle):
                mechanics.add(theme_name)
                signals.append(det["signal_label"])

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
    # Death-triggers decks synergize with cards that force sacrifices — every
    # forced sac causes a death trigger. Catches Demon's Disciple, Plaguecrafter,
    # Fleshbag Marauder. Restricted to forced-sac wording ("each player /
    # opponent / target opponent sacrifices …") so self-sac costs on your own
    # cards don't double-fire here (they already match via "sacrifice" mechanic
    # when extracted).
    if "death_triggers" in themes["mechanics"] and re.search(
        r"(?:each player|target opponent|opponents?)\s+sacrifices?\s+(?:a|an|another)",
        oracle,
    ):
        return True

    # === v3.23.5: counter-cluster matching ===
    # When the commander cares about a SPECIFIC counter type, the cluster
    # of related keyword mechanics also counts as Synergy.
    #
    # +1/+1 cluster: direct mentions + the keywords that grow with or
    # produce +1/+1 counters. Persist is included for the counter-cancellation
    # interaction (a persist creature carrying a -1/-1 counter can be reset
    # by adding a +1/+1 to it — Gorma-style decks deliberately exploit this).
    if "plus_one_plus_one_counters" in themes["mechanics"]:
        if re.search(r"\+1/\+1 counters?\b", oracle):
            return True
        if re.search(r"\b(?:proliferate|undying|persist)\b", oracle):
            return True
    # -1/-1 cluster: direct mentions + keywords that write -1/-1 counters or
    # spread/exploit them. Blight N puts N -1/-1 counters per the keyword
    # (Lorwyn Eclipsed). Infect deals damage as -1/-1 counters to creatures.
    if "minus_one_minus_one_counters" in themes["mechanics"]:
        if re.search(r"-1/-1 counters?\b", oracle):
            return True
        if re.search(r"\b(?:proliferate|wither|infect|persist)\b", oracle):
            return True
        if re.search(r"\bblight \d+\b", oracle):
            return True

    # === v3.23.5: broader counter-interaction (Option B) ===
    # Counter-themed decks also want cards that manipulate counters generically
    # — counter-doublers (Ferrafor "Double the number of each kind of counter"),
    # counter-trigger payoffs (Lasting Tarfire "if you put a counter on a
    # creature"), counter-of-any-kind synergy (Puca's Covenant "creature you
    # control with a counter on it dies"), counter-removal (Eventide's Shadow
    # "Remove any number of counters"). Loyalty/lore/energy counters can
    # false-positive here but are rare in +1/+1 or -1/-1 themed decks.
    if (
        "plus_one_plus_one_counters" in themes["mechanics"]
        or "minus_one_minus_one_counters" in themes["mechanics"]
    ):
        if re.search(
            r"\bif you (?:put|move|placed?) [^.]{0,40}counters?\b"
            r"|\b(?:doubles?|twice (?:as many|the (?:number of )?))[^.]{0,40}counters?\b"
            r"|\bdouble the (?:number of )?[^.]{0,40}counters?\b"
            r"|\bremove [^.]{0,30}counters?\b"
            r"|\bmoves? [^.]{0,30}counters?\b"
            r"|\bcreatures? (?:you control )?with (?:a |\w+ )?counters?\b"
            r"|\bcreatures? with (?:a |\w+ )?counters?\b",
            oracle,
        ):
            return True

    # === v3.23.5: sac-cost recognition for death-trigger decks (Option B) ===
    # Cards like Immoral Bargain ("As an additional cost to cast this spell,
    # sacrifice X creatures") feed the death-trigger engine even though they
    # don't say "Whenever … dies" or "each player sacrifices". The existing
    # mass-edict rule only catches forced-OPPONENT sacrifices; this branch
    # catches SELF-sac as a cost.
    if "death_triggers" in themes["mechanics"]:
        if re.search(
            r"\bas an additional cost[^.]{0,80}sacrifice [^.]{0,40}creatures?\b"
            r"|\bsacrifice [\dx]+ creatures?\b",
            oracle,
        ):
            return True

    # === v3.23.5: creature-token engines in death-trigger / sacrifice decks ===
    # Cards that create creature tokens feed the death/sac engine even when
    # the card itself doesn't trigger on death. Jadar (Zombie tokens at end
    # step), Ophiomancer (Snake tokens at upkeep), Tendershoot Dryad
    # (Saprolings at upkeep), Creakwood Liege (Worm tokens at upkeep) all
    # match here.
    #
    # Magic's templating uses "creature token" specifically for creature
    # tokens; Treasure / Food / Clue / Blood / etc. never carry "creature
    # token" in their wording, so substring on "creature token" is a clean
    # discriminator. Cards that create BOTH (e.g. Tireless Provisioner ->
    # Food OR Treasure) say "Food token or a Treasure token" with no
    # "creature token", and correctly DON'T match here.
    if "death_triggers" in themes["mechanics"] or "sacrifice" in themes["mechanics"]:
        if re.search(r"\bcreates? [^.]{0,80}creature tokens?\b", oracle):
            return True
        if re.search(r"\bputs? [^.]{0,80}creature tokens? onto the battlefield\b", oracle):
            return True

    # Data-driven theme matching (v3.23.1) — handles the 15 new themes added
    # in _THEME_DETECTORS. For each theme that's in this deck's mechanics, the
    # candidate must match card_include AND not match card_exclude.
    for theme_name in themes["mechanics"]:
        det = _THEME_DETECTORS.get(theme_name)
        if not det:
            continue
        if not det["card_include"].search(oracle):
            continue
        excl = det.get("card_exclude")
        if excl is not None and excl.search(oracle):
            continue
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
        tags = get_row_tags_at_or_above(row, "medium")
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
        if get_row_tags_at_or_above(row, "medium"):
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


def create_deck(
    session: Session,
    user_id: int,
    name: str,
    format_name: str = "",
    notes: str = "",
    is_brew: bool = False,
) -> Deck:
    deck_name = name.strip()

    location = StorageLocation(
        user_id=user_id,
        name=deck_name,
        type="deck",
        parent_id=None,
        sort_order=0,
        # v3.26.2: deck locations are "manual" — the user explicitly places
        # cards via Add-to-Deck; the drawer sorter must never touch them.
        # The column default ("managed") would be semantically wrong here.
        mode="manual",
    )
    session.add(location)
    session.flush()

    deck = Deck(
        user_id=user_id,
        storage_location_id=location.id,
        name=deck_name,
        format=format_name.strip() or None,
        notes=notes.strip() or None,
        is_brew=bool(is_brew),
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
    blurb: str | None = None,
    update_blurb: bool = False,
    is_brew: bool = False,
) -> Deck:
    """Update a deck's editable attributes.

    v3.28.7 — ``blurb`` and ``update_blurb`` parameters added for the
    editorial-row Decks page's flavour-blurb sub-line. ``update_blurb``
    distinguishes "not in this form submission" (preserve stored value)
    from "explicitly cleared" (empty string clears to NULL) — same
    pattern v3.28.6 used for ``StorageLocation.note`` / ``capacity``.
    """
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
    # v3.37.0 — is_brew is always sent by the edit form (a checkbox that
    # posts "true" when checked, absent→False), so set it directly like
    # format/notes rather than via the blurb-style update flag.
    deck.is_brew = bool(is_brew)
    if update_blurb:
        deck.blurb = (blurb.strip() if blurb else None) or None

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


def list_decks_basic(session: Session, user_id: int) -> list[Deck]:
    """Lightweight deck list for destination dropdowns.

    ``list_decks`` runs ~3 queries plus combos/bracket/consistency *per
    deck* — only the /decks table needs that. Dropdown contexts
    (collection, import preview, location detail) just need id / name /
    format / storage_location_id, so this is a single query with zero
    per-deck analytics and no CommanderSpellbook network calls.
    """
    return (
        session.query(Deck)
        .options(joinedload(Deck.storage_location))
        .filter(Deck.user_id == user_id)
        .order_by(Deck.name.asc())
        .all()
    )


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
        # issue #27 — the deck card count includes inbound variant-group shares
        # (the FULL decklist). Gated on variant_group_id, so non-variant decks
        # run zero extra queries. Collection count is unaffected — shares are
        # never InventoryRows of this deck.
        deck.card_count += inbound_share_count_for_deck(session, deck)

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

        # v3.27.9: combos + bracket removed from the per-deck loop. The combo
        # path made one Spellbook /find-my-combos POST per deck on cold cache
        # (request-path network invariant violation — 14s /decks load against
        # 11 decks). The bracket display rolled up to "untrusted" until a
        # dedicated analytics rebuild lands; see the Deferred / latent items
        # entry "Deck Analytics Rebuild" in roadmap.md. compute_deck_combos is
        # deliberately left importable for the rebuild (dormant code, same
        # pattern as the retired .site-header CSS in v3.27.8). The V1
        # compute_deck_bracket estimator was deleted in the pre-v4 cleanup
        # sprint (2026-06-09); bracket_v2_service.py is the sole estimator.
        # consistency stays — it's a local computation.
        all_rows = (
            session.query(InventoryRow)
            .join(Card)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.storage_location_id == deck.storage_location_id,
            )
            .all()
        )
        deck.consistency = compute_consistency(all_rows) if all_rows else None

    return decks


def compute_deck_game_stats(session: Session, user_id: int, deck_ids: list[int]) -> dict[int, dict]:
    """Batched per-deck game stats for the v3.28.7 editorial-row Decks page.

    Returns a dict keyed by deck_id with ``games`` / ``wins`` / ``win_rate``
    / ``last_played``. Single GROUP BY query — no N+1 — matching the
    v3.28.5 ``dashboard_service.get_dashboard_data`` deck_performance
    pattern. Decks with zero games are simply absent from the returned
    dict; callers default to 0/0/0.0/None on miss.

    Filter contract: only finalized games (`Game.status == 'finalized'`)
    with placement set (`GameSeat.placement IS NOT NULL`) count toward
    games and wins. ``placement == 1`` is a win. Mirrors the dashboard
    deck-performance contract for cross-page reconciliation.

    Visibility (v4.0.1): a game counts if the viewer CREATED it
    (``Game.user_id``) OR was a player at the table (``GameSeat.user_id``) —
    matching the hybrid read-visibility added in v3.32.0. Previously only
    games the viewer created were counted, so a participant who logged no game
    saw 0 on a deck they actually played. No double-count: the query counts
    distinct ``GameSeat`` rows grouped by deck, and a deck has exactly one seat
    per game, so a viewer who is both creator and seat-holder still counts it
    once.
    """
    if not deck_ids:
        return {}

    from sqlalchemy import case, desc, func, or_

    rows = (
        session.query(
            GameSeat.deck_id.label("deck_id"),
            func.count(GameSeat.id).label("games"),
            func.sum(case((GameSeat.placement == 1, 1), else_=0)).label("wins"),
            func.max(Game.played_at).label("last_played"),
        )
        .join(Game, GameSeat.game_id == Game.id)
        .filter(
            GameSeat.deck_id.in_(deck_ids),
            or_(Game.user_id == user_id, GameSeat.user_id == user_id),
            Game.status == "finalized",
            GameSeat.placement.is_not(None),
        )
        .group_by(GameSeat.deck_id)
        .all()
    )

    # Silence the unused-import warning (desc is imported for symmetry with
    # the dashboard pattern but not actually used in this query).
    _ = desc

    out: dict[int, dict] = {}
    for r in rows:
        if r.deck_id is None:
            continue
        games = int(r.games or 0)
        wins = int(r.wins or 0)
        out[int(r.deck_id)] = {
            "games": games,
            "wins": wins,
            "losses": games - wins,
            "win_rate": (wins / games) if games > 0 else 0.0,
            "last_played": r.last_played,
        }
    return out


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


# ── Variant groups (v3.33.0) ────────────────────────────────────
# A named family of builds of the same deck that SHARE one physical copy of
# many cards. Accounting-only overlay — one-card-one-location stays intact (no
# shared pool, no row duplication, no multi-location reads). All functions are
# user-scoped + ownership-checked. See find_inventory_matches_for_deck_import
# for the one place this changes behaviour.


def create_variant_group(session: Session, user_id: int, name: str) -> VariantGroup:
    """Create a variant group. Raises ValueError on empty/duplicate name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Variant group name is required.")
    existing = (
        session.query(VariantGroup)
        .filter(VariantGroup.user_id == user_id, VariantGroup.name == name)
        .first()
    )
    if existing:
        raise ValueError(f"A variant group named '{name}' already exists.")
    group = VariantGroup(user_id=user_id, name=name[:128])
    session.add(group)
    session.commit()
    return group


def list_variant_groups(session: Session, user_id: int) -> list[VariantGroup]:
    return (
        session.query(VariantGroup)
        .filter(VariantGroup.user_id == user_id)
        .order_by(VariantGroup.name.asc())
        .all()
    )


def get_variant_group(session: Session, user_id: int, group_id: int) -> VariantGroup | None:
    return (
        session.query(VariantGroup)
        .filter(VariantGroup.id == group_id, VariantGroup.user_id == user_id)
        .first()
    )


def rename_variant_group(
    session: Session, user_id: int, group_id: int, name: str
) -> VariantGroup | None:
    """Rename a group. Returns None if not owned; ValueError on empty/duplicate."""
    group = get_variant_group(session, user_id, group_id)
    if group is None:
        return None
    name = (name or "").strip()
    if not name:
        raise ValueError("Variant group name is required.")
    clash = (
        session.query(VariantGroup)
        .filter(
            VariantGroup.user_id == user_id,
            VariantGroup.name == name,
            VariantGroup.id != group_id,
        )
        .first()
    )
    if clash:
        raise ValueError(f"A variant group named '{name}' already exists.")
    group.name = name[:128]
    session.commit()
    return group


def delete_variant_group(session: Session, user_id: int, group_id: int) -> bool:
    """Delete a group, first nulling ``variant_group_id`` on its decks.

    SQLite doesn't enforce ``ON DELETE SET NULL`` (PRAGMA foreign_keys OFF), so
    the decks are cleared explicitly (mirrors playgroup_service.delete_playgroup).
    Returns False if the group isn't owned by ``user_id``.
    """
    group = get_variant_group(session, user_id, group_id)
    if group is None:
        return False
    # issue #27 — drop every share belonging to this group first (SQLite
    # enforces no FK CASCADE; the DB CASCADE is Postgres defense-in-depth).
    session.query(DeckCardShare).filter(DeckCardShare.variant_group_id == group_id).delete(
        synchronize_session=False
    )
    session.query(Deck).filter(Deck.variant_group_id == group_id, Deck.user_id == user_id).update(
        {Deck.variant_group_id: None}, synchronize_session=False
    )
    session.delete(group)
    session.commit()
    return True


def assign_deck_variant_group(
    session: Session, user_id: int, deck_id: int, group_id: int | None
) -> Deck | None:
    """Set (or clear, when ``group_id`` is None) a deck's variant group.

    Ownership-checked on both the deck and the group. Returns None if the deck
    isn't owned; raises ValueError if ``group_id`` is given but not owned.
    """
    deck = get_deck(session, deck_id=deck_id, user_id=user_id)
    if deck is None:
        return None
    old_group_id = deck.variant_group_id
    if group_id is not None:
        if get_variant_group(session, user_id, group_id) is None:
            raise ValueError("Variant group not found.")
        deck.variant_group_id = group_id
    else:
        deck.variant_group_id = None
    # issue #27 — a deck LEAVING or SWITCHING its variant group can no longer
    # legitimately share with (or be shared by) its old siblings, so drop every
    # share touching this deck. No-op when the group is unchanged.
    if old_group_id != deck.variant_group_id:
        delete_shares_for_deck(session, deck.id)
    session.commit()
    return deck


def sibling_variant_deck_location_ids(session: Session, deck: Deck) -> list[int]:
    """StorageLocation ids of the deck's SIBLING variant decks (excludes the
    deck itself and any sibling with no storage location). Empty list when the
    deck has no variant group — the gate that keeps non-variant reconciliation
    byte-for-byte unchanged."""
    if deck.variant_group_id is None:
        return []
    return [
        loc_id
        for (loc_id,) in session.query(Deck.storage_location_id)
        .filter(
            Deck.variant_group_id == deck.variant_group_id,
            Deck.user_id == deck.user_id,
            Deck.id != deck.id,
            Deck.storage_location_id.isnot(None),
        )
        .all()
    ]


# ----------------------------------------------------------------------------
# Variant-group deck sharing — deck_card_shares (issue #27)
#
# A DeckCardShare records that a physical InventoryRow (still stored in its
# SOURCE deck's storage location — one-card-one-location PRESERVED) is ALSO a
# member of a SIBLING build's decklist within the same variant group. A
# reference, never a copy: sharing does NOT move ``storage_location_id``.
#
# Every helper short-circuits when the deck has no variant group, so decks NOT
# in a variant group run zero extra queries and are byte-for-byte unchanged.
# ----------------------------------------------------------------------------


def _deck_for_inventory_row(session: Session, user_id: int, row: InventoryRow) -> Deck | None:
    """The deck whose storage location physically holds ``row`` (or None)."""
    if row.storage_location_id is None:
        return None
    return (
        session.query(Deck)
        .filter(
            Deck.user_id == user_id,
            Deck.storage_location_id == row.storage_location_id,
        )
        .first()
    )


def share_card_to_deck(
    session: Session,
    user_id: int,
    inventory_row_id: int,
    target_deck_id: int,
) -> DeckCardShare:
    """Share an owned card (a physical InventoryRow in one deck) into a SIBLING
    build of the same variant group.

    Creates a :class:`DeckCardShare` reference — it does NOT move the row's
    ``storage_location_id`` (the source deck keeps the physical card; the
    target deck gains membership). Idempotent on ``(inventory_row_id,
    target_deck_id)`` — re-sharing returns the existing record.

    Validation (all raise ValueError):
      - the row and both decks must be owned by ``user_id``;
      - the row must physically live in one of the user's decks (its SOURCE);
      - source and target must differ (not-self);
      - source and target must be in the SAME, non-null variant group.
    """
    target_deck = get_deck(session, deck_id=target_deck_id, user_id=user_id)
    if target_deck is None:
        raise ValueError("Target deck not found.")
    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == inventory_row_id, InventoryRow.user_id == user_id)
        .first()
    )
    if row is None:
        raise ValueError("Inventory row not found.")
    source_deck = _deck_for_inventory_row(session, user_id, row)
    if source_deck is None:
        raise ValueError("That card is not in a deck.")
    if source_deck.id == target_deck.id:
        raise ValueError("A card cannot be shared to its own deck.")
    if (
        source_deck.variant_group_id is None
        or source_deck.variant_group_id != target_deck.variant_group_id
    ):
        raise ValueError("Both decks must be in the same variant group.")

    existing = (
        session.query(DeckCardShare)
        .filter(
            DeckCardShare.inventory_row_id == inventory_row_id,
            DeckCardShare.target_deck_id == target_deck_id,
        )
        .first()
    )
    if existing is not None:
        return existing

    share = DeckCardShare(
        inventory_row_id=inventory_row_id,
        source_deck_id=source_deck.id,
        target_deck_id=target_deck.id,
        variant_group_id=target_deck.variant_group_id,
    )
    session.add(share)
    session.commit()
    return share


def unshare_card_from_deck(
    session: Session,
    user_id: int,
    inventory_row_id: int,
    target_deck_id: int,
) -> bool:
    """Remove a share (the row reverts to membership in its source deck only).

    Ownership-checked via the target deck. Idempotent: returns False when no
    matching share exists. Never touches the physical row.
    """
    target_deck = get_deck(session, deck_id=target_deck_id, user_id=user_id)
    if target_deck is None:
        return False
    deleted = (
        session.query(DeckCardShare)
        .filter(
            DeckCardShare.inventory_row_id == inventory_row_id,
            DeckCardShare.target_deck_id == target_deck_id,
        )
        .delete(synchronize_session=False)
    )
    session.commit()
    return bool(deleted)


def get_inbound_shares_for_deck(session: Session, deck: Deck) -> list[dict]:
    """Cards shared INTO ``deck`` from sibling builds (read-only render data).

    Returns one dict per inbound share with the physical InventoryRow, its
    Card, and the source deck's name. Empty list for a deck with no variant
    group (the short-circuit gate). Order: card name, then row id.
    """
    if deck.variant_group_id is None:
        return []
    rows = (
        session.query(DeckCardShare, InventoryRow, Card, Deck.name)
        .join(InventoryRow, DeckCardShare.inventory_row_id == InventoryRow.id)
        .join(Card, InventoryRow.card_id == Card.id)
        .join(Deck, DeckCardShare.source_deck_id == Deck.id)
        .filter(DeckCardShare.target_deck_id == deck.id)
        .order_by(Card.name.asc(), InventoryRow.id.asc())
        .all()
    )
    return [
        {
            "share_id": share.id,
            "inventory_row": inv,
            "inventory_row_id": inv.id,
            "card": card,
            "finish": inv.finish,
            "quantity": inv.quantity,
            "is_proxy": bool(inv.is_proxy),
            "source_deck_id": share.source_deck_id,
            "source_deck_name": source_name,
        }
        for share, inv, card, source_name in rows
    ]


def inbound_shared_rows_for_deck(
    session: Session, deck: Deck, search: str | None = None
) -> list[tuple[InventoryRow, str]]:
    """The physical ``InventoryRow`` objects shared INTO ``deck`` from sibling
    builds, paired with each one's SOURCE deck name.

    Returned as ``(InventoryRow, source_deck_name)`` tuples so the deck-detail
    item builder can fold them into the SAME unified card list as the deck's own
    rows (issue #27 query semantics: a deck's card list = own rows UNION inbound
    shares, sorted/grouped together). The ``InventoryRow.card`` is eager-loaded.
    When ``search`` is given the shared rows are filtered by the SAME
    boolean/Scryfall parser as the own rows, so they filter in lock-step.

    Empty list for a deck with no variant group (the short-circuit gate), so a
    non-variant deck's grid is byte-for-byte unchanged.
    """
    if deck.variant_group_id is None:
        return []
    query = (
        session.query(InventoryRow, Deck.name)
        .options(joinedload(InventoryRow.card))
        .join(DeckCardShare, DeckCardShare.inventory_row_id == InventoryRow.id)
        .join(Deck, DeckCardShare.source_deck_id == Deck.id)
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(DeckCardShare.target_deck_id == deck.id)
    )
    if search and search.strip():
        from app.inventory_service import apply_collection_search_filters

        query = apply_collection_search_filters(query, search)
    return [(row, source_name) for row, source_name in query.all()]


def own_deck_card_options(session: Session, user_id: int, deck: Deck) -> list[dict]:
    """``{id, name, finish, role}`` for each of ``deck``'s own physically-held
    rows — the picker for the "share a card with a sibling" control in the
    deck-edit popouts (``decks.html``). Empty when the deck has no storage
    location. Ordered by card name."""
    if deck.storage_location_id is None:
        return []
    rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id == deck.storage_location_id,
        )
        .order_by(Card.name.asc(), InventoryRow.id.asc())
        .all()
    )
    return [
        {
            "id": r.id,
            "name": r.card.name if r.card else f"#{r.id}",
            "finish": r.finish,
            "role": r.role,
        }
        for r in rows
    ]


def get_outbound_shares_for_deck(session: Session, deck: Deck) -> list[dict]:
    """``{inventory_row_id, card_name, target_deck_id, target_deck_name}`` for
    every share FROM ``deck`` — the unshare list in the deck-edit popouts.
    Empty for a deck with no variant group. Ordered by card name."""
    if deck.variant_group_id is None:
        return []
    rows = (
        session.query(DeckCardShare, Card.name, Deck.name)
        .join(InventoryRow, DeckCardShare.inventory_row_id == InventoryRow.id)
        .join(Card, InventoryRow.card_id == Card.id)
        .join(Deck, DeckCardShare.target_deck_id == Deck.id)
        .filter(DeckCardShare.source_deck_id == deck.id)
        .order_by(Card.name.asc())
        .all()
    )
    return [
        {
            "inventory_row_id": share.inventory_row_id,
            "card_name": card_name,
            "target_deck_id": share.target_deck_id,
            "target_deck_name": target_name,
        }
        for share, card_name, target_name in rows
    ]


def inbound_shared_row_ids_for_deck(session: Session, deck: Deck) -> set[int]:
    """The set of InventoryRow ids shared INTO ``deck``. Empty for a deck with
    no variant group. Used by reconciliation to recognize an already-shared
    card as covered (counted ONCE alongside the sibling-location tally)."""
    if deck.variant_group_id is None:
        return set()
    return {
        rid
        for (rid,) in session.query(DeckCardShare.inventory_row_id)
        .filter(DeckCardShare.target_deck_id == deck.id)
        .all()
    }


def outbound_share_map(session: Session, deck: Deck) -> dict[int, list[str]]:
    """Map each of ``deck``'s own rows that is shared OUT to the list of target
    deck names it is shared with (for the "SHARED WITH …" badge). Empty for a
    deck with no variant group."""
    if deck.variant_group_id is None:
        return {}
    rows = (
        session.query(DeckCardShare.inventory_row_id, Deck.name)
        .join(Deck, DeckCardShare.target_deck_id == Deck.id)
        .filter(DeckCardShare.source_deck_id == deck.id)
        .order_by(Deck.name.asc())
        .all()
    )
    out: dict[int, list[str]] = {}
    for row_id, target_name in rows:
        out.setdefault(row_id, []).append(target_name)
    return out


def inbound_share_count_for_deck(session: Session, deck: Deck) -> int:
    """Total copies shared INTO ``deck`` (sum of the shared rows' quantities).

    Added to a deck's own-row count to report the FULL decklist size. The
    collection count NEVER includes this — a shared card is one physical copy.
    Zero for a deck with no variant group."""
    if deck.variant_group_id is None:
        return 0
    return (
        session.query(func.sum(InventoryRow.quantity))
        .join(DeckCardShare, DeckCardShare.inventory_row_id == InventoryRow.id)
        .filter(DeckCardShare.target_deck_id == deck.id)
        .scalar()
        or 0
    )


def delete_shares_for_deck(session: Session, deck_id: int) -> int:
    """Drop every share where ``deck_id`` is the source OR the target.

    Called when a deck is deleted or leaves/switches its variant group. SQLite
    enforces no FK CASCADE, so this is the explicit cleanup (the DB CASCADE is
    Postgres defense-in-depth). Does NOT commit — the caller owns the txn."""
    return (
        session.query(DeckCardShare)
        .filter(
            (DeckCardShare.source_deck_id == deck_id) | (DeckCardShare.target_deck_id == deck_id)
        )
        .delete(synchronize_session=False)
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


def _brew_same_name_rows(
    session: Session,
    user_id: int,
    card_names: set[str],
) -> dict[str, list[tuple]]:
    """Brew oracle-level fallback source: the user's real (non-proxy) inventory
    rows of the given card names, with StorageLocation joined, bucketed by
    lower-cased ``Card.name``.

    Keyed on name because reprints share the exact name (including full DFC
    names like "Docent of Perfection // Final Iteration") and the local catalog
    has no ``oracle_id`` column — so name is the oracle proxy, with NO schema
    change. Callers split each bucket into the UNASSIGNED pool (``loc is None``
    or ``type != 'deck'`` — claimable by a brew) vs DECK-resident copies
    (informational "owned in another deck"; never claimed per the 2026-06-11
    amendment). One query regardless of row count.
    """
    wanted = {n.lower() for n in card_names if n}
    if not wanted:
        return {}
    rows = (
        session.query(InventoryRow, StorageLocation, Card.name, Card.set_code)
        .join(Card, InventoryRow.card_id == Card.id)
        .outerjoin(StorageLocation, InventoryRow.storage_location_id == StorageLocation.id)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_proxy.is_(False),
            func.lower(Card.name).in_(wanted),
        )
        .all()
    )
    by_name: dict[str, list[tuple]] = {}
    for inv, loc, cname, set_code in rows:
        by_name.setdefault(cname.lower(), []).append((inv, loc, set_code))
    return by_name


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

    # v3.33.0 — variant-group coverage. A card physically held by a SIBLING
    # variant deck is "covered by the group" — not recommended for import and
    # not moved. ``sibling_location_ids`` is EMPTY for a deck with no variant
    # group, which is the gate that keeps every branch below byte-for-byte
    # identical to the pre-feature logic (variant_covered_qty stays 0).
    sibling_location_ids = set(sibling_variant_deck_location_ids(session, deck))
    # issue #27 — physical rows already shared INTO this deck. Such a row is
    # ALSO a sibling-held row, so it would be double-counted by the sibling
    # tally below; it is counted under the share path instead (each physical
    # row counted ONCE). Empty for a deck with no variant group.
    inbound_shared_row_ids = inbound_shared_row_ids_for_deck(session, deck)

    if not parsed_rows:
        return []

    # Shared scaffold (with find_inventory_matches_for_collection_import):
    # resolve sid→card_id, build the (card_id, finish) keys, and run the
    # tuple-IN outerjoin fetch (pending rows come through with loc=None).
    # Local import to avoid the deck_service↔inventory_service cycle (same
    # precedent as get_or_create_card below).
    from app.inventory_service import resolve_import_inventory_matches

    card_by_sid, lookup_keys, rows = resolve_import_inventory_matches(session, user_id, parsed_rows)

    # Bucket the fetched rows into three lists per (card_id, finish):
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
    # v3.33.0 — per-key quantity held by sibling variant decks (subset of
    # other_deck_by_key). Always all-zero for a deck with no variant group.
    variant_covered_by_key: dict[tuple[int, str], int] = {}
    # issue #27 — per-key copies whose physical row is shared INTO this deck.
    # Disjoint from variant_covered_by_key (a row is counted in exactly one)
    # so the two together count each physical row once.
    shared_covered_by_key: dict[tuple[int, str], int] = {}
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
                # v3.33.0 — flag + tally copies held by a sibling variant
                # deck so the recommendation can treat them as "covered".
                # issue #27 — a row already shared INTO this deck is counted on
                # the share path instead (NOT the sibling tally) so each
                # physical row is counted exactly once.
                is_sibling = row.storage_location_id in sibling_location_ids
                is_shared_in = row.id in inbound_shared_row_ids
                entry["is_variant_sibling"] = is_sibling
                entry["is_shared_in"] = is_shared_in
                if is_shared_in:
                    shared_covered_by_key[key] = shared_covered_by_key.get(key, 0) + row.quantity
                elif is_sibling:
                    variant_covered_by_key[key] = variant_covered_by_key.get(key, 0) + row.quantity
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

    # v3.38.x Brew Mode — oracle-level fallback. For a BREW deck the claimable
    # pool is the UNASSIGNED inventory of the SAME CARD (any printing/finish),
    # not just the exact requested printing+finish (owner decision 2026-06-11:
    # "foil is a preference; claim the printing you own; proxy only when no
    # unassigned copy exists"). Build the per-name buckets once; the per-row
    # loop ranks + folds them in. Gated on is_brew so non-brew decks keep the
    # exact-printing matches byte-for-byte.
    brew_rows_by_name: dict[str, list[tuple]] = {}
    name_by_card_id: dict[int, str] = {}
    if deck.is_brew and card_by_sid:
        resolved_ids = set(card_by_sid.values())
        name_by_card_id = {
            c.id: c.name
            for c in session.query(Card.id, Card.name).filter(Card.id.in_(resolved_ids)).all()
        }
        brew_rows_by_name = _brew_same_name_rows(session, user_id, set(name_by_card_id.values()))

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
                    "variant_covered_qty": 0,
                    "is_variant_group": deck.variant_group_id is not None,
                    "is_brew": deck.is_brew,
                    "brew_owned_in_other_deck": False,
                    "brew_other_deck_names": [],
                    "brew_claim_fallback": False,
                    "brew_claim_set": "",
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

        # v3.38.x Brew — replace the exact-printing movable set with the
        # oracle-aware UNASSIGNED pool (same card, any printing/finish), ranked
        # by the owner's preference ladder (exact printing+finish → exact
        # printing → finish match → anything, then tier, then row id). Surface
        # deck-resident copies of the same card as "owned in another deck" —
        # informational only; a brew never claims them (2026-06-11 amendment).
        brew_owned_in_other_deck = False
        brew_other_deck_names: list[str] = []
        brew_claim_fallback = False
        brew_claim_set = ""
        if deck.is_brew:
            cname = (name_by_card_id.get(card_id) or "").lower()
            unassigned: list[dict] = []
            other_deck: list[dict] = []
            for inv, loc, set_code in brew_rows_by_name.get(cname, []):
                if loc is not None and loc.type == "deck":
                    if inv.storage_location_id != target_storage_location_id:
                        other_deck.append(
                            {
                                "inventory_row_id": inv.id,
                                "location_name": loc.name,
                                "location_type": "deck",
                                "quantity_available": inv.quantity,
                                "tags": get_row_tags(inv),
                                "set_code": set_code,
                            }
                        )
                    # Rows already in THIS deck are not movable and not "other".
                    continue
                unassigned.append(
                    {
                        "inventory_row_id": inv.id,
                        "location_name": "Pending" if loc is None else loc.name,
                        "location_type": "pending" if loc is None else loc.type,
                        "quantity_available": inv.quantity,
                        "tags": get_row_tags(inv),
                        "set_code": set_code,
                        "_entry_card_id": inv.card_id,
                        "_entry_finish": inv.finish,
                    }
                )
            unassigned.sort(
                key=lambda m: (
                    0 if m["_entry_card_id"] == card_id else 1,
                    0 if m["_entry_finish"] == finish else 1,
                    _RECONCILE_TIER_PRIORITY.get(m["location_type"], 99),
                    m["inventory_row_id"],
                )
            )
            matches = unassigned
            other_deck_matches = sorted(other_deck, key=lambda m: m["inventory_row_id"])
            brew_other_deck_names = sorted({m["location_name"] for m in other_deck})
            brew_owned_in_other_deck = bool(other_deck)
            if unassigned:
                brew_claim_set = unassigned[0].get("set_code") or ""
                brew_claim_fallback = unassigned[0]["_entry_card_id"] != card_id

        total_available = sum(m["quantity_available"] for m in matches)
        total_in_target_deck = sum(m["quantity_available"] for m in target_deck_matches)
        total_in_other_decks = sum(m["quantity_available"] for m in other_deck_matches)

        # v3.33.0 — variant-group coverage is applied FIRST: copies held by a
        # sibling variant deck satisfy part (or all) of the need without an
        # import or a move. ``remaining_needed`` then drives the existing
        # non-deck move/import logic UNCHANGED. With no variant group,
        # variant_covered_qty is 0 and this reduces byte-for-byte to the prior
        # three-way recommendation.
        # issue #27 — sibling-held copies AND copies already shared into this
        # deck both count as covered (each physical row once — the two tallies
        # are disjoint). An already-shared card is therefore recognized, not
        # treated as "missing".
        variant_covered_qty = min(
            variant_covered_by_key.get(key, 0) + shared_covered_by_key.get(key, 0),
            quantity_needed,
        )
        remaining_needed = quantity_needed - variant_covered_qty

        # recommended_action / move_qty / new_qty are driven by `matches`
        # (non-deck rows) ONLY. Deck-located rows never auto-move by
        # default — they're informational. The commit handler uses
        # target_deck_matches separately to merge new imports into an
        # existing deck row rather than create a duplicate.
        if remaining_needed <= 0:
            # Fully covered by a sibling variant deck — leave it where it is.
            action = "covered_by_variant"
            move_qty = 0
            new_qty = 0
        elif total_available >= remaining_needed:
            action = "move_existing"
            move_qty = remaining_needed
            new_qty = 0
        elif total_available > 0:
            action = "move_existing_plus_new"
            move_qty = total_available
            new_qty = remaining_needed - total_available
        else:
            action = "import_new"
            move_qty = 0
            new_qty = remaining_needed

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
                "variant_covered_qty": variant_covered_qty,
                "is_variant_group": deck.variant_group_id is not None,
                "is_brew": deck.is_brew,
                "brew_owned_in_other_deck": brew_owned_in_other_deck,
                "brew_other_deck_names": brew_other_deck_names,
                "brew_claim_fallback": brew_claim_fallback,
                "brew_claim_set": brew_claim_set,
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
    row.updated_at = utc_now()
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
    row.updated_at = utc_now()
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
        existing_deck_row.updated_at = utc_now()
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
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(existing_deck_row)
        session.flush()

    row.quantity -= quantity
    row.updated_at = utc_now()

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
        existing_row.updated_at = utc_now()
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
            created_at=utc_now(),
            updated_at=utc_now(),
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


def delete_deck(session: Session, deck_id: int, user_id: int, *, commit: bool = True) -> bool:
    deck = get_deck(session, deck_id=deck_id, user_id=user_id)
    if not deck:
        return False

    # gate-#5 (Phase 2): clean the deck's children the ORM never cascaded — these
    # leaked under prod SQLite (FK off) and only the v4 DB CASCADE would have saved
    # them. Clean explicitly here so the app is correct on BOTH backends (the DB
    # CASCADE/SET NULL clauses are then defense-in-depth, not the sole mechanism).
    #   - deck_bracket_estimates / deck_bracket_findings are RAW (non-ORM) tables
    #     not mapped to Deck, so session.delete(deck) never touched them. Raw-SQL
    #     delete, findings first (child of estimates via estimate_id CASCADE), then
    #     estimates; both keyed by deck_id.
    #   - deck_token_requirements.deck_id is CASCADE intent (ORM, but no Deck
    #     relationship) — delete the rows.
    #   - game_seats.deck_id is SET NULL intent; deck_name_at_game (snapshotted at
    #     game creation) preserves the historical deck identity, so just null the FK.
    from app.models import DeckTokenRequirement

    # issue #27 — drop every deck_card_share where this deck is the source OR
    # the target before its rows/location go (SQLite enforces no FK CASCADE).
    delete_shares_for_deck(session, deck.id)

    session.execute(text("DELETE FROM deck_bracket_findings WHERE deck_id = :d"), {"d": deck.id})
    session.execute(text("DELETE FROM deck_bracket_estimates WHERE deck_id = :d"), {"d": deck.id})
    session.query(DeckTokenRequirement).filter(DeckTokenRequirement.deck_id == deck.id).delete(
        synchronize_session=False
    )
    session.query(GameSeat).filter(GameSeat.deck_id == deck.id).update(
        {GameSeat.deck_id: None}, synchronize_session=False
    )

    if deck.storage_location_id:
        deck_rows = (
            session.query(InventoryRow)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.storage_location_id == deck.storage_location_id,
            )
            .all()
        )

        # gate-#5 (Phase 2): the proxy-row branch below session.delete()s rows
        # WITHOUT the FK-safe cleanup, orphaning any showcase_items / trade_items
        # that referenced them (a proxy row CAN be showcased/traded). Route the
        # proxy ids through the shared clean-path FIRST (deletes ShowcaseItems,
        # abandons pending trades → writes the *_at_trade snapshot → NULLs the ref).
        from app.inventory_service import clean_inventory_row_references

        proxy_ids = [r.id for r in deck_rows if r.is_proxy]
        if proxy_ids:
            clean_inventory_row_references(session, proxy_ids)

        # Disband, not destroy (decision 2026-06-12). A PROXY row represents a
        # card the user does not own — discard it outright. A REAL (claimed)
        # row returns to the collection as PENDING: for drawer-sorter users the
        # caller's post-delete ``resort_collection`` re-files it to its drawer
        # (byte-identical round trip); others place it manually. No
        # prior-location persistence — pending + resort (the brew-buylist v4
        # note records why exact-location memory is deferred to the nullable
        # inventory link). This makes deck deletion non-destructive of owned
        # inventory and closes the brew import→delete→export idempotent loop.
        for row in deck_rows:
            if row.is_proxy:
                session.delete(row)
                continue
            row.storage_location_id = None
            row.is_pending = True
            row.drawer = None
            row.slot = None
            row.updated_at = utc_now()
            log_transaction(
                session=session,
                user_id=user_id,
                event_type="return_from_deck",
                card_id=row.card_id,
                finish=row.finish,
                quantity_delta=row.quantity,
                source_location=f"deck:{deck.name}",
                destination_location="collection",
                inventory_row_id=row.id,
                note=f"Returned to collection on delete of deck {deck.name}",
                flush=False,
            )

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
    if commit:
        session.commit()
    return True
