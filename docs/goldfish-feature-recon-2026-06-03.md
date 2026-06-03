# Goldfish feature recon — 2026-06-03

**Scope:** fact-gathering for three goldfish playtester features. **RECON ONLY** —
no code changed, no version bumped, nothing staged. Surface = `GET
/decks/{deck_id}/goldfish` → `app/templates/goldfish.html` + `app/static/goldfish.js`
+ `app/routes/goldfish.py`.

---

## SECTION A — Foundational facts (gate all three features)

### A1. Persistence key — **goldfish does NOT persist game state. Session-only.**

`goldfish.js` reads/writes localStorage in exactly **one** place, and it is a
*preference*, not game state:

- `localStorage["cartarch-goldfish-autodraw"]` = `"true"`/`"false"` — the
  "Draw on new turn" toggle (`goldfish.js:2321` read, `:2333` write).

There is **no** save/restore of the board. The board is built fresh on every page
load from a JSON blob inlined in the page (`payload = JSON.parse(document.getElementById("gf-deck-data").textContent)`,
`goldfish.js:139`) and shuffled. Reloading the page = new game; in-progress board
state is lost by design. `pagehide` only clears the `gf-mode` body class (scroll
lock), it does not serialize state.

- **`gameFingerprint()` is NOT used by goldfish at all.** That function lives in
  `game_detail.html` and keys the *live multiplayer game tracker's* localStorage —
  a completely separate subsystem. The "82/83-release streak" invariant is about
  that tracker, **not** goldfish.

**Consequence:** the A1 fingerprint-coupling worry does not apply here. See A4.

### A2. State shape

Per-card battlefield/permanent object (`buildInstances`, `goldfish.js:158`;
`createToken`, `:1676`):

```
inst = {
  id:         "gf-<n>" (or "gf-tok-<n>" for tokens),
  card:       <the per-card payload dict>  // synthetic for tokens
  tapped:     false,
  counters:   { <label>: <int count> },    // v3.30.5; battlefield-only
  attachedTo: <host instance id> | null,    // v3.30.6; render pointer only
  // tokens additionally carry: isToken:true, power, toughness (user-typed)
}
```

**Zones** (`ZONE_LISTS`, `goldfish.js:208`): `library, hand, graveyard, exile,
command`, plus the battlefield **partitioned into five type-based regions**:
`bf-creatures, bf-lands, bf-artenc, bf-pwbattle, bf-other`.

**What drives the sub-zone:** `classifyRegion(card)` (`goldfish.js:238`) parses
`card.type_line` with strict priority Creature → Planeswalker/Battle →
Artifact/Enchantment → Land → Other. **There is no stored type/category field** —
region is derived live from `type_line` on every placement, and a card is only
ever in one region's array. The user does **not** pick a battlefield region
directly; the per-card menu offers a single "Move → Battlefield" that routes
through `classifyRegion` (`buildMenuItems`, `:1979-1997`).

### A3. Existing infra

**(a) Per-permanent counters — YES, a full mechanism already exists (v3.30.5+).**
`inst.counters` is a `{label → int}` map, battlefield-only, cleared by `moveTo`
when a card leaves the battlefield. Single mutator: `adjustCounter(instId, label,
delta)` (`goldfish.js:1241`, clamped at ≥0, deletes key at 0). Curated labels
(`COUNTER_LABELS`, `:1168`) = **`+1/+1, -1/-1, Loyalty, Charge, Experience, Quest`**
plus a Custom… free-text path. Any `±X/±Y`-shaped label is rendered as a
multiplied P/T modifier (`PT_LABEL_RE`, `formatCounterDisplay`, `:1206/1199`).
UI: a "Counters…" panel (`openCounterPanel`) reachable from the per-card menu,
plus click-pill-to-adjust on the on-card chips (`:1311`). **Loyalty is already a
first-class manual counter label**, with a brass-gold chip tint.

**(b) Mana tracker.** `state.manaPool = {W,U,B,R,G,C}` (`goldfish.js:119`),
**session-only** (never persisted). Cleared by `clearManaPool()`
(`:491`), which is called from **`newTurn()`** (`:377`) and `newGame()` (`:368`).
So "mana clears on new turn" is intentional and correct.

### A4. Change consequence — **state-shape changes are FREE.**

Because goldfish state is session-only (A1) and uncoupled from `gameFingerprint()`:

- Adding per-instance fields (e.g. a P/T override, a region override) or
  session-level fields (e.g. a damage counter) **forces no version bump and no
  fingerprint bump**.
- The only thing any such change "wipes" is the **current in-memory goldfish
  session on reload — which already resets on every reload regardless.** Live
  multiplayer games are untouched (separate subsystem).
