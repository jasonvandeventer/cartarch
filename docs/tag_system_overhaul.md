# Tag System Accuracy Overhaul

**Status:** design / pre-implementation
**Sequenced before:** analytics_overhaul.md (the analytics overhaul consumes the tags this work hardens)
**Authoring date:** 2026-05-13
**Last revised:** 2026-05-14 to reflect v3.22.0 architecture

---

## 0. Premise

Tags on `InventoryRow` drive Synergy classification, Deck Health metrics, the existing Upgrade Targets list, and (when they land) the AI-powered deck upgrade suggestions. They are upstream of everything analytical the app does.

User feedback has surfaced that the auto-tagger (`suggest_card_roles` in `app/deck_service.py`) is unreliable in practice: cards get tagged with roles that don't match their actual function, and cards that should be tagged get missed. The downstream features inherit these errors.

This doc is a re-scoping. The first version of this doc proposed work that has since landed (the v3.22.0 confidence migration), so the document has been revised to reflect the current state and focus on what remains.

The fix is not a single regex tweak. It's a structured upgrade to:

- The intrinsic regex pattern accuracy (audit, document, improve)
- The commander theme extraction (the biggest accuracy lever)
- The relationship between themes and per-card Synergy tagging (the false-positive problem)
- The use of tag confidence in downstream consumers (built but not wired up)
- The user-facing review surface (let users correct mistakes cheaply)

## 1. Current state

### 1.1 The tag taxonomy (unchanged)

| Tag        | Intent                                                     |
| ---------- | ---------------------------------------------------------- |
| Ramp       | Mana acceleration that is not basic land                   |
| Draw       | Card draw of any kind                                      |
| Removal    | Targeted removal                                           |
| Wipe       | Mass removal (board wipes)                                 |
| Tutor      | Library search effects                                     |
| Engine     | Repeating value source                                     |
| Synergy    | Card whose value depends on interaction with deck strategy |
| Threat     | Finisher / win condition                                   |
| Hate       | Graveyard hate, artifact hate, stax-adjacent               |
| Protection | Protects creatures or commander                            |

These tags are correct in concept. The accuracy problem is in how they're assigned, not what they are.

### 1.2 What's already implemented (as of v3.22.0)

**Structured tag storage.** `InventoryRow.tags` stores a JSON array of `{tag, confidence, source}` dicts. The shape is:

```json
[
  { "tag": "Ramp", "confidence": "certain", "source": "auto" },
  { "tag": "Synergy", "confidence": "medium", "source": "auto" },
  { "tag": "Engine", "confidence": "high", "source": "user" }
]
```

`get_row_tags(row)` returns just the names (for backward compat). `get_row_tag_details(row)` returns the full dicts. `set_row_tags(row, tags, source=..., confidence=...)` writes them. `add_auto_tags(row, suggested)` unions new auto-tagger suggestions without downgrading user-confirmed tags. The confidence vocabulary is `("certain", "high", "medium", "low")`; source vocabulary is `("user", "auto")`.

**Auto-tagger with theme awareness.** `suggest_card_roles(card, themes=None)` accepts an optional themes dict. When themes are provided, Synergy is suggested for cards that match the deck's strategy via `card_matches_theme`. When themes are None (loose inventory, no deck context), only intrinsic tags are suggested.

**Commander theme extraction.** `extract_commander_themes(commander_rows)` parses commander oracle text and returns a dict with `card_types`, `excluded_subtypes`, `cmc_gate`, `mechanics`, `subtypes`, and `signals`. Currently detects six mechanics: `counters`, `tokens`, `graveyard`, `sacrifice`, `discard`, `death_triggers`.

**Per-card theme match.** `card_matches_theme(card, themes)` returns True if a card matches the commander's themes by tribal subtype, card type (with exclusion + CMC gates), or mechanic.

**Intrinsic regex patterns.** Six compiled patterns (`_REMOVAL_RE`, `_WIPE_RE`, `_PROTECTION_RE`, `_ENGINE_RE`, `_THREAT_RE`, `_HATE_RE`) plus the draw/ramp helpers (`matches_draw`, `matches_ramp_non_land`) cover the intrinsic tag detection. The patterns are not naive: they anchor library searches to "your library" (so opponent-search effects don't trigger Ramp), strip quoted granted abilities (so Sifter of Skulls doesn't falsely tag Ramp), distinguish edicts from wipes, and handle the bargain-cast-cost vs colon-delimited-sac-cost distinction for Engine.

