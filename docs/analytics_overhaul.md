# Deck Analytics Overhaul

**Status:** design / pre-implementation
**Replaces:** Bracket V2 estimator (`app/bracket_v2_service.py`) and the legacy
`compute_deck_bracket` in `app/deck_service.py`
**Authoring date:** 2026-05-11

---

## 0. Premise

Single-number power-level scoring is fiction. The Commander community's own
"bracket" framework is fuzzy by design — "casual," "upgraded," and "optimized"
don't have measurable definitions; they describe a vibe and a social contract,
not a deck property. Bracket V2 tried to bolt mechanics + intent + a 0-100 soft
score on top of that fuzziness. The result is two competing displays
(mechanical bracket vs intent bracket), a "Power score" that is explicitly
labeled informational and never affects anything, and a confidence stack that
the user has to interpret without a baseline.

The replacement plan: **stop producing a power level. Surface the underlying
facts.** A deck has objective mechanical signals (these tutors, this avg CMC,
2 mass-land-denial cards), an empirical play record (5W-3L over 8 games, avg
elimination turn 9), and a position within the user's own playgroup
(this deck wins more than 70% of your active decks). Each of those is a fact,
not a vibe. The user — not the system — synthesizes them.

This doc covers what comes out, what goes in, and how to land the change
without breaking the live system.

Source-of-truth framing: Mana Archive's analytics are anchored in the user's
own data and the playgroup's actual game history. They do not compare a deck
to community averages, aggregate inclusion rates, or other users' decks
outside the playgroup. External services (EDHREC, Commander Spellbook) are
integrated as inline enrichment where they add value (per-card inclusion
percentages, combo detection), but never as the primary signal. This
positioning is intentional and described in the North Star section of the
roadmap.

---

## 1. Inventory — what's being deleted

### 1.1 Python modules and functions

