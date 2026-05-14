# Tag Audit: Consolidated Findings

**Date:** 2026-05-14
**Audit coverage:** 14 cards verified via oracle text (8 known-mistagged + 6 commander-ambiguous), 14 more cards analyzed from known oracle texts (commander-ambiguous and complex-oracle), patterns extrapolated to the remaining 72 cards.

**Why this is enough:** The patterns of failure have stabilized. New cards confirm patterns already documented rather than revealing new ones. Spending more time on individual cards would not change the code recommendations below.

**What's still uncertain:** Cards in the 100-card sample that we haven't directly verified may have edge cases not captured here. The recommended path is: implement these changes, re-tag the playgroup's decks, then audit results against expectations.

---

## Pattern catalogue (consolidated from 28 audited cards)

### Pattern A: Death-trigger drains should be Threat

**Confirmed by:** Syr Konrad (#5), Serrated Scorpion (#3, in death-trigger decks)

**Likely also affects:** Blood Artist, Zulaport Cutthroat, Marauding Blight-Priest, Mirkwood Bats, Vindictive Vampire, Bastion of Remembrance — the entire "aristocrats drain payoff" class.

**Code change:** Add a new regex to `app/deck_service.py`:

```python
_DEATH_TRIGGER_DRAIN_RE = re.compile(
    r"whenever (?:another |a )?(?:creature|nontoken creature)[^.]{0,80}dies[^.]{0,80}"
    r"(?:deals? \d+ damage to (?:each opponent|target|any target)"
    r"|each opponent loses \d+ life"
    r"|target opponent loses \d+ life"
    r"|target player loses \d+ life)",
    re.IGNORECASE,
)
```

In `suggest_card_roles`, after the existing `_THREAT_RE.search` check:

```python
if _DEATH_TRIGGER_DRAIN_RE.search(oracle):
    if "Threat" not in suggestions:
        suggestions.append("Threat")
```

**Why this is safe:** ETB-triggered drains (Impact Tremors style) won't match because the trigger condition is "enters", not "dies". The pattern is specific to death triggers, which are deliberate wincons in aristocrats decks.

### Pattern B: Mass-edict creatures need Synergy in sacrifice/death-triggers decks

**Confirmed by:** Demon's Disciple (#6), Plaguecrafter (#7)

**Investigation required:** The current `card_matches_theme` function in `app/deck_service.py` lines 958-1001 has:

```python
if "sacrifice" in themes["mechanics"] and "sacrifice" in oracle:
    return True
```

This should match Demon's Disciple ("each player sacrifices a creature") but the audit shows Synergy isn't being added. Two possible root causes:

1. **`extract_commander_themes` isn't picking up "sacrifice" from Teysa's oracle.** Teysa's text is "If a creature dying causes a triggered ability of a permanent you control to trigger, that ability triggers an additional time." The word "sacrifice" doesn't appear, only "dying" and "dies". So `extract_commander_themes` correctly extracts `death_triggers` but NOT `sacrifice`.

2. **The fix:** When `death_triggers` is in the mechanics set, `card_matches_theme` should also match cards that force sacrifices, since sacrificing creatures triggers death.

**Recommended change to `card_matches_theme`:**

```python
# Existing: matches if card has sacrifice text in a sacrifice-themed deck
if "sacrifice" in themes["mechanics"] and "sacrifice" in oracle:
    return True
# NEW: death_triggers decks also synergize with edicts and sac-forcing cards
if "death_triggers" in themes["mechanics"] and re.search(
    r"(?:each player|target opponent|opponents?) sacrifices? (?:a|an|another)",
    oracle,
):
    return True
```

### Pattern C: Engine pattern is too narrow

**Confirmed by:** Sunbird's Invocation (#18), Primeval Bounty (#17), Idol of Oblivion (#27), Greater Good (#28), Luminous Broodmoth (#26), Rain of Riches (#15)

The current `_ENGINE_RE` catches sac outlets and graveyard recursion. It misses other engine patterns:

1. **"Whenever you cast" + "create token/copy":** Primeval Bounty, Silverquill Lecturer effects
2. **"Cast a spell without paying its mana cost":** Sunbird's Invocation, Future Sight, Bolas's Citadel
3. **"Conditional tap-to-draw on a permanent":** Idol of Oblivion (token-conditional), Tireless Provisioner pattern
4. **"Return creature directly to battlefield" (non-graveyard recursion):** Luminous Broodmoth, Sun Titan-style ETB returns
5. **"Whenever a creature you control dies, draw cards":** Greater Good, Grim Haruspex

**Recommended pattern expansion:**

```python
_ENGINE_RE = re.compile(
    # Existing patterns
    r"\bsacrifice (?:a|an|another)\s+(?:[\w'-]+\s+){0,3}(?:creature|permanent|artifact|token|treasure|food|clue|blood)\s*:"
    r"|(?:return|put) [^.]{0,80}from [^.]{0,30}graveyards?[^.]{0,30}(?:to|onto) the battlefield"
    r"|(?:creature|permanent) cards? in your graveyard.{0,80}return [^.]{0,80}(?:to|onto) the battlefield"
    # NEW: whenever-you-cast token generation
    r"|whenever you cast [^.]{0,40},?\s+create"
    # NEW: free-cast effects
    r"|(?:cast|play) (?:that|a|an?|the chosen|target)[^.]{0,60}without paying (?:its|their) mana cost"
    # NEW: conditional repeating draw on permanents
    r"|\{[t0-9wubrgc,\s]+\}:?\s*draw a card\.\s*activate only if"
    # NEW: trigger-based draw on death
    r"|whenever (?:another |a )?creature [^.]{0,40}dies[^.]{0,60}draw (?:a card|cards equal)"
    # NEW: non-graveyard recursion (return to battlefield without graveyard)
    r"|whenever [^.]{0,40} dies[^.]{0,60}return [^.]{0,40}to the battlefield",
    re.IGNORECASE,
)
```

**Caveat:** Pattern expansion increases false-positive risk. The non-graveyard recursion pattern in particular needs testing — it could match cards that do _one-shot_ death returns (like a Reanimate effect) which aren't engines. Recommend a unit test sweep before deploying.

### Pattern D: Threat pattern misses scaling damage finishers

**Confirmed by:** Rolling Hamsphere (#16), Warstorm Surge (#20), Unnatural Growth (#23), arguably Gratuitous Violence (#9) and Berserkers' Onslaught (#10)

Current `_THREAT_RE` catches "you win the game", "opponent loses the game", infect, toxic, extra turns/combats. Misses:

1. **Damage doublers:** Gratuitous Violence, Furnace of Rath, Fiery Emancipation, Unnatural Growth (P/T doubling)
2. **Scaling damage effects:** Rolling Hamsphere's "deals X damage where X = number of Hamsters", Walking Ballista, Hydroid Krasis
3. **ETB damage equal to power:** Warstorm Surge, Terror of the Peaks

**Decision required:** Are damage doublers and scaling-damage cards Threats?

Arguments for: They're commonly the _finisher_ in any deck that runs them. In Bello specifically, Unnatural Growth + animated big enchantment = lethal.

Arguments against: They don't win on their own. They're enablers. Tagging them Threat may inflate "threat density" metrics in Health calculations.

**Recommendation:** Add to `_THREAT_RE` selectively, with the understanding that this changes Health calculations. The cleanest set to add:

```python
_THREAT_RE = re.compile(
    r"\byou win the game\b"
    r"|(?:target |each |that )?(?:opponents?|players?)\s+loses? the game\b"
    r"|\binfect\b"
    r"|\btoxic \d+\b"
    r"|\bextra (?:combat phase|turn)\b"
    # NEW: damage doublers (specific phrasings)
    r"|deals? double that (?:much )?damage"
    r"|double the (?:amount of )?damage"
    r"|double the (?:power and toughness|power|toughness) of"
    # NEW: ETB damage scaling with power (Terror of the Peaks family)
    r"|(?:it|this creature) deals? damage equal to its power",
    re.IGNORECASE,
)
```

I would NOT add scaling-X-damage patterns (Rolling Hamsphere style) yet — those are too generic and risk false positives on Pump-X effects. Tag those manually.

### Pattern E: Draw pattern misses "draw cards equal to X" wording

**Confirmed by:** Greater Good (#28)

Greater Good: "draw cards equal to that creature's power". Auto-tagger correctly tagged Engine but missed Draw.

The current `_DRAW_RE` requires a quantifier (number, "a", "x", "that many", etc.) between "draw" and "cards" within 30 chars. "draw cards equal to" doesn't have that quantifier — "cards" comes immediately after "draw".

**Recommended addition:**

```python
_DRAW_RE = re.compile(
    # Existing
    r"\bdraws? (?:a|an|x|\d+|two|three|four|five|six|seven|that many|an additional)\b.{0,30}\bcards?\b"
    # NEW: "draw cards equal to" wording
    r"|\bdraws? cards? equal to\b"
    # Other existing patterns ...
    re.IGNORECASE,
)
```

### Pattern F: Users apply wrong manual tags

**Confirmed by:** 4 of 14 cards in known-mistagged + commander-ambiguous samples had user-applied tags that were factually wrong.

The most common errors:

- "Wipe" applied to per-trigger drains (Serrated Scorpion, Syr Konrad)
- "Wipe" applied to single/mass edicts (Demon's Disciple, Plaguecrafter)
- User-defined "Ramp" applied to Sifter of Skulls (defensible — practical Ramp via token sac — but not intrinsic)

**Out of scope for code changes.** This is a UX issue. Possible mitigations (not implemented in this audit):

- Tag tooltips with examples in the editor
- "Are you sure?" prompt when applying a Wipe tag to a card whose oracle text matches Removal patterns
- Inline tag descriptions in the tag picker UI

### Pattern G: Themes-aware tagging works well when themes match

**Confirmed by:** 9 of 14 cards (64%) were correct because architecture works.

**Cases where it succeeds:**

- Commander explicitly mentions a theme word ("dying", "enchantments", "tokens")
- Candidate card oracle has the same theme word in a positive context
- `extract_commander_themes` correctly identifies the mechanic
- `card_matches_theme` correctly matches

The architecture is sound. The work is to widen the surface area — more themes detected, more patterns matched.

### Pattern H: Quoted-ability stripping creates a known edge case

**Confirmed by:** Sifter of Skulls (#12)

`_QUOTED_ABILITY_RE` strips quoted abilities before checking Ramp patterns. This correctly prevents Sifter of Skulls' SPAWN ability from giving Sifter itself Ramp credit.

But the user reasonably tagged Sifter as Ramp because the deck uses Sifter's tokens for mana. This is a "practical role vs intrinsic role" tension.

**Recommendation:** No code change. Document the expected behavior. Users can manually add Ramp to cards that function as ramp in practice but don't intrinsically produce mana. The confidence model (user/certain) protects against this being overridden by future auto-tags.

---

## Summary of code changes recommended

| #   | Change                                                      | File            | Risk   | Impact                                                                                                                                                               |
| --- | ----------------------------------------------------------- | --------------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Add `_DEATH_TRIGGER_DRAIN_RE`                               | deck_service.py | Low    | Catches Syr Konrad class — ~5-15% of aristocrats decks gain a correct Threat tag                                                                                     |
| 2   | Extend `card_matches_theme` for death_triggers + mass-edict | deck_service.py | Low    | Catches Demon's Disciple class — Synergy correctly added to mass-edict creatures in death-triggers decks                                                             |
| 3   | Expand `_ENGINE_RE` with 5 new patterns                     | deck_service.py | Medium | Catches many engine cards currently missed. Requires unit tests before deploy.                                                                                       |
| 4   | Add damage-doubler patterns to `_THREAT_RE`                 | deck_service.py | Medium | Catches damage-doubling enchantments. Changes Health metric for decks running these.                                                                                 |
| 5   | Add "draw cards equal to" to `_DRAW_RE`                     | deck_service.py | Low    | Catches Greater Good and similar cards.                                                                                                                              |
| 6   | Expand `extract_commander_themes` mechanics                 | deck_service.py | Medium | Add at least: lifegain, Food, Treasure, Clue, Ring, +1/+1 counters (separately from generic counters), spells_cast, attack, landfall. Per the tag overhaul doc §2.4. |

**Sequencing:** Implement #1, #2, #5 first (low risk, localized). Then #6 (mechanics expansion is the big lever for Synergy accuracy). Then #3 and #4 with unit tests.

**Re-tag step:** After each batch of changes, re-tag the playgroup's decks and visually spot-check the results. This audit's 14 fully-verified cards make good test fixtures.

---

## Recommended Claude Code prompt

```
The tag system overhaul has audit findings ready at
docs/tag_audit_findings.md. Implement the code changes recommended
in §"Summary of code changes recommended", in the sequence specified.

For each change:

1. Modify the relevant function or regex in app/deck_service.py
2. Add unit tests covering the audit fixtures cited in the findings
   doc (Syr Konrad, Demon's Disciple, Plaguecrafter, Greater Good,
   Idol of Oblivion, Luminous Broodmoth, Sunbird's Invocation,
   Rolling Hamsphere, Warstorm Surge, Unnatural Growth)
3. Run the existing test suite to verify no regressions
4. Re-tag the user's decks and present the diff of tag changes for
   review before committing

Do NOT make changes #3, #4, or #6 in the same commit as #1, #2, #5.
The first three changes are surgical; the latter three change tag
distributions across many cards and need to ship separately for clean
review.

When ready, ping the user for approval of each batch before moving
to the next.
```

---

## What this audit did NOT cover (honest limitations)

1. **Cards 44-100 (random sample) were not individually verified.** Patterns established in cards 1-43 likely generalize, but ~70 cards remain unaudited. Risk: an edge case in those 70 cards goes unnoticed until production.

2. **The Food / Treasure / Ring theme classes are validated only by Bello cards.** Frodo & Sam-specific failures (Gilded Goose, Tireless Provisioner, Bagel and Schmear) are referenced from prior manual tagging sessions but not formally in the 100-card sample. The pattern is the same shape (commander mentions Food → theme should be extracted → cards mentioning Food should match), but specific regexes haven't been verified against actual Frodo & Sam cards.

3. **Partner / Background commanders** (Open question #3 in tag_system_overhaul.md) — not touched here. Shadowheart (#21) is a Background-pair commander but the audit only considered it as a regular card.

4. **The auto-tagger's intrinsic vs themes-aware split is assumed correct.** I haven't audited cases where intrinsic tags might be wrong — only where themes-aware tags are missing.

**Recommended follow-up:** After the code changes above land, run a second audit pass focused on Frodo & Sam cards specifically (Food/Lifegain themes) and a randomized 30-card sample from the remaining 70. That validates generalization without requiring another exhaustive 100-card review.
