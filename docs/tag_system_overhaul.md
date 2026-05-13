# Tag System Accuracy Overhaul

**Status:** design / pre-implementation
**Sequenced before:** analytics_overhaul.md (the analytics overhaul consumes the tags this work hardens)
**Authoring date:** 2026-05-13

---

## 0. Premise

Tags on `InventoryRow` drive Synergy classification, Deck Health metrics, the existing Upgrade Targets list, and (when they land) the AI-powered deck upgrade suggestions. They are upstream of everything analytical the app does.

User feedback has surfaced that the auto-tagger (`suggest_card_roles` in `app/deck_service.py`) is unreliable in practice: cards get tagged with roles that don't match their actual function, and cards that should be tagged get missed. The downstream features inherit these errors.

The fix is not a single regex tweak. It's a structured upgrade to:

- The tag taxonomy (clearer rules for ambiguous cases)
- The auto-tagger's accuracy (audit, document, improve)
- The user-facing review surface (let users correct mistakes cheaply)
- The confidence model (downstream consumers should know which tags are reliable)

## 1. Current state

The 10-tag system in use:

| Tag | Intent |
|---|---|
| Ramp | Mana acceleration that is not basic land |
| Draw | Card draw of any kind |
| Removal | Targeted removal |
| Wipe | Mass removal (board wipes) |
| Tutor | Library search effects |
| Engine | Repeating value source |
| Synergy | Card whose value depends on interaction with deck strategy |
| Threat | Finisher / win condition |
| Hate | Graveyard hate, artifact hate, stax-adjacent |
| Protection | Protects creatures or commander |

The auto-tagger (`suggest_card_roles`) reads oracle text and suggests tags via regex/keyword matching. Users can confirm, edit, or override the auto-tags. The user-applied state persists on `InventoryRow.tags` (JSON).

Known failure modes (collected from real-world tagging sessions):

- Cards with multiple modes get tagged for one mode only. Example: "Solemn Simulacrum" is tagged as Ramp but its death-trigger draw is missed.
- Cards whose role depends on the commander are mis-tagged generically. Example: "Gilded Goose" in a Food deck is Ramp + Synergy; in a non-Food deck it's just Ramp.
- Hybrid-mana cards with conditional effects get tagged for the wrong half. Example: a card with `{R/G}` cost and flexible text.
- Replacement effects and triggered abilities that gain resources (lifegain on damage, etc.) get missed by the Engine pattern.
- "Cast from top of library" effects (Mystic Forge style) get tagged as Draw when they're actually Engine.
- Devour and similar resource-consuming abilities get tagged as Synergy when the consumed resource doesn't trigger downstream payoffs.

## 2. Proposed work

### 2.1 Taxonomy clarifications

Keep the 10 tags. Add a set of disambiguation rules per tag, documented in a single canonical place (this doc, plus inline comments in `suggest_card_roles`). The rules cover:

- When a card with multiple modes earns multiple tags.
- When commander context matters and when it doesn't.
- When a "synergy" tag is justified vs. when the card is just a good card.
- How replacement effects and triggered abilities map to tags.
- How activated abilities map to tags vs. their target.

Specific rules to add (informed by recent tagging sessions):

- **Multi-mode cards get all applicable tags.** A creature that ETBs with a draw and dies with a drain gets Draw + Synergy. Don't pick "the primary" mode.
- **Synergy requires demonstrable interaction with the deck's actual themes.** A card is Synergy if removing the commander would meaningfully reduce its value. A "good card" in a vacuum is not Synergy.
- **Cast-from-top effects are Engine, not Draw.** Mystic Forge, Future Sight, and similar cards generate value without drawing into hand; they bypass the deck's draw step rather than augmenting it.
- **Resource-consuming effects (Devour, sac costs) are Threat, not Synergy, unless the consumed resource has its own downstream payoffs in the deck.** Devour Food in a Food deck is Threat (the Foods are consumed, not activated).
- **"Free interaction" stays under Removal/Protection regardless of cost.** Force of Will is Protection; Counterspell is Removal. The free-cast nature is captured by the bracket estimator separately, not by the tag.

### 2.2 Auto-tagger audit

Audit `suggest_card_roles` against a sample of 100 real-world cards drawn from the playgroup's active decks. For each card:

- List the auto-tagger's output.
- List the expected tags (per the disambiguation rules in 2.1).
- Categorize the difference: false positive, false negative, partial match, correct.