**Synergy classification.** `compute_deck_synergy(all_rows, combos)` classifies each non-commander card as direct synergy, supporting, or unrelated. A card is "direct" if it's in a combo, has Synergy or Threat tag, or matches the commander's themes via `card_matches_theme`. "Supporting" if it has Ramp/Draw/Removal/Wipe/Tutor/Protection/Engine/Hate or is a Land. "Unrelated" otherwise.

**Dead cards / Upgrade Targets.** `compute_dead_cards(all_rows, synergy)` flags untagged cards classified as unrelated by the synergy engine. This is the feature users have called inaccurate.

### 1.3 What's NOT yet implemented

The gaps that the rest of this doc addresses:

**Pattern accuracy is unaudited.** The intrinsic regexes are thoughtful but have not been systematically tested against the playgroup's actual decks. False positives and false negatives are anecdotal.

**Commander theme detection is too narrow.** Only six mechanics are detected. Real-world commanders care about themes the current system misses entirely: Food, Treasure, Energy, lifegain, +1/+1 counters specifically (versus generic "counters"), the Ring, Adventure, Saga progression, "spells you cast" (storm/prowess), landfall, attacking, blocking, equip/aura attachment, and others. For decks built around these themes (Frodo & Sam, Bello, Sisay), Synergy is systematically undertagged.

**`card_matches_theme` over-broad matching.** The mechanic matchers use plain substring tests. "sacrifice" in the commander themes matches any card whose oracle text contains "sacrifice" anywhere — including removal spells like Diabolic Edict ("target opponent sacrifices a creature") that aren't really synergy with a sac-based deck. This is the false-positive problem.

**Confidence is stored but not used downstream.** `compute_deck_synergy` calls `get_row_tags(row)`, which returns name strings only. `compute_dead_cards` calls `get_row_tags` the same way. Neither consults confidence. A user-confirmed Synergy and an auto-suggested low-confidence Synergy are treated identically. The data exists; the consumers don't read it.

**No review-tags UI.** Users can edit tags on individual cards via the existing tag editor, but there's no surface that flags low-confidence auto-tags for review, no batch confirm flow, and no signal of which tags need human attention.

**Partner/Background commanders not specially handled.** `extract_commander_themes` iterates over commander rows additively. There's no weighting or conflict resolution if one commander cares about graveyards and the other about sacrifice — both themes are extracted with equal weight.

### 1.4 Known failure modes (from real tagging sessions)

Patterns observed during manual tagging passes on the playgroup's decks:

