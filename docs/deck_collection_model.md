# Deck ↔ Collection Relationship

**Status:** design / decision document
**Authoring date:** 2026-05-12
**Trigger:** user reported confusion that Moxfield deck imports duplicate cards already in their collection — "Could you add an option to the importer to mark those cards as part of the deck without actually adding new copies to the collection?"

---

## 0. TL;DR

The current architecture is **already Model A** (decks reference inventory) and that's the right model. The bug is that the **import flow doesn't honor it** — it unconditionally creates new `InventoryRow` records for each deck card instead of reconciling against existing inventory first. We should keep Model A and fix the import.

Recommended direction: **Refined Model A.** Decks remain slices of inventory (current architecture). The import-to-deck flow gains an ownership-reconciliation step that detects cards the user already owns and proposes moving them into the deck instead of creating duplicates. Wishlist support (deck cards the user doesn't physically own) is a small additive feature that uses the same data model — no architectural rewrite.

Implementation scope: **2–3 Claude Code sessions** for the import reconciliation; **1 more** if/when wishlist support is added. No schema migration required for the core fix.

---

## 1. Current behavior — inventory

### 1.1 The schema

Per [app/models.py](../app/models.py):

- **`Card`** — global catalog row keyed on `scryfall_id`. One per printing across all users.
- **`InventoryRow`** — per-user, per-card, per-finish quantity. Has:
  - `card_id`, `user_id`, `finish`, `quantity`
  - `storage_location_id` (nullable — null when `is_pending=True`)
  - `is_pending` (true after import, false after placement)
  - `role` (`"commander"` or null)
  - `tags` (JSON list — per-row role tags)
  - `drawer` / `slot` (drawer-sorter physical-layout fields)
- **`StorageLocation`** — per-user, named location with `type` in `{root, drawer, binder, box, deck, other}`.
- **`Deck`** — per-user, named deck pointing at a `StorageLocation` of `type="deck"` via `storage_location_id`.

### 1.2 The conceptual model the schema encodes

> **A deck IS a storage location.** Cards "in a deck" are `InventoryRow` records whose `storage_location_id` points to that deck's storage location. There is no separate "deck card" table — the legacy `DeckItem` table was dropped in v3.5 because it duplicated `InventoryRow` records that were already filtered by `storage_location_id`.

This is **Model A** as the user framed it: every card in a deck is a quantity of inventory that lives at a particular location (the deck location).

### 1.3 Walking the four scenarios through the code

**Scenario 1: User imports a Moxfield/Archidekt deck via paste-list, picks "My Bello Deck" as the destination.**

[app/import_service.py:391-438](../app/import_service.py#L391-L438) builds an `inventory_map` keyed on `(user_id, card_id, finish, drawer=None, slot=None, is_pending=True)`. The lookup only matches **other pending rows from the same import batch** — it does not look at the user's existing placed inventory (drawer rows, binder rows, deck rows). New `InventoryRow` records are always created for the imported cards as `is_pending=True`.

Then [app/inventory_service.py:814-837](../app/inventory_service.py#L814-L837) (`place_imported_rows`) takes those pending rows and sets their `storage_location_id` to the deck's `storage_location_id`. `is_pending=False`.

**Result:** if the user already owns 4 Lightning Bolts in a drawer AND imports a Moxfield deck containing 4 Lightning Bolts, they now own **8 Lightning Bolts** — 4 in the drawer, 4 in the deck. This is the duplication the user reported.

**Scenario 2: User manually adds a card to a deck via the deck detail page.**

[app/deck_service.py:1103-1185](../app/deck_service.py#L1103-L1185) (`pull_card_to_deck`) is invoked from `POST /decks/pull` (called from the Search Collection panel on the deck detail page). It:

1. Finds the source `InventoryRow` (the row the user owns in some collection location).
2. Decrements the source row's `quantity` by the requested amount; deletes the row if quantity reaches 0.
3. Finds-or-creates a destination `InventoryRow` at the deck's `storage_location_id` and increments its `quantity`.
4. Writes a `TransactionLog` event `pull_to_deck`.

**This correctly implements Model A as a quantity transfer.** No duplication.

**But there's a UX gap:** the only entry point to this function is "search MY collection for a card I already own." There is no way to add a card to a deck if I don't own a copy — except by importing it, which (per scenario 1) doesn't reconcile against existing inventory.

**Scenario 3: User removes a card from a deck.**

[app/deck_service.py:1188-1277](../app/deck_service.py#L1188-L1277) (`return_card_from_deck`) is the inverse:

1. Find the deck row.
2. Find-or-create a destination row in the user's collection as `is_pending=True, storage_location_id=None`.
3. Delete the deck row.
4. Write `TransactionLog` event `return_from_deck`.

The card moves from deck → pending. The user later confirms placement into a drawer/binder/etc. **Correct Model A.**

**Side effect worth noting:** the deck row's `tags` (e.g., manual "Ramp", "Removal" tags) are lost when the row is deleted. The pending row starts with `tags=NULL` and gets re-auto-tagged when the user next loads a deck containing that card. Minor data-loss bug, not architectural.

**Scenario 4: User imports a CSV that includes cards already in a deck.**

If the destination is "Auto-sort to drawers" or any non-deck location, [app/inventory_service.py:resort_collection](../app/inventory_service.py) places the new pending rows into drawers. It does not consult existing deck rows. **Result: duplicates again** — the deck row's quantity is untouched, the drawer row is new with `quantity` set to the imported amount.

If the destination IS that deck (e.g., user imports a CSV directly into "My Bello Deck"), the new rows go into the deck location alongside the existing deck rows — and `pull_card_to_deck`'s smart-merge logic does NOT apply because we're going through `place_imported_rows`, not `pull_card_to_deck`. The deck location now has TWO rows for the same `(card_id, finish)` pair: the original deck row and the newly-imported one. The deck-detail page iterates both and shows them as separate cards in the grid. **Result: deck appears to "have" 2 of the card when really it has 1 in two rows.** This is subtler than Scenario 1's duplication but it's the same root cause.

### 1.4 Which model does the current behavior resemble?

- **The schema and `pull_card_to_deck` / `return_card_from_deck` paths are Model A.**
- **The import-to-deck path violates Model A** by creating new rows instead of consuming from existing inventory.
- **The deck-detail "Search Collection" feature is Model A done correctly** — it transfers quantity.

So the current behavior is **Model A with a bug in the import flow.** The user's complaint is a symptom of that bug, not a sign that the model is wrong.

---

## 2. Evaluating the three models

### Scoring matrix

Each row is a use case or evaluation dimension. Each column is one model. Score is qualitative: ✅ handled, ⚠ awkward, ❌ broken.

| Dimension                                        | Model A (decks ref inventory)                                     | Model B (decks independent)                                                                    | Model C (hybrid + join)                                              |
| ------------------------------------------------ | ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| User's stated need: import without duplicating   | ✅ if import reconciles — current bug is fixable                  | ✅ by definition — import never touches inventory                                              | ✅ default behavior is "don't add to inventory"                      |
| Wishlist deck (cards user doesn't own)           | ⚠ requires phantom inventory rows                                 | ✅ natural                                                                                     | ✅ natural                                                           |
| Move a card from one deck to another             | ✅ change `storage_location_id`                                   | ⚠ depends on how cross-deck moves are defined                                                  | ⚠ need to decide if "move" means anything at the deck-list level     |
| Sell a card that's in a deck                     | ✅ decrement deck row → log sell event                            | ❌ deck still lists the card, inventory rows say sold — drift                                  | ⚠ user has to remember to remove the card from both places           |
| "This card is in 3 of your decks" on card detail | ✅ join `InventoryRow` filtered to `storage_location_type='deck'` | ⚠ join via `DeckCard` table (new table)                                                        | ✅ same join as A via the deck-list table                            |
| Total collection value reflects reality          | ✅ — every card with a quantity is "owned"                        | ❌ — deck cards are double-counted or invisible                                                | ⚠ depends on whether the join considers deck-only entries as owned   |
| Drawer sorter logic stays correct                | ✅ — already works                                                | ❌ — drawer sorter can't know what's "in a deck" without joining a separate table              | ✅ — join works                                                      |
| Cross-format games tracker (which deck won?)     | ✅ existing wiring                                                | ✅ unchanged (deck table still exists)                                                         | ✅ unchanged                                                         |
| Schema simplicity                                | ✅ — one table for cards                                          | ⚠ — add `DeckCard` table (or similar)                                                          | ⚠ — same, plus join logic in every read path                         |
| Migration cost from current state                | ✅ — fix import only, no schema change                            | ❌ — drop `storage_location_id='deck'` rows, populate new `DeckCard` table, rewrite every read | ⚠ — add `DeckCard` alongside existing, dual writes during transition |
| Implementation complexity to land                | ✅ — 2-3 sessions for import reconciliation                       | ❌ — 6+ sessions, touches every deck read path, schema migration with data movement            | ⚠ — 4-5 sessions, plus permanent sync burden                         |
| Implementation complexity to maintain            | ✅ — same code paths as today                                     | ⚠ — separate code paths for deck content vs inventory                                          | ❌ — every "add card to deck" has to ask: also update inventory?     |

### 2.1 Model A — Decks reference inventory

**Already implemented at the schema and core-operation level.** The bug is in one specific code path (import). Fixing it is a localized change, not a rewrite.

The one case A handles awkwardly is **wishlist** — cards in a deck that the user doesn't physically own. The current data model has no concept of "I want this card." But this is solvable without changing the model: add `InventoryRow.is_wishlist` (or equivalent). Wishlist rows have `quantity > 0` for deck-completion purposes but are excluded from total-value, total-cards, and drawer-sorter calculations. The deck-detail UI shows them with a "wishlist" badge. The Scryfall search "$0.05" total for the deck wouldn't include them.

This is additive — no migration required for existing data. Future feature; not part of the user's immediate complaint.

### 2.2 Model B — Decks independent of inventory

**Conceptually clean but practically terrible for this app.** It would mean:

- A new `DeckCard` table: `(deck_id, card_id, finish, quantity)` with no relationship to `InventoryRow`.
- All current "this card is in deck X" logic rewritten to consult both tables.
- The drawer sorter has no way to know if a card is "in use" — the user could pull a card from a drawer into a deck, but the drawer still says they own it.
- Total collection value either double-counts (deck cards exist in `DeckCard` AND in `InventoryRow`) or doesn't reflect what's in decks.
- The migration would have to scan every existing deck row, create `DeckCard` entries, decide whether to leave the `InventoryRow` in place (double-count) or delete it (lose ownership data), and rewrite every read path.

The "import doesn't duplicate" property comes free in Model B because nothing connects the two systems — but that's not actually what the user wants. They want **the deck to recognize what they already own.** Model B can't do that without re-introducing a join, which is just Model C.

### 2.3 Model C — Hybrid with join

**Worst of both worlds.** Maintains two tables (DeckCard + InventoryRow) with an implicit relationship that the application has to enforce on every write. Every time the user edits a deck, the system has to ask: "do you also want to update inventory?" Every time the user sells a card, the system has to ask: "should I also remove it from the decks it's in?"

The "best of both worlds" framing in the question is misleading. The two worlds are actually **incompatible models of ownership**, and trying to maintain both means the user is constantly making the same decision over and over: which side of the divide is this card change on?

The one real advantage — wishlist cards — is achievable in Model A with a single `is_wishlist` flag. We don't need a whole separate ownership system to get it.

---

## 3. Recommendation: Refined Model A

**Keep the current architecture. Fix the import flow. Add `is_wishlist` as a follow-up feature, not now.**

### 3.1 Core principle

> A card in a deck is the same `InventoryRow` as a card in a drawer or binder. The user owns N physical copies of card X; those N copies are distributed across N storage locations (which may include decks). Quantity is conserved across moves.

This is what the existing schema, `pull_card_to_deck`, and `return_card_from_deck` already implement. We're not changing the model — we're making the import flow honor it.

### 3.2 What stays the same

- `InventoryRow` schema unchanged.
- `Deck`/`StorageLocation` relationship unchanged.
- `pull_card_to_deck`, `return_card_from_deck`, the deck-detail "Search Collection" panel, the Bulk Move panel — all unchanged.
- The drawer sorter is unchanged.
- The Pending Placement page is unchanged.
- `TransactionLog` continues to track moves.

### 3.3 What changes — the import-to-deck flow

#### Reconciliation step

When a user imports a deck-list (paste-list, CSV, or Moxfield export) and selects a deck as the destination, the import preview page gains a **reconciliation table** before commit:

For each card in the import:

1. Query the user's existing `InventoryRow`s that match `(card_id, finish)` and are NOT already in the destination deck location.
2. If matches exist, show a row in the reconciliation table:
   `Lightning Bolt — you own 4 in Drawer 2. Move 4 of them to this deck? [✓ Yes (default) / No, import new copies]`
3. If no matches exist, the card is treated as a new addition (current behavior, creating a new pending row).

The user sees the reconciliation list at preview time, alongside the existing "valid rows" preview. They can:

- Accept all defaults (move existing copies where available, import new only for cards they don't own)
- Override per-card (e.g., "I'm buying a second copy of this for the deck — import new")
- Cancel and rethink

On commit:

- Cards marked "move existing" call `pull_card_to_deck` internally (quantity transfer from source to deck — exact same code path as the deck-detail Search Collection flow).
- Cards marked "import new" go through the current `persist_import_rows` + `place_imported_rows` path.

#### Default behavior

The default for a row with existing inventory matches is **"move existing copies"** — because that's the answer the user wants 95% of the time, and the alternative (importing duplicates) is the silent footgun they reported.

For cards where the user owns FEWER than the deck requires (deck needs 4, user has 2), the default is "move 2 existing + import 2 new." The reconciliation row shows the math: `Lightning Bolt — deck needs 4, you own 2. Move 2 + import 2 new. [override]`.

#### What about cards already in another deck?

A subtle case: user imports Moxfield deck A and already has the cards in deck B. Should we propose moving them from B to A?

**No, default is to import new copies in this case.** If a card is in deck B, the user probably wants both decks intact. Moving a card OUT of deck B would silently break that deck. We can show the location in the reconciliation row (`Sol Ring — you own 1 in deck "Krenko Burn". Move it? [✗ No (default) / Yes, move from Krenko Burn]`), but the default is no — only cards in non-deck locations (drawers, binders, pending) are auto-proposed for moves.

#### CSV imports targeting a non-deck location

Unchanged. Importing a CSV "Auto-sort to drawers" or to a specific binder/box continues to behave as today — new rows are created, no reconciliation against existing inventory. This is intentional: a "collection import" is the user telling the system about cards they've acquired, not asking it to deduplicate against what they already had logged.

If a Helvault export is _partially_ overlapping with the existing collection (user re-exports their full collection on top of an old import), they'd see doubled counts. That's a known pattern with collection imports and isn't part of the user's complaint. A separate "merge/replace collection import" feature is out of scope for this design.

### 3.4 What changes — the deck-detail page

Minor UX improvement: when the user types a card name into the existing "Search Collection" panel and the search returns zero results because they don't own the card, the panel can offer a one-click "Search Scryfall and add new copies to this deck" — which goes through the existing manual-import-to-deck path with reconciliation skipped (since by definition the user doesn't own the card).

This is small and additive; it doesn't change anything else.

### 3.5 Wishlist (deferred — not in initial implementation)

The cleanest extension: add `InventoryRow.is_wishlist: Boolean default False`. Wishlist rows:

- Are excluded from `total_value`, `total_cards`, `Unique Cardnames`, drawer-sorter assignment, and the price-refresh loop.
- Appear in deck-detail with a "Wishlist" badge.
- Get a "I bought this" action that flips the flag to false and prompts the user to confirm placement.

A "Add wishlist card to deck" button on deck detail creates an `is_wishlist=True` row in the deck location. Removing from the deck either:

- Deletes the wishlist row (default — wishlist cards aren't kept around when not in a deck), or
- Returns to pending with `is_wishlist=True` (if the user wants to track unowned cards across decks)

This is a future feature. It uses the existing schema with one new column. **Not part of the import reconciliation work.** Mentioning it here so we know the architecture supports it when we want it.

---

## 4. Import flow under the recommended model — UI walk-through

Below describes what the user sees. Markup is illustrative, not final.

### 4.1 Step 1: Upload / paste / search (unchanged)

`/import` page with the existing CSV, paste-list, and manual options. No change.

### 4.2 Step 2: Preview (extended)

Current preview (`import_preview.html`) shows valid rows + invalid rows. After this change, when the user picks a **deck** as the destination from the dropdown, a new "Reconciliation" section appears between the valid-rows table and the Import button:

```
+-- Reconciliation --------------------------------------------------+
| 7 of 100 cards in this deck are already in your collection.        |
|                                                                    |
|  Card                  In deck   You own           Action          |
|  ──────────────────────────────────────────────────────────────    |
|  Lightning Bolt        4×        4× in Drawer 2   [Move 4] ▼     |
|  Sol Ring              1×        1× in Drawer 6   [Move 1] ▼     |
|  Mana Crypt            1×        1× in "Krenko"    [Import 1] ▼  |
|  Brainstorm            4×        2× in Drawer 1   [Move 2 + new 2] ▼|
|  Counterspell          4×        none              (Import 4)      |
|  ...                                                               |
|                                                                    |
|  [Accept defaults]   [Override individually]                       |
+--------------------------------------------------------------------+

[Import 100 cards into "Bello"]
```

Per-row action options (selectable per row):

- **Move N**: transfer N copies from a non-deck location to this deck. Default when user owns ≥ deck-required quantity in a non-deck location.
- **Move N + new M**: transfer N owned copies, import M new ones. Default when user owns FEWER than deck requires in non-deck locations.
- **Import N (new)**: ignore existing inventory, create new rows. User override.
- For cards already in OTHER decks: default is "Import new," with a non-default option "Move from <deck name>" hidden behind a per-row dropdown.

### 4.3 Step 3: Commit

Backend processes the reconciliation list:

- For "Move N" rows: call the equivalent of `pull_card_to_deck` per source row (split across multiple sources if the user owns the card in multiple drawers).
- For "Import N" rows: create new `InventoryRow` as today.
- For "Move + new" rows: do both.

The import-complete page (`import_result.html`) reports both counts:

> Imported 100 cards (95 unique). 18 cards moved from your collection, 82 new copies imported. [View deck] [Import more cards]

### 4.4 What collection-CSV import looks like (unchanged)

CSV imports targeting "Auto-sort to drawers" or a binder/box continue to behave as today. The reconciliation only kicks in when the destination is a deck.

---

## 5. Migration

### 5.1 Data migration: none required

The schema already supports the recommended model. The import-flow fix is purely application-code: a new reconciliation preview step and a new commit handler that branches per-row. Existing decks, existing collection rows, existing pending rows all stay where they are.

### 5.2 Data fix: optional one-shot dedup

Some users may already have the "duplicate import" condition — they imported a Moxfield deck on top of an existing collection, and now have both a "drawer" copy and a "deck" copy of the same cards. We could:

- **Option 1: leave them alone.** Users who notice can manually return cards from the deck (existing flow), which decrements their drawer-side row when it lands back as pending. No code change.
- **Option 2: write a one-shot reconciliation script** that scans for `(user_id, card_id, finish)` triples appearing in both a deck location AND a non-deck location, and prompts the user (via a UI panel) to choose which is "real." This is more work and adds a one-time UI surface; defer unless users actually ask for it.

Recommendation: **Option 1.** The bug stops accumulating new duplicates as of the deploy; existing duplicates are rare and self-correcting via normal use.

### 5.3 Transition period

None. The change is a single deploy. Before the deploy, deck imports duplicate. After, they reconcile. No flag, no period of coexistence.

### 5.4 Rollback

If reconciliation has bugs that cause data loss (e.g., a `pull_card_to_deck` call goes wrong and drops quantity), users would notice immediately on the import-result screen ("Imported 95 cards but I only see 50 in the deck"). The TransactionLog records every move with `quantity_delta`, so a manual recovery is possible via the existing Undo Last Batch path: the entire import batch can be undone, which restores all source row quantities and removes the deck rows.

The fix to roll back the code is a single revert. No migration to unwind because no schema changed.

---

## 6. Implementation scope estimate

**Refined Model A — import reconciliation:** 2-3 sessions.

| Session      | Work                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1            | Backend: extend the preview-parse step to query existing inventory and decorate each parsed row with a `match` list (existing rows that could be sources). New service function `find_inventory_matches_for_deck_import(session, user_id, deck_id, parsed_rows)`. Returns per-row source candidates.                                                                                                                      |
| 2            | UI: extend `import_preview.html` with the reconciliation table when destination is a deck. Add per-row action dropdowns (default behavior + override). Add new form fields to encode the user's choices. Extend `/import/commit` and `/import/manual/commit` to read the reconciliation choices and dispatch to `pull_card_to_deck` vs `persist_import_rows`+`place_imported_rows` per row.                               |
| 3 (probable) | Edge cases: rows where user owns the card in MULTIPLE non-deck locations (split-source moves), rows where the source-row quantity changes between preview and commit (concurrent edits), rows where the destination deck is brand-new (no existing deck rows yet but the user has the card in a drawer). Plus the small UX addition on the deck-detail page (offer "add new copies" when collection search returns zero). |

**Wishlist feature (deferred):** 1 session.

| Session        | Work                                                                                                                                                                                                                                               |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 (when ready) | Add `InventoryRow.is_wishlist` column + migration. Update queries that compute total_value, total_cards, drawer-sorter, price-refresh to exclude wishlist rows. Add wishlist badge in deck-detail UI. Add "Add wishlist card to deck" entry point. |

**Total to fully address the user's complaint:** 2-3 sessions. **Total to also have wishlist support:** +1 session.

---

## 7. Open questions for review

1. **Should the reconciliation default move from other decks?** Recommendation in §3.3: no, default to "import new" if the only existing copy is in another deck. The user should explicitly opt into cannibalizing one deck for another. Confirm.

2. **What happens to the deck row's `tags` when a card is moved between decks?** Per scenario 3, current behavior loses tags on return-to-pending. Should the reconciliation flow preserve `tags` when moving a card from drawer-A to deck-B? Probably yes — copy tags from source row. Worth confirming during implementation.

3. **Should the reconciliation step also run when importing into a non-deck location (e.g., importing a binder export into binder "Vintage Collection")?** Probably no — collection-import flows are about adding to inventory, not reconciling against it. But if a user has a "fix my doubled collection" need later, that's a different feature. Confirm scope is deck-only.

4. **When the user owns the card in multiple non-deck locations** (e.g., 2 copies in Drawer 2 + 2 in Binder "Legacy"), where should the default move pull from? Recommendation: prefer drawer-sorter drawers first (since drawer-sorter cards are "loose" inventory by design), then binders, then other locations. Or just take the oldest row first. Not load-bearing on architecture; decide during implementation.

5. **Wishlist scope when implemented:** should wishlist cards be marked at import time too? (e.g., "I'm importing a deck I don't fully own; mark the unowned cards as wishlist.") Or is wishlist only for cards manually added via a wishlist-specific UI? Worth deciding before that session.

These are all knobs, not architectural questions. The model itself is settled.