Produce a spreadsheet or markdown table documenting the audit results. The audit is the input to 2.3.

### 2.3 Auto-tagger improvements

Based on the audit, update `suggest_card_roles`:

- Add oracle-text patterns for the missed cases (replacement effects, triggered draws, etc.).
- Tighten patterns for the over-eager cases.
- Add commander-context awareness: if the commander defines a theme (e.g., Food, +1/+1 counters, tokens), cards that interact with that theme get the Synergy tag in addition to their base tag.
- Preserve user-confirmed tags from earlier sessions (don't overwrite them on re-tag).

### 2.4 Confidence indicator

Each tag gains a confidence value:

- `certain` — derived from an unambiguous oracle-text rule. Example: "Search your library for any card" → Tutor.
- `high` — derived from a community-consensus pattern. Example: Sol Ring → Ramp.
- `medium` — context-dependent. Example: Reliquary Tower as Engine or just utility.
- `low` — auto-tagger guessed; needs review.

Store the confidence alongside the tag. The `InventoryRow.tags` JSON gains structure: instead of `["Ramp", "Draw"]`, it stores `[{tag: "Ramp", confidence: "certain", source: "auto"}, {tag: "Draw", confidence: "medium", source: "auto"}]`. User-applied tags get `source: "user"` and `confidence: "certain"`.

Migration: existing tags get backfilled as `confidence: "high"`, `source: "user"` if the user manually confirmed them, or `confidence: "medium"`, `source: "auto"` if they came from the auto-tagger without user review. Existing schema field stays JSON; no DDL change required.

### 2.5 Per-deck "review tags" workflow

Add a UI surface on the deck detail page that surfaces low-confidence and medium-confidence auto-tags for the user to review. Each row shows:

- The card name
- The auto-applied tags with confidence indicators
- A one-click "confirm" button that upgrades all tags on that card to user-confirmed
- An inline editor to add, remove, or change tags
- An "ignore" option that dismisses the card from the review list without changing tags

The review surface defaults to hidden; opening it shows a count ("12 cards have low or medium confidence tags"). Reviewing a card removes it from the count.

This is the human-in-the-loop step. The auto-tagger is never going to be perfect; the goal is making correction cheap.

## 3. Downstream effects

Once confidence indicators land, downstream consumers can filter:

- **Synergy classification** can use only `certain` and `high` confidence tags to decide if a card is "supporting" vs "unrelated", reducing false positives.
- **Deck Health** counts already use oracle-text + tags; with confidence, the count can show "12 Ramp cards (10 confirmed, 2 auto-suggested)".
- **Upgrade Targets** can prefer `low` confidence cards as candidates for review before flagging them as dead. A card with weak tags is more likely a tagging miss than a bad fit.
- **AI Phase 1 recommendations** consume only `certain`, `high`, and user-confirmed `medium` tags. Low-confidence tags are ignored to avoid the AI building on shaky data.

## 4. Migration plan

Single deploy unit (one version bump, one tag):

1. Code change: updated `suggest_card_roles` with new patterns and commander-context awareness, JSON schema update for tags-with-confidence, new review-tags UI surface, retag-with-preserve logic.
2. Migration script `vX_Y_Z_tag_confidence_backfill`: walks every `InventoryRow.tags` and rewrites entries from plain string to structured dict. Idempotent (skips rows already in structured format).
3. Deploy.

No data loss. Existing tags are preserved; their confidence is conservatively backfilled.

## 5. Version bump

Minor (Y in the semver). The schema change is internal (JSON contents), not external (no DDL), so it doesn't warrant a major bump.

## 6. Open questions

1. Should the review-tags surface also support bulk actions ("confirm all Synergy auto-tags on cards in this deck"), or is per-card review sufficient at the playgroup scale of 100 cards per deck?

2. Does the commander-context awareness in 2.3 apply only to user-built decks, or also to imported decklists before the user has touched them? Imported decks should arguably get a "review your tags" prompt as part of the import flow.

3. How are partner commanders and Background pairs handled? The current single-commander logic in `extract_commander_themes` already supports multiple commander rows; the question is whether tag-confidence should differ based on which commander dominates the theme.

4. Should the auto-tagger run on every InventoryRow on every page load, or only on explicit user action (the existing Retag button)? Performance is fine at 100 cards per deck, but if the auto-tagger gets richer, on-demand may be preferable.