- **Multi-mode cards get tagged for one mode only.** Solemn Simulacrum gets Ramp but not Draw despite the death-trigger draw. Faramir, Field Commander has both a draw trigger (creature dies → end-step draw) and a token trigger (Ring tempts → Soldier token) but the auto-tagger picks one.
- **Commander-context-dependent cards mis-tagged generically.** Gilded Goose in Frodo & Sam should be Ramp + Synergy (Foods matter); in a deck where Foods don't matter it's just Ramp. The current system gets the Ramp right but only adds Synergy when `extract_commander_themes` knows about Food, which it currently doesn't.
- **Name-based mis-tagging.** Cards whose name suggests a role but text contradicts it: Revive the Shire (sounds like land ramp; actually graveyard recursion + Food), Echoing Assault (sounds like removal; actually a combat trick), Hazel of the Rootbloom (sounds like a beater; actually a token-copy engine).
- **Hybrid mana cards with conditional effects** get tagged for the wrong half or both halves equally.
- **Replacement effects** that generate value (Leyline of Hope's "if you would gain life, gain that much plus 1") get missed by the Engine pattern because they don't use the typical "sacrifice ... :" or "return ... from graveyard" wording.
- **Cast-from-top effects** (Mystic Forge, Future Sight) get tagged Engine correctly today but historically were sometimes tagged Draw.
- **Devour and resource-consuming abilities** get over-tagged Synergy. Feasting Hobbit with Devour Food consumes Foods as a cost without triggering downstream lifegain payoffs — it's Threat, not Synergy with a Food deck.
- **Synergy over-tagging on tangential mentions.** Any card with the word "sacrifice" anywhere gets flagged Synergy in a sacrifice-themed deck, including removal that triggers an opponent's sacrifice.

## 2. Proposed work

### 2.1 Taxonomy clarifications (rules, not new tags)

Keep the 10 tags. Add a set of disambiguation rules documented in this doc and as inline comments in `suggest_card_roles`. The rules below codify decisions made during real-world tagging sessions:

- **Multi-mode cards get all applicable tags.** A creature that ETBs with a draw and dies with a drain gets Draw + Synergy. Don't pick "the primary" mode.
- **Synergy requires demonstrable interaction with the deck's strategy.** A card is Synergy if removing the commander would meaningfully reduce its value. A good card in a vacuum is not Synergy.
- **Cast-from-top effects are Engine, not Draw.** Mystic Forge, Future Sight, and similar cards generate value without drawing into hand; they bypass the deck's draw step rather than augmenting it.
- **Resource-consuming effects (Devour, sacrifice as additional cost) are Threat, not Synergy, unless the consumed resource has its own downstream payoffs in the deck.** Devour Food in a Food deck is Threat (the Foods are consumed, not activated).
- **Removal-as-Synergy is rejected.** Diabolic Edict in a sacrifice deck is Removal, not Synergy + Removal. The "sacrifice" word in its text refers to opponent sacrifice and doesn't make the card a deck engine.
- **"Free interaction" stays under Removal/Protection regardless of cost.** Force of Will is Protection; Counterspell is Removal. The free-cast nature is captured by the bracket estimator separately, not by the tag.

### 2.2 Auto-tagger audit

Audit `suggest_card_roles` against a sample of cards drawn from the playgroup's active decks. Recommended sample: 100 unique non-basic cards, prioritized to include:

- 30 cards we already know are mis-tagged from real tagging sessions
- 30 cards selected randomly from across the playgroup's decks
- 20 cards that are Synergy in some commander contexts and not others (commander-relative ambiguity)
- 20 cards with complex oracle text (multi-mode, replacement effects, granted abilities)

For each card, record:

- Auto-tagger output with no commander context (intrinsic only)
- Auto-tagger output with commander context for each playgroup commander the card might appear under
- Expected tags per the disambiguation rules in §2.1
- Category of difference: false positive, false negative, partial match, correct

Produce a markdown table documenting the audit results in this doc or a sibling file. The audit informs §2.3 (intrinsic pattern improvements) and §2.4 (theme extraction improvements). **The audit is the precondition for the rest of the work, not optional.**

### 2.3 Intrinsic pattern improvements

Based on the audit, update the six intrinsic regex patterns (`_REMOVAL_RE`, `_WIPE_RE`, `_PROTECTION_RE`, `_ENGINE_RE`, `_THREAT_RE`, `_HATE_RE`) plus the draw/ramp helpers. The patterns are already thoughtful; this work is incremental refinement of cases the audit surfaces.

Specific known issues to address (from §1.4):

- **Engine pattern misses replacement effects.** Add a pattern for "if you would [gain life|create a token|put a counter] ... instead". This is the Leyline of Hope class.
- **Engine pattern misses repeated triggers without explicit recursion or sacrifice.** "At the beginning of your upkeep, [draw a card | create a token | put a counter]" is repeating value. Match these.
- **Draw pattern's trigger-condition exclusion is good but conservative.** Cards like Sheoldred's Edict that _cause_ drawing as a consequence (rather than punishing drawing as a condition) are handled correctly today, but the audit may surface edge cases.
- **Threat pattern is intentionally narrow.** Soft threats (large creatures, evasion) aren't detected because the schema doesn't include power/toughness. This is by design and stays.

### 2.4 Commander theme extraction expansion

This is the biggest accuracy lever and the largest scope item in the overhaul.

The current `extract_commander_themes` detects six mechanics. Add detection for at least these themes that real playgroup commanders care about:

| Theme          | Detection cue in commander oracle                                 |
| -------------- | ----------------------------------------------------------------- |
| lifegain       | "gain X life", "whenever you gain life", "lifelink"               |
| food           | "food token", "sacrifice a food", "foods you control"             |
| treasure       | "treasure token", "sacrifice a treasure"                          |
| clue           | "clue token", "sacrifice a clue"                                  |
| blood          | "blood token"                                                     |
| ring           | "the ring tempts you", "ring-bearer"                              |
| +1/+1 counters | "+1/+1 counter" (split from generic "counters")                   |
| -1/-1 counters | "-1/-1 counter"                                                   |
| spells_cast    | "whenever you cast", "spells you cast cost", "storm", "prowess"   |
| landfall       | "whenever a land enters", "lands you control enter"               |
| attack         | "whenever a creature attacks", "whenever you attack"              |
| blocking       | "whenever a creature blocks", "whenever ~ is blocked"             |
| equip          | "equip", "equipped creature", "attached creature" (for equipment) |
| aura           | "enchant creature", "enchanted creature"                          |
| energy         | "energy counter", "{E}"                                           |
| saga           | "saga", "lore counter"                                            |

The detection should sit in `extract_commander_themes` alongside the existing mechanics detection. Output structure: add each new theme as a string key into the `mechanics` set, so existing consumers see them without API changes.

A few of these (the token types) are likely also surfaced by Spellbook combos or tokens elsewhere in the codebase. Reuse those signal sources where possible rather than duplicating regex.

### 2.5 `card_matches_theme` precision

The current implementation matches by substring. Tighten it to reduce false-positive Synergy:

For each mechanic theme, define an inclusion pattern _and_ an exclusion pattern. A card matches the theme if its oracle text matches the inclusion pattern AND does not match the exclusion pattern.

Examples:

- **Sacrifice theme:**
  - Include: "sacrifice a/an/another [creature|permanent|artifact|token|treasure|food|clue|blood]:" (sac outlets with colon), OR "whenever you sacrifice", OR "if a creature you control died this turn" (sac payoff signals)
  - Exclude: "target opponent sacrifices", "each opponent sacrifices" (these are removal effects on opponents, not engine)
- **Graveyard theme:**
  - Include: "from your graveyard", "creature cards in your graveyard", "whenever ~ dies"
  - Exclude: "exile target card from a graveyard" alone (that's graveyard hate, not graveyard engine — already gets Hate tag)
- **Tokens theme:**
  - Include: "create a/an/X [type] token", "tokens you control", "if one or more tokens would be created"
  - Exclude: "becomes a copy of" alone (that's a copy effect, not synergy with a token theme)
- **Lifegain theme:**
  - Include: "whenever you gain life", "you gained X or more life this turn", "if you would gain life ... instead"
  - Exclude: oracle text where "life" only appears in the context of opponent life loss

The patterns should be data-driven (a dict keyed by mechanic name) rather than embedded in `card_matches_theme` as branching code. This makes future additions cheaper.

### 2.6 Confidence-aware downstream consumers

Wire confidence into the analytics functions that currently ignore it.

**`compute_deck_synergy`:** Currently treats Synergy/Threat tags as binary. Change to weight them by confidence:

- `certain` / `high` confidence Synergy → direct (full credit)
- `medium` confidence Synergy → direct only when the card also has another supporting tag, otherwise supporting
- `low` confidence Synergy → supporting (treat as "auto-tagger guessed, don't trust as direct synergy")
- User-confirmed tags always win over auto-suggested tags of the same confidence

**`compute_dead_cards`:** The current rule is "no role tags AND classified unrelated → flag as dead." Change to:

- No tags at all → flag
- Only `low` confidence tags from auto-tagger → flag (treat as effectively untagged for purposes of upgrade-target detection)
- Any `medium`+ tag from any source → not flagged

This directly addresses the user complaint that Upgrade Targets is inaccurate: many of the "dead" cards are cards the auto-tagger correctly identified as supporting (Ramp, Draw, Removal) but the dead-card logic ignores those tags. The confidence-aware version respects them.

**`compute_deck_health`:** Currently counts Ramp/Draw/Removal/Wipes by tag presence + oracle text fallback. Expose confidence in the count display: "12 Ramp cards (10 user-confirmed, 2 auto-suggested)". This is a UI change in the Health panel, not a logic change.

### 2.7 Review-tags workflow

Add a UI surface on the deck detail page that surfaces tags requiring user review.

A tag needs review when:

- Confidence is `low` from any source, OR
- Confidence is `medium` from `source: "auto"` AND the user has never explicitly interacted with this card's tags

For each card in the review queue, show:

- Card name
- The auto-applied tags with their confidence indicators
- A one-click "confirm" button that upgrades all tags on the card to `source: "user"`, `confidence: "high"`
- An inline editor to add, remove, or change tags
- An "ignore" option that marks the card as user-acknowledged without changing tags (a third state alongside confirm and edit)

The review surface defaults to a collapsed banner ("12 cards have tags needing review") at the top of the deck detail page. Expanding it shows the list.

**Sequencing within the deck:** Review by impact — cards classified unrelated by `compute_deck_synergy` first (they're affecting the Upgrade Targets list), then cards with multiple low-confidence tags, then everything else.

**Persisting the "ignore" state:** Add an `acknowledged` boolean to the tag entry, or a parallel marker on the row. The exact storage choice is an implementation detail; the user-facing behavior is that an ignored card doesn't return to the review queue on subsequent loads.

## 3. Downstream effects

Once §2.4 (theme expansion) and §2.5 (matcher precision) land, the Synergy column on the deck detail page becomes meaningfully more accurate for the playgroup's actual decks. For Frodo & Sam specifically: Foods, lifegain payoffs, and Ring-tempt cards start getting Synergy tagged at high confidence rather than being missed.

Once §2.6 (confidence-aware downstream) lands, the Upgrade Targets list stops flagging cards that the auto-tagger has correctly identified as Ramp/Draw/Removal. This addresses the user complaint directly.

Once §2.7 (review surface) lands, users have a clear path to convert auto-suggestions into confirmed tags without trawling through every card in every deck.

The AI recommendation engine (Tier 3 of the roadmap) consumes the same tag data. The same accuracy improvements compound into better AI recommendations.

## 4. Migration plan

The v3.22.0 confidence migration is done; this overhaul does not require a schema migration. The work is:

1. **Audit (§2.2).** Produces a markdown table of 100 cards × auto-tagger output × expected output. Living document, updated as patterns are refined.
2. **Code changes**, in any order or together:
   - Pattern improvements in the regex constants (§2.3)
   - Theme extraction expansion in `extract_commander_themes` (§2.4)
   - Matcher precision in `card_matches_theme` (§2.5)
   - Confidence wiring in `compute_deck_synergy`, `compute_dead_cards`, `compute_deck_health` (§2.6)
   - Review-tags UI: new template partial, new route, new endpoint for confirm/ignore (§2.7)
3. **Backfill retag.** After the code changes land, run the auto-tagger across every existing `InventoryRow` to apply the improved patterns. The `add_auto_tags` function already preserves user-confirmed tags, so backfilling is safe.

Single deploy unit, one version bump. No data loss. Existing user tags preserved.

## 5. Version bump

Minor. The schema is unchanged (the JSON contents already support the new shape from v3.22.0). The code change is large but the data change is zero.

## 6. Open questions

1. **Where does the audit live?** Inline in this doc, in a sibling `tag_audit.md`, or as a CSV/markdown table checked into the repo at `docs/tag_audit.md`? Recommend the latter — it's living data that benefits from being version-controlled.

2. **How are partner commanders and Background pairs handled?** `extract_commander_themes` already iterates over all commander rows additively. Open question: should `card_matches_theme` give weighted credit when a card matches a theme from one commander but not the other? For a Halfling-tribal + Equipment partner pair, equipment cards should be Synergy (one commander cares), creatures should be Synergy if they're Halflings (the other commander cares). The current additive model handles this case correctly. The harder case is conflict — but conflicts among commander themes are rare in practice and probably don't need special logic.

3. **Should the auto-tagger run on every page load, or only on explicit user action?** Currently the existing Retag button is the trigger; on page load, cached tags are read but not regenerated. Performance is fine at 100 cards per deck, but if the auto-tagger's commander-theme awareness gets significantly richer (§2.4), re-running it cheaply on every deck render could keep tags fresh as the deck evolves. Recommend: stay on the explicit Retag model for now, revisit if users report stale tags.

4. **What's the trigger for backfill retag?** After the code changes land, every existing `InventoryRow` should be re-tagged to pick up the improved patterns. Should this happen automatically on next deploy, or be a manual button users hit? Auto-running on deploy is faster but blocks the deploy on the backfill duration; manual is safer but easy to forget. Recommend: manual button per-user ("Re-tag my decks") on the user settings page, with an admin-level option to run for all users.

5. **Where do theme cues live?** The expanded theme detection patterns (§2.4) need a home. Inline in `extract_commander_themes` becomes unwieldy at 16 themes. Recommend: extract a `THEME_PATTERNS` constant mapping theme name to (inclusion regex, optional exclusion regex), then iterate it in `extract_commander_themes`. Same data structure used by `card_matches_theme` (§2.5) so the two stay in sync.