| Location | What | Disposition |
|---|---|---|
| [app/bracket_v2_service.py](../app/bracket_v2_service.py) | Entire module (~970 lines). Exports `tag_card_from_oracle`, `upsert_card_tags`, `estimate_bracket_v1`, `estimate_bracket_v2`, `derive_intent_bracket`, `resolve_mechanics_intent`, `compute_soft_score`, `derive_combo_role`, `persist_estimate`. Dataclasses `AutoTag`, `Finding`, `BracketEstimate`. | **Delete entirely.** A subset of the oracle-text auto-tagger logic (`tag_card_from_oracle`) is salvageable as a private helper inside the new analytics module if needed — but the `card_tags` table that backs it is also being dropped (see §6), so the auto-tagger has nowhere to write. Per-row tags on `InventoryRow.tags` (drives Synergy/Health) are unchanged. |
| [app/deck_service.py:873-956](../app/deck_service.py#L873-L956) | `compute_deck_bracket(all_rows, combos)` — legacy V1 floor-based estimator. Returns `{bracket, reasons, signals}`. | **Delete.** |
| [app/deck_service.py:1084-1085](../app/deck_service.py#L1084-L1085) | In `list_decks()`: `combos = compute_deck_combos(all_rows); deck.bracket = compute_deck_bracket(...)`. | **Replace.** `list_decks` will attach the new `deck.composition_signals` + `deck.play_record` summary objects instead. The expensive `compute_deck_combos` call moves out of `list_decks` — it does not need to fire on every Decks page load. |
| [app/main.py:43](../app/main.py#L43) | `from app.deck_service import compute_deck_bracket` | **Remove import.** |
| [app/main.py:1660-1732](../app/main.py#L1660-L1732) | `deck_detail_page` initial render: lazy-loads V2 estimate, populates `bracket_v2` template var (mechanics_bracket, intent_bracket, score, confidence, findings, rules_version). | **Delete the entire `bracket_v2` block.** Replace with composition/play/comparison context. |
| [app/main.py:1776-1820](../app/main.py#L1776-L1820) | `panels_endpoint`: re-runs V2 estimator with combo data, persists, builds legacy `bracket` dict for the lazy-loaded badge in the hero. | **Delete the V3 re-run block.** The panels endpoint still computes combos + tokens; it just stops persisting bracket estimates. |
| [app/main.py:1964-1986](../app/main.py#L1964-L1986) | `POST /decks/{id}/intent` — persists the 5-answer intent survey. | **Delete the route.** Survey is gone. |

### 1.2 Templates

| File | What | Disposition |
|---|---|---|
| [app/templates/deck_detail.html:54-199](../app/templates/deck_detail.html#L54-L199) | `bracket-v2-panel` section: badge, mechanics/intent rows, signal-density + mechanics-clarity + intent-alignment confidence bars, findings list, 5-question intent survey form. | **Delete the entire `{% if bracket_v2 %}` block.** Replace with the new analytics panel (§5). |
| [app/templates/deck_detail.html:45-46](../app/templates/deck_detail.html#L45-L46) | `#deck-bracket-placeholder` lazy-load span in the hero. | **Delete.** |
| [app/templates/_deck_panels.html:2-15](../app/templates/_deck_panels.html#L2-L15) | `#deck-bracket-fragment` lazy-loaded bracket badge fragment. | **Delete the fragment.** The panels endpoint keeps emitting tokens/combos/synergy/upgrade-targets. |
| [app/templates/decks.html:49,64-78](../app/templates/decks.html#L49-L78) | "Bracket" column header + per-row `bracket-badge` with reasons popover. | **Replace.** Column becomes "Record" (W-L from `get_deck_record`) — see §5.2. |

### 1.3 CSS

`app/static/style.css` has 39 selectors matching `.bracket-` or `.bracket-v2-`
(badge colors per bracket, popout/findings/confidence bars, intent-survey form
layout). All are dead after the templates are updated — strip the block. Keep
`.consistency-badge` and `.cs-*` (Health score, unchanged).

### 1.4 Database schema

Five tables created by `scripts/migrate_v3_15_0_bracket_v2_tables.py` and one
column set added by `scripts/migrate_v3_15_1_bracket_v2_intent_confidence.py`:

| Table / column | Purpose | Disposition |
|---|---|---|
| `commander_bracket_rules` | Tier thresholds (max_game_changers, allows_mld, etc.) | **Drop table.** No replacement. |
| `game_changer_cards` | WotC Game Changer list seeded from Scryfall `is:gamechanger`. | **Keep table, repurpose.** The list itself is still a useful signal (Layer 1 surfaces "this deck contains N WotC Game Changers" as a fact). Source-of-truth for the list. |
| `card_tags` | Per-card intrinsic auto-tags from oracle-text rules. | **Drop table.** Layer 1 reads oracle text directly during a one-shot pass per deck load and consults frozenset name lists + a small regex set — no precomputed cache. Total deck is ~100 cards; oracle-text scanning is fast enough not to need a denormalized cache. The per-row `InventoryRow.tags` (user-assigned, drives Synergy/Health) remains untouched. |
| `deck_bracket_estimates` | Persisted bracket + score + confidence per deck per rules version. | **Drop table.** Analytics are computed on read; nothing to persist. |
| `deck_bracket_findings` | Per-estimate findings list. | **Drop table.** |
| `decks.intent_pod`, `intent_speed`, `intent_combo`, `intent_winning`, `intent_played` | 5-answer survey columns. | **Drop columns.** The survey produced an "intent_bracket" that fed mechanics-vs-intent reconciliation logic. With brackets gone, intent has nothing to reconcile against. (If you want to keep a single user-defined free-text label, see §7.) |

### 1.5 Migration scripts and runner

`scripts/run_migrations.py` registers five Bracket V2 migrations
(`v3_15_0_bracket_v2_tables`, `v3_15_0_seed_bracket_rules`,
`v3_15_0_seed_game_changers`, `v3_15_0_seed_card_tags`,
`v3_15_1_bracket_v2_intent_confidence`). The migrations themselves stay in the
repo for historical reference and aren't re-run (the `schema_migrations` table
already marks them applied). The new teardown migration (§6) drops the tables
they created. The runner registration lines for the seed migrations can be
removed alongside the bracket service module — they're already idempotent /
already-applied for current installs.

### 1.6 What stays

These look bracket-adjacent but are independent and **do not change**:

- `app/spellbook.py` and `compute_deck_combos` — still drives the Win
  Conditions panel and feeds Layer 1's combo signal.
- `InventoryRow.tags` JSON + `app/deck_service.py::suggest_card_roles` /
  `get_row_tags` — drives Synergy and Health panels. A separate work stream
  (the "Tag system accuracy overhaul" in Tier 2 of the roadmap) is sequenced
  ahead of this overhaul and is expected to improve the precision of the
  auto-tagger that populates `InventoryRow.tags`. The composition signals in
  Layer 1 (see section 2) consume both oracle-text-derived signals (computed
  on the fly) and user-confirmed tags from `InventoryRow.tags`; the latter
  become more reliable as the tag work lands.
- `compute_deck_health`, `compute_consistency`, `compute_deck_analytics`,
  `compute_deck_synergy`, `extract_commander_themes` — none of these depend
  on Bracket V2.
- `Game` / `GameSeat` tables and `app/game_service.py` — already produce per-deck
  W-L records; Layer 2 extends what gets read out, but the schema is sufficient
  as-is.
- `compute_dead_cards` in `app/deck_service.py` — currently produces the
  Upgrade Targets list shown on the deck detail page. Stays for now, but is
  slated to be replaced by the AI Phase 1 recommendation engine (Tier 3 of
  the roadmap). Users have reported that the current implementation's
  accuracy is limited, which the AI replacement is designed to address. No
  changes to `compute_dead_cards` are required as part of this analytics
  overhaul; the eventual replacement is tracked separately.

---

## 2. Layer 1 — Objective deck composition signals

**Goal:** surface mechanical facts about the cards in a deck. No score, no
threshold, no "this is Bracket 3." A list of signals with counts and example
cards, each independently meaningful.

### 2.1 Signals to compute

Each signal returns `{count: int, cards: list[str]}` unless noted. Counts use
"distinct named cards" (not row quantity sums) — a deck with 4 copies of Sol
Ring still shows "1 fast mana card" because the *signal* is "deck contains Sol
Ring," not "deck has 4 Sol Rings."

| Signal | Source | Notes |
|---|---|---|
| **Game Changers** | `game_changer_cards.card_name` join (table kept from §1.4). | The WotC-curated list. Surface count + names. |
| **Fast mana** | Frozenset of names: Mana Crypt, Mox Diamond, Chrome Mox, Mox Opal, Jeweled Lotus, Grim Monolith, Mana Vault, Lotus Petal, Ancient Tomb, plus the rest of the existing `_FAST_MANA` list. | Already exists in `deck_service.py`. |
| **Free interaction** | Frozenset: Force of Will, Force of Negation, Mana Drain, Fierce Guardianship, Deflecting Swat, Flusterstorm, Mental Misstep, Pact of Negation, Commandeer. | Already exists. |
| **Mass land denial** | Frozenset: Armageddon, Ravages of War, Jokulhaups, Devastation, Obliterate, Decree of Annihilation, Catastrophe, Ruination, Boom // Bust. | Already exists. |
| **Extra-turn spells** | Oracle text contains `take an extra turn`. | Already exists. |
| **Tutors (unconditional)** | Oracle text `search your library for a card` — minus the "land" disambiguation lookbehind already in `compute_deck_bracket`. | Same logic as today. |
| **Tutors (restricted)** | Oracle text matches `search your library for (a|an|up to N) (creature|artifact|enchantment|instant|sorcery|planeswalker)` etc. | Optional separate row; if implementation complexity isn't worth the visual distinction, fold into a single "tutors" count. |
| **Counterspells** | Oracle text starts with `counter target` OR contains `counter target spell`. | New signal. |
| **Board wipes** | Existing `_WIPE_RE` pattern from `deck_service.py`. Reuse `compute_deck_health` already counts this. | Pull from the same place Health uses; do not re-implement. |
| **Combos present** | `len(compute_deck_combos(rows)["included"])`. | Cache via existing panels-cache so the Decks page doesn't fire the Spellbook API per deck. |
| **Average CMC** | Existing `compute_deck_analytics` already returns `avg_cmc`. | Reuse. |
| **Mana curve** | Existing `compute_deck_analytics.curve`. | Reuse. |
| **Land count** | Count of cards with `Land` in `type_line` (quantity-weighted). | Easy add. |
| **Color identity** | Already on `Deck.color_identity` (set by `list_decks`). | Reuse. |

### 2.2 Where the computation lives

Add a single new function to `app/deck_service.py`:

```python
def compute_composition_signals(rows: list, combos: dict) -> dict:
    """Return objective deck-composition signals as separate facts."""
```

The function operates on already-loaded rows (no DB calls except the
`game_changer_cards` lookup, which can be batched). For the Decks list page,
the signals are summarized to integer counts only (no card-name lists) to keep
the page light; the deck detail page gets the full payload with card lists.

### 2.3 What this is NOT

- Not a power score. The function returns dict-of-facts.
- Not threshold-based ("≥4 fast mana → red flag"). The UI may color a row,
  but the threshold lives in the template, not the data layer, and is
  comparative (§4), not absolute.
- Not predictive. We report what the deck contains, not what it can do.

---

## 3. Layer 2 — Empirical play record

**Goal:** for each deck, surface what has actually happened when the deck was
played. This is the layer that distinguishes "looks scary on paper" from
"actually wins."

### 3.1 Stats to surface per deck

All derived from existing `Game` + `GameSeat` rows. Schema is sufficient as-is
([app/models.py:171-204](../app/models.py#L171-L204)):

| Stat | Definition |
|---|---|
| **Games played** | Count of `GameSeat` rows where `deck_id = X` AND `placement IS NOT NULL` (placement-set implies the game ended). |
| **Wins** | Same scope, `placement = 1`. |
| **Losses** | `games_played - wins`. |
| **Win rate** | `wins / games_played` (display as percentage; show N/A below ~3 games). |
| **Average finish** | Mean of `placement` across all ended games. Lower = better. |
| **Last played** | `MAX(Game.played_at)` joined on the deck's seats. |
| **Average game length when won / lost** | `AVG(Game.turn_count)` partitioned by win/loss. Tells you whether this deck wins quickly or grinds out long games. |
| **Final-life-when-lost** | `AVG(GameSeat.final_life)` where `placement > 1`. Negative final lives suggest you got combo-killed; close-to-zero suggests grindy losses. Informational. |

### 3.2 Where the computation lives

Extend `app/game_service.py`:

```python
def get_deck_play_stats(session, deck_id: int) -> dict:
    """Single-deck empirical record."""
```

Returns the dict above. One query (joined `GameSeat` × `Game` filtered by
`deck_id`), aggregated in Python. Existing `get_deck_record` becomes a thin
wrapper over this (kept for backward compat with the deck-detail hero W-L
display).

For the Decks list page, add:

```python
def get_play_stats_for_decks(session, deck_ids: list[int]) -> dict[int, dict]:
    """Batch version. One query covering all of the user's decks."""
```

### 3.3 Honest unknowns to surface

If a deck has <5 games played, **say so**. Show `5W-3L (8 games)` not `63% WR`.
A 1-0 deck is not 100% win rate. The display rule: show counts always, show
percentages only when N ≥ 5.

If a deck has 0 games, the panel says "No games recorded yet" and links to
`/games/new`. Don't show a placeholder zero.

---

## 4. Layer 3 — Comparative context (playgroup-relative)

**Goal:** for any signal in Layer 1 or Layer 2, express where this deck sits
relative to the user's other decks. "5 tutors" is meaningless without knowing
the user's median deck has 1. "60% win rate" is meaningless without knowing
the average is 25% in a 4-player pod.

### 4.1 Definition of "active playgroup"

"Active decks" = the user's decks where `last_played` is within the last
90 days. The 90-day window biases the comparison toward decks the user
actually plays, so an old jank deck that hasn't been touched in a year doesn't
distort percentiles.

Decks with 0 games played are excluded from the comparison cohort entirely
(they have no Layer 2 data and would skew Layer 1 distributions, since
build-but-don't-play decks tend toward casual). They still get their own
detail page with Layer 1 signals; they just can't *compare to others* until
they're played.

If the user has fewer than 3 active decks, the comparison panel collapses to
"Need more decks for comparison" — quartiles on 1-2 decks are noise.

### 4.2 Comparison stats

For each numeric Layer 1 / Layer 2 signal, compute the deck's **percentile
rank** within the user's active cohort:

```
percentile = (count_of_decks_with_lower_value / total_active_decks) * 100
```

Surfaced as:

- "Top 25% in tutors among your active decks"
- "Below median for win rate"
- "Above median average CMC (slower than your other decks)"

The percentile itself is not displayed as a number ("82nd percentile" is
data-vibes again). The label is qualitative: **top quartile / above median /
below median / bottom quartile**, plus the comparison cohort size in
parentheses ("vs your other 6 active decks").

### 4.3 What gets compared

| Signal | Compared? | Why |
|---|---|---|
| Game Changers (count) | Yes | The whole point of WotC's list is comparative. |
| Fast mana (count) | Yes | Same. |
| Free interaction (count) | Yes | |
| Tutors (count) | Yes | |
| Mass land denial (count) | Yes | |
| Counterspells (count) | Yes | |
| Combos (count) | Yes | |
| Average CMC | Yes | Above-median CMC vs the user's casual decks is a useful self-knowledge signal. |
| Land count | No | Comparison isn't meaningful — depends on deck type. |
| Win rate | Yes | Most important comparison. |
| Average finish | Yes | (Reversed — lower is better.) |
| Average game length | Yes | "This deck wins faster than your other winning decks" — meaningful. |
| Games played | No | Comparing play counts produces "use this deck more" advice that doesn't belong in analytics. |

### 4.4 Where the computation lives

Add to `app/deck_service.py`:

```python
def compute_playgroup_context(session, user_id: int, current_deck_id: int) -> dict:
    """Return per-signal percentile context for current_deck within user's active cohort."""
```

Single function called once per deck-detail load. Returns a dict keyed by
signal name with the qualitative label and the cohort size. The deck-detail
template renders each Layer 1 / Layer 2 signal alongside its context label.

The cohort computation is shared (all decks fetched once) — caching is not
needed for the Decks list page since percentiles re-render per page load and
the user typically has <20 decks.

---

## 5. UI design — what replaces the Bracket V2 panel

### 5.1 Deck detail page

Replace the deleted `bracket-v2-panel` block with three stacked sub-panels in a
single new "Deck Profile" section. They go in the same vertical position
(below hero stats, above the Mana Curve / Analytics panel).

```
+--- Deck Profile ----------------------------------------------+
| Composition (Layer 1)                                          |
|   • 2 Game Changers (Mana Crypt, Smothering Tithe)             |
|       Top quartile in your active decks                        |
|   • 1 fast mana card (Mana Crypt)        Above median          |
|   • 4 tutors (Demonic Tutor, ...)        Top quartile          |
|   • 0 mass land denial                   Below median          |
|   • 1 combo line present                                       |
|   • Avg CMC 3.2                          Above median          |
|   ▸ Mana curve [reuse existing curve viz]                      |
+----------------------------------------------------------------+
| Play Record (Layer 2)                                          |
|   8 games · 5W-3L · 63% wins (vs your active 4-player decks)  |
|   Avg finish: 1.8  ·  Last played: 4 days ago                  |
|   Wins typically end turn 7  ·  Losses average turn 11         |
|   Top quartile win rate in your active decks                   |
+----------------------------------------------------------------+
| Playgroup Context (Layer 3)                                    |
|   Compared to your 6 other active decks (played in last 90d):  |
|     Wins more, tutors more, slightly slower mana curve.        |
|     [3-sentence prose summary derived from the percentile dict]|
+----------------------------------------------------------------+
```

Implementation notes:

- "Composition" is a flat list of signal rows. Each row shows count + sample
  card names (truncated to 2-3 with "and N more" link) + the qualitative
  comparison label inline. Zero-count signals are still shown (the *absence*
  of mass land denial is itself a fact users want to see) but greyed.
- "Play Record" renders nothing but the "no games yet" prompt when the deck
  has 0 games.
- "Playgroup Context" is the only sub-panel that produces narrative text.
  Compose deterministically from the percentile dict — pick the 2-3 most
  outlying signals and write a templated sentence. No LLM, no fuzziness.
  If the user has <3 active decks, this sub-panel is replaced by a single
  line: *"Need 3+ active decks for playgroup comparison."*

### 5.2 Decks list page

Today's columns: Name · Format · Total Cards · **Bracket** · Health · Edit.

After:                  Name · Format · Total Cards · **Record** · Health · Edit.

- **Record column** renders `5W-3L` for decks with games. `—` for unplayed
  decks. Color the cell green if win rate ≥ 60%, neutral otherwise.
  No bracket badge.
- Hovering / opening a `<details>` on the record cell shows the same Layer 1
  composition counts that the bracket popout used to show — Game Changers /
  fast mana / tutors / combos — as plain rows. No popout title that says "Why
  bracket N"; just the fact rows.
- The Health column is unchanged.

### 5.3 Lazy loading

Layer 1 composition signals are cheap and computed inline during
`deck_detail_page`. Layer 2 play stats are cheap. Layer 3 percentile context
requires loading all active decks' signals — keep this in the lazy panels
endpoint (`/decks/{id}/panels`) so the initial page paints fast and the
playgroup-context sub-panel slides in.

Cache the active-cohort signal payload in the existing panels-cache layer with
a 1-hour TTL keyed on `user_id` + active deck set hash. Invalidate when any
deck is edited or a new game is recorded.

## 5.4 What this overhaul enables downstream

The three-layer composition + play record + playgroup context output is
designed to be consumable by downstream features, not just rendered on the
deck detail page. The two specific downstream consumers in the near-term
roadmap are:

- The **AI-powered upgrade suggestions** feature (Tier 3 of the roadmap). The
  AI prompt will receive the same composition signal payload that the UI
  renders, plus the user's collection inventory, plus an optional free-text
  "deck intent" string from the user. The AI's recommendations are then
  grounded in the deck's actual composition rather than in generic
  deck-building heuristics.

- The **playgroup-relative comparison** itself (Layer 3 here). A user with
  multiple active decks gets a per-signal comparison without ever leaving
  the app. This is what distinguishes Mana Archive's analytics from a
  Moxfield-or-Archidekt analytics page: the comparison cohort is the user's
  own decks and the playgroup's decks, not the anonymous community.

Both downstream consumers benefit from the same payload structure, so
`compute_composition_signals` (section 2.2) should be designed with reuse in
mind. Returning a flat dict of `{signal_name: {count, cards,
percentile_label}}` is sufficient and avoids over-engineering the return
type.

---

## 6. Migration plan

### 6.1 Sequencing

Single deploy unit (one version bump, one tag), in this order:

1. **Code change** — new functions in `deck_service.py` / `game_service.py`;
   updated templates; deleted bracket service module; deleted intent route;
   deleted decks-list bracket column; deleted bracket CSS block.
2. **Migration `v3_17_0_drop_bracket_v2`** — drops the five Bracket V2 tables
   and the five `decks.intent_*` columns. Idempotent (`DROP TABLE IF EXISTS`,
   conditional column drops via PRAGMA + table rebuild for SQLite).
3. **Migration `v3_17_0_drop_card_tags_only`** — separate script for the
   `card_tags` table specifically, because it's the largest (one row per
   tagged card × tag, multiple thousands). Keeping the drop separate lets the
   teardown be reversed independently if needed.

Both migration scripts go in `scripts/`, registered in
`scripts/run_migrations.py` after the v3.16.1 entries. They run on the next
deploy via the startup hook in `main.py:on_startup()`.

### 6.2 No data preservation

The Bracket V2 estimates are derived data — nothing in `deck_bracket_estimates`
or `deck_bracket_findings` is user-authored. Dropping the tables loses nothing
the user would care about. The intent-survey columns *are* user-authored
(5 dropdowns per deck) — but they only existed to feed the
mechanics-vs-intent reconciliation that this overhaul deletes. Honest
conversation in the release notes: "the intent survey is being removed because
the bracket reconciliation it fed is also being removed." If a user wants to
preserve a deck-level note, they have the existing `Deck.notes` text field.

### 6.3 What if someone has Bracket V2 data they care about?

Provide a one-shot script `scripts/export_bracket_v2_estimates.py` that dumps
the contents of `deck_bracket_estimates` + `deck_bracket_findings` +
`decks.intent_*` to a CSV per user under `/data/legacy/`. Run it once during
the migration *before* the drops. Users who want to see "what bracket did the
old system think this deck was" can read the CSV. The export is a courtesy,
not a feature — there's no UI for reading it back.

### 6.4 Rollback

The deletion is one-way. Rolling back the code change without rolling back the
migration leaves the app calling code paths that read tables that exist but
are empty — confusing but not broken (the `try/except` blocks around bracket
calls already swallow failures). Rolling back both code and migration is a
manual restore from the pre-deploy DB backup. Document this in the release
notes; don't engineer a reverse migration for a feature this user has already
decided to discard.

### 6.5 Version bump

`v3.17.0` — major-enough feature change for a minor bump, not a patch. Bracket
V2 was a v3.15.0/v3.15.1 minor bump; replacing it warrants the same.

---

## 7. Optional: user-assigned `power_label`

Out of scope for the initial overhaul, but worth a decision.

### 7.1 The case for it

Users may want a free-text label per deck ("casual," "Friday night,"
"meet-the-pod," "table 1") to express social context that no amount of data
captures. The Layer 1/2/3 split deliberately strips out social intent — Layer 7
puts a thin, optional version back in, controlled by the user.

### 7.2 Proposed shape

Add a single nullable column: `Deck.power_label: VARCHAR(64)`. No enum, no
dropdown of predefined values, no validation. Display it as a small badge in
the deck-detail hero and the Decks list (replacing the current Bracket
column's secondary slot, not adding a new column). Editable from the existing
deck Edit popout.

### 7.3 Reasons to skip it

- It's exactly the thing this overhaul is rejecting: a user-supplied fuzzy
  label that has no measurable definition. Surfacing it next to objective
  signals risks dragging the eye back to "what's the *real* number" thinking.
- `Deck.notes` (existing free-text field) already handles this use case for
  the small minority of users who want it. They can write `[Casual]` at the
  top of their notes; the analytics layer doesn't need to care.
- It's a nontrivial UI surface (display + edit + Decks list column / chip)
  for a feature with weak demand signal.

### 7.4 Recommendation

**Don't add `power_label` in the initial v3.17 deploy.** Ship the three-layer
analytics, observe whether users miss having a label, and add it as a v3.17.x
patch only if asked. The migration in §6 already drops the intent columns —
adding `power_label` later is one tiny ALTER TABLE, not a structural change.

---

## 8. Open questions

1. **Should the Decks list page show Layer 3 percentile labels?** Per §5.2 it
   currently doesn't (just the raw W-L). The argument for adding qualitative
   labels ("top quartile · 5W-3L") is that the list page is where users
   actually pick decks; the argument against is column-width and visual
   density. Default: leave it off for v3.17.0, revisit if the deck-detail
   panel proves useful.

2. **Decks list combo computation.** The new `compute_composition_signals`
   wants a `combos` argument; today `list_decks` calls `compute_deck_combos`
   for every deck on every Decks list load (with a per-card-set in-memory
   cache). That should stay — the cache hits ~100% for re-loads — but if cold
   starts on the Decks page become a problem after this change, consider
   making the combos lookup lazy on the Decks list (don't show combo count,
   only show on detail). Punt this until measured.

3. **Average game length** in Layer 2 requires `Game.turn_count` to actually
   be populated. Today this is collected by the game-tracker's End Game form
   but not enforced. If `turn_count` is null for ~50% of games, the "wins
   typically end turn N" line in §5.1 is misleading. Audit the existing data
   before relying on this stat; if coverage is poor, drop the line from the
   initial panel and add it back once future games are filling the field.
   The playgroup has not yet logged any games as of this writing, so
   historical coverage is zero. Layer 2 stats will populate over time as
   games are recorded. The "wins typically end turn N" line in section 5.1
   should be hidden until at least 5 games with non-null turn_count exist
   for the deck in question.

4. **The "no games" deck experience.** A deck with 0 games shows Layer 1
   signals fine, gets a "No games recorded yet" Layer 2 panel, and is
   excluded from Layer 3 cohort. That's the design. Worth a one-time prompt
   in the UI: "Track your next game to unlock comparison context"? Maybe.
   Defer until v3.17.1.

5. **Should the analytics overhaul ship before, after, or alongside the tag
   system accuracy overhaul?** The roadmap sequences tags first, but a
   strict "tags first, then analytics" interpretation could delay the
   analytics overhaul by weeks. An alternative is to ship the analytics
   overhaul with the current tag system as-is (Layer 1 will be less reliable
   for cards with weakly-tagged roles), then backfill accuracy as the tag
   work lands. Recommend: ship the analytics overhaul with the existing tag
   system and improve the inputs over time. The overhaul's value is in the
   three-layer structure, not the absolute accuracy of any single signal.