- The one backward-compat note already baked into the code: reads use
  `inst.counters || {}` / `inst.attachedTo` with null-safe defaults so a stale
  open tab running an older shape doesn't throw. New optional fields should follow
  the same null-safe-read convention; no migration, no reset needed.

---

## SECTION B — Per-feature recon

### B1. Damage / opponent-life counter

- **Where it lives today:** the player's own life is `state.life` (init 40),
  shown in the masthead HUD (`gf-stat-life`, `goldfish.html:20`) and adjusted by a
  "Life" control group with −1/+1/−5/+5 buttons (`goldfish.html:74-78`,
  `data-life-delta`). Turn shows in the same masthead (`gf-stat-turn`, `:21`).
  `render()` writes both (`goldfish.js:638-639`).
- **Recommendation:** add a **session-level** counter (a new `state.*` field, e.g.
  `state.oppLife` or `state.damageDealt`), a new masthead stat next to Life/Turn,
  and a control group mirroring the existing Life one. Wire its readout into
  `render()` and reset it in `newGame()` (and optionally `newTurn()` if it's a
  per-turn damage tally — owner decides). **Session-only is correct** — persistence
  isn't implemented and isn't worth adding for this (A1).
- **Open Q for owner (report both, do not decide):**
  - *(i)* Configurable starting life (20/40) counting **DOWN** ("opponent life,
    turns-to-lethal"), or
  - *(ii)* a plain damage-dealt counter counting **UP**.
  - These differ only in init value + button direction + a reset choice; either is
    a thin addition to the same HUD slot.
- **Classification:** **pure frontend.** No seam touch. No state-shape risk (A4).

### B2. Planeswalker auto-loyalty

- **GATING RESULT: loyalty is NOT available — the spec SPLITS.** A prerequisite
  seam-extension release must land first.
  - `Card` model (`app/models.py:81`) has **no `loyalty`** (nor `power`/`toughness`).
  - The `scryfall_cards` cache seam is **22 keys and contains no `loyalty`**
    (`_CACHE_COLUMNS`, `app/scryfall.py:405-409`; `_normalize_card_payload`,
    `:62`; `_cached_row_to_payload`, `:413`). The 22nd key is `produced_tokens`.
  - The goldfish route payload (`app/routes/goldfish.py:88-107`) sends per card:
    `inventory_row_id, card_id, name, set_code, collector_number, image_url,
    mana_cost, cmc, type_line, oracle_text, colors, color_identity, quantity,
    is_commander` — **no loyalty**.
- **Request-path invariant:** goldfish may NOT live-fetch loyalty per card. So the
  data must come from cache, which means **extend the seam first**:
  1. Append `loyalty` **LAST** to the seam (now 23 keys): `_normalize_card_payload`
     return dict, `_CACHE_COLUMNS`, `_cached_row_to_payload`, and (if loyalty
     becomes an ORM column) `card_constructor_kwargs`/the `Card` model.
  2. Daemon backfill off the request path (`_bulk_data_loop` re-ingest, same
     pattern as `produced_tokens` in v3.30.11).
  3. **Move the pinned counts:** `tests/test_scryfall_cache.py` has a **hard
     `len(_COLS) == 22` assertion** (`:234-238`, plus the order/shape test at
     `:223-235` and the labels at `:17`, `:351`) — these must change to 23.
     `tests/test_bulk_data_loop.py` imports `_CACHE_COLUMNS` (`:40`) and must move
     with the seam.
  4. Thread `loyalty` into the goldfish route payload (`goldfish.py` card dict).
  5. **Only then** the goldfish work: on a planeswalker entering the battlefield,
     auto-initialize its loyalty.
- **Planeswalker identification:** already done — `classifyRegion` routes
  `type_line` containing `"planeswalker"` to `bf-pwbattle` (`goldfish.js:242`).
- **Counter-widget reuse:** **YES.** "Loyalty" is already a curated counter label
  (A3a) with its own chip tint. Auto-loyalty = on ETB into `bf-pwbattle`, call the
  existing `adjustCounter(instId, "Loyalty", <printed loyalty>)`. No new counter
  primitive needed — only the data and the auto-init trigger. (Edge: variable-
  loyalty walkers like `X` print as non-numeric loyalty; the seam should store the
  raw string and the auto-init should no-op / leave manual when it isn't an
  integer.)
- **Classification:** **needs a cache-seam touch (prerequisite release)**, then a
  small frontend addition. Two-release sequence.

### B3. Manual permanent editing — type/zone (3a) + P/T (3b)

**3a — type/zone conversion:**

- A generic **"move permanent to zone" action already exists** for the
  cross-zone moves (Hand/Graveyard/Exile/Command/Library) via `buildMenuItems` →
  `moveTo` (`goldfish.js:1979-2002`). Battlefield→graveyard etc. is fully covered.
- **But there is NO manual choice of battlefield *region*.** "Move → Battlefield"
  always routes through `classifyRegion(type_line)`. So "convert a non-creature to
  a creature" (i.e. move an artifact into `bf-creatures`) has **no existing
  affordance** and `type_line` is the only signal. Options:
  - *(i)* add a per-instance **region/type override** field (null-safe, A4) that
    `classifyRegion`/render consult before falling back to `type_line`; or
  - *(ii)* add explicit "Move → Creatures / Artifacts&Ench / …" menu items that
    place directly into a chosen region (bypassing `classifyRegion`).
  - Either is pure frontend; *(i)* survives a later re-classification, *(ii)* is
    simpler but a re-place would re-derive from `type_line`.

**3b — power/toughness editing:**

- **Base P/T is NOT in the payload for real cards.** `Card` has no `power`/
  `toughness`, the seam has none, and the goldfish route sends none. The on-card
  **P/T badge renders for tokens ONLY** (`goldfish.js:789`), and those values are
  **synthetic** — typed by the user in the Create-token modal and stored on the
  token's invented `card` object (`createToken`, `:1722-1723`). Real creatures
  currently show **no P/T at all**.
- **Implication:** there is no "base" P/T to show or modify for deck creatures
  unless P/T is also added to the seam (same mechanism as B2: append `power`,
  `toughness` LAST, daemon backfill, move the test pins, thread into the route).
- **Editing model options (report both, do not decide):**
  - *(i)* **Reuse the existing `±X/±Y` counter mechanism** (A3a) — already works,
    already renders multiplied P/T modifiers, **no new field, pure frontend, no
    seam touch.** Shows a delta, not an absolute P/T.
  - *(ii)* **Free-text current-P/T override** — a new per-instance field rendered
    as an absolute badge. Pure frontend for the *override* itself, but showing it
    *relative to base* (or showing P/T on real creatures at all) requires the seam
    extension above.
- **Classification:** **3a = pure frontend.** **3b = pure frontend if done as
  counters (option i); needs a cache-seam touch if it must show real-card base P/T
  (option ii).**

---

## SECTION C — Per-feature summary + recommended order

| Feature | Frontend-only or seam touch? | State-shape / version impact | What a change wipes |
|---|---|---|---|
| **B1** Damage/opp-life counter | **Pure frontend** | New `state.*` field; **no version/fingerprint bump** | Nothing beyond the per-reload session (already resets) |
| **B2** PW auto-loyalty | **Seam touch required** (loyalty absent from model + 22-key seam + route payload) → prerequisite release, then frontend | Seam → 23 keys; test pins 22→23; **no goldfish-state version/fingerprint bump** | Nothing (reuses existing "Loyalty" counter) |
| **B3a** Type/zone convert | **Pure frontend** | Optional null-safe per-instance override field; **no bump** | Nothing beyond per-reload session |
| **B3b** P/T edit | **Frontend-only via counters (opt i)**; **seam touch if real-card base P/T needed (opt ii)** | Counters: none. Override: null-safe field. Base P/T: seam +2 keys, test pins | Nothing beyond per-reload session |

**Recommended implementation order:**

1. **B1 (damage/opp-life)** — smallest, pure frontend, no dependencies, directly
   answers the originating "mana clears on new turn" report by giving the user the
   counter they actually wanted. Ship first.
2. **B3a + B3b-as-counters** — both pure frontend, both extend the per-card menu /
   existing counter widget; natural to ship together as "manual permanent
   editing." Defer the base-P/T-on-real-cards variant (B3b opt ii) unless the
   owner wants real-card P/T display, since that pulls in the seam.
3. **Seam-extension release** (append `loyalty` — and `power`/`toughness` if B3b
   opt ii is chosen — LAST; daemon backfill; move `tests/test_scryfall_cache.py`
   + `tests/test_bulk_data_loop.py` pins). Standalone, data-only.
4. **B2 (PW auto-loyalty)** — after the seam release backfills, thread `loyalty`
   into the goldfish route payload and auto-init the existing "Loyalty" counter on
   planeswalker ETB.

**Key gating fact:** only B2 (and B3b option ii) cross the `scryfall_cards` seam;
everything else is pure frontend. Nothing in any of the three touches
`gameFingerprint()` or forces a version bump, because goldfish state is session-
only and uncoupled from the live game tracker.
