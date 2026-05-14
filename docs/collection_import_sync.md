# Full-Collection Import: Sync vs Acquisition

**Status:** design / pre-implementation
**Trigger:** user reported (during v3.16.14 review) that a re-imported Helvault collection will create duplicate inventory rows for any cards already in app — including cards the user pulled into decks via the v3.16.13/14 import-to-deck flow.
**Builds on:** [docs/deck_collection_model.md](deck_collection_model.md) — Refined Model A, sessions 1–2 shipped.

---

## 0. TL;DR

The current import flow treats every CSV upload as "the user telling the system about cards they've acquired." That assumption was fine when users imported once and managed inventory by hand; it breaks when users re-export their full collection from Helvault/Moxfield on top of an existing app state — every overlapping card duplicates.

The [deck_collection_model.md §3.3](deck_collection_model.md) design explicitly carved this out as future work. The v3.16.13/14 work made the bug worse: users can now legitimately put cards directly into decks without ever filing them in a drawer first, so a routine "re-export Helvault" on top of any deck-built inventory creates non-deck duplicates of every deck card.

**Recommendation: per-row reconciliation on every import, regardless of destination.** Same shape as the deck reconciliation we just shipped, generalized: each parsed row is matched against the user's total inventory across all locations, and the recommended action is "skip — you already own N+ of this" / "import partial — you own M, this brings you to N" / "import all — you don't have any." The user expands a `<details>` to override per row if their intent is "these are new acquisitions on top of existing" rather than "sync."

**Net effect of the recommendation:** the import preview always offers the same trustworthy default — never duplicate. The user can always opt into "import as new acquisitions" via the override.

Implementation scope: **~2 sessions** (one for the function generalization + UI extension, one for edge cases + smoke coverage). Schema unchanged. Existing reconciliation UI infrastructure reused.

---

## 1. Current behavior

### 1.1 What happens today

The CSV/paste-list/manual import flow ends in `persist_import_rows` + `place_imported_rows`. For deck destinations, the v3.16.13/14 reconciliation pipeline now sits between them. For non-deck destinations, none of that fires:

**`persist_import_rows`** at [app/import_service.py:391-438](../app/import_service.py#L391-L438):

- Looks for "merge candidates" only among **other pending rows from the same batch**. The inventory_map key is `(user_id, card_id, finish, drawer=None, slot=None, is_pending=True)` — so it only matches against rows that are still in pending limbo (`is_pending=True`, no drawer/slot assigned).
- All previously-placed rows (in drawers, binders, boxes, decks, anything with `is_pending=False`) are invisible to this query.
- Every imported row therefore creates a new `InventoryRow` (or merges with the running pending set inside the current batch).

**`place_imported_rows`** at [app/inventory_service.py:814-837](../app/inventory_service.py#L814-L837):

- Takes the row IDs persist_import_rows returned and sets `storage_location_id` + `is_pending=False`.
- No merging. Even if the destination already has a row for the same card+finish, the new row is placed alongside as a separate `InventoryRow`. (This is the bug v3.16.14 fixed for deck destinations specifically; the non-deck case still has it.)

**`resort_collection`** (the drawer-sorter, [app/inventory_service.py:1156-1262](../app/inventory_service.py#L1156-L1262)):

- Excludes deck-located rows correctly (since v3.11.3).
- Treats freshly-imported rows the same as any other inventory — sorts them into drawers based on `assign_drawer()`. If a card already has a drawer slot, the new row gets a fresh slot assignment alongside the existing one.

### 1.2 The duplicate scenarios

Five common user flows that all produce duplicate inventory under current behavior:

**A. Pure re-import** — user re-exports the same Helvault on top of an unchanged app state. Every imported row creates a new `InventoryRow` because none of the existing rows are pending. After commit: every card the user owns is now logged twice.

**B. Acquisition re-import** — user bought 5 cards since last export, re-exports the full collection. Same outcome as A but plus 5 more new rows for the genuinely-new cards. The duplication is invisible inside the "new" noise.

**C. Deck-build re-import** — user pulled cards into decks (via the v3.16.13/14 import-to-deck path OR the deck-detail "Search Collection" panel). Re-imports the full collection. Helvault doesn't know about decks; it still lists those cards as owned. The import creates a fresh non-deck row for each one. Result: every deck card now exists as both a deck row AND a duplicate drawer/binder row.

**D. Partial-export re-import** — user re-exports a SUBSET (e.g., only Standard-legal cards). Imports onto the existing collection. The export's "0 of card X" doesn't mean the user lost card X; the export just doesn't include it. No silent removal — but no dedupe either. Net: every overlapping card duplicates.

**E. Cross-app sync** — user starts manually tracking via Mana Archive's "add a card" flow, then later imports a Helvault export representing the same physical collection. The manually-added rows don't get merged with the import; they sit alongside the new rows. Same duplication shape.

All five cases share the same root cause: `persist_import_rows` doesn't consult placed inventory.

### 1.3 What v3.16.13/14 already does

For **deck destinations only**, the reconciliation pipeline:

- Matches each parsed row against the user's total inventory via `find_inventory_matches_for_deck_import`
- Recommends `move_existing` / `move_existing_plus_new` / `import_new` based on what the user owns in non-deck locations
- Surfaces deck-located copies informationally ("Already in this deck: N", "In another deck: N in X")
- Auto-merges new imports into existing target-deck rows so a deck never gets two rows for the same printing

**For non-deck destinations, none of this runs.** The reconciliation panel doesn't appear. `persist_import_rows` + `place_imported_rows` execute byte-identical to pre-v3.16.13 behavior. Which is exactly the bug.

### 1.4 Why this wasn't fixed earlier

[deck_collection_model.md §3.3](deck_collection_model.md) explicitly scoped out the non-deck case:

> CSV imports targeting a non-deck location: Unchanged. Importing a CSV "Auto-sort to drawers" or to a specific binder/box continues to behave as today — new rows are created, no reconciliation against existing inventory. This is intentional: a "collection import" is the user telling the system about cards they've acquired, not asking it to deduplicate against what they already had logged. A separate "merge/replace collection import" feature is out of scope for this design.

The framing — "import = acquisition, not sync" — was reasonable at the time. But the v3.16.13/14 work changed the calculus: cards can now legitimately exist in decks without ever passing through a drawer/binder, which means **re-importing the full Helvault state now silently creates non-deck duplicates of any deck cards.** The user concretely pointed this out during v3.16.14 review.

The new framing: the import flow can't tell whether a given upload is an acquisition or a sync. It needs to default to safe (dedupe) and let the user opt into unsafe (import as new) per-row.

---

## 2. Use case matrix

Each row is a realistic user pattern. Columns score the current behavior vs the proposed model.

| Use case                                                    | Today                                            | Proposed                                                                                                                      |
| ----------------------------------------------------------- | ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| First-time import (zero existing inventory)                 | ✅ works                                         | ✅ works (nothing to dedupe against)                                                                                          |
| Pure re-import on unchanged state                           | ❌ every row duplicates                          | ✅ every row recognized as "you already own this, skipping"                                                                   |
| Re-import + acquisitions (some new cards)                   | ⚠ new cards work, but duplicates everything else | ✅ new cards imported, existing recognized as owned                                                                           |
| Re-import after deck-building (the user's complaint)        | ❌ deck cards duplicate in non-deck locations    | ✅ deck rows count toward owned, no duplicates                                                                                |
| Partial export re-import                                    | ❌ overlapping cards duplicate                   | ✅ overlap recognized; non-overlap doesn't affect existing                                                                    |
| Cross-app sync (Helvault on top of manual entries)          | ❌ duplicates                                    | ✅ existing rows count toward owned                                                                                           |
| Manual single-card import ("I bought a Sol Ring")           | ✅ works (user expects to add a copy)            | ⚠ default would recommend "skip if owned" — user has to override "import as new"; minor friction; acceptable trade for safety |
| Paste-list with truly-new acquisitions ("opened a booster") | ✅ works                                         | ⚠ same as above — defaults recommend skip; user overrides on a small list, fine                                               |

The proposed model is strictly better for re-import cases. The new-acquisition cases get a small UX cost (one override click per row) in exchange for never-silent-duplication. The user explicitly accepted this trade in the v3.16.14 review conversation.

---

## 3. The product question

What does importing a card list MEAN when the user already has matching inventory?

Two interpretations:

**Acquisition semantics** (current): "Here are cards I just got. Add them to my inventory." Each row is additive; existing inventory is irrelevant. The import flow's job is to extend, not synchronize.

**Sync semantics** (proposed): "Here is my current collection state. Make my inventory reflect this." Each row is a state assertion; existing inventory determines what changes are needed. The import flow's job is to converge inventory to the import's view.

The product reality: **users mean both, depending on the source and intent**, but they can't reliably signal which is which at upload time. A Helvault export of the full collection is almost always sync. A paste-list of cards just opened in a booster is almost always acquisition. Manual single-card entry is almost always acquisition. CSV mid-cases (some new cards, some that overlap) are genuinely ambiguous.

The system can't tell from the data shape alone. It needs to ask the user, AND it needs to have a safe default.

The safe default is **sync** (skip-if-owned). The unsafe default is acquisition (silent duplication). When in doubt, fewer rows is recoverable; more rows is data loss in a real way (the user thinks they own twice as many cards as they do; later flows make wrong decisions).

---

## 4. Recommendation: per-row reconciliation on every import

Generalize the deck-reconciliation flow shipped in v3.16.13/14. Every import preview, regardless of destination, computes per-row matches against the user's total inventory and presents a reconciliation panel. The action select per row has three options:

- **`skip_already_owned`** — user has at least the imported qty in their inventory. No-op.
- **`import_delta`** — user has fewer than the imported qty. Import the difference.
- **`import_new`** — import the full qty as new copies regardless. The "I really did acquire this" override.

The default per row is driven by `total_user_owned` vs `quantity_needed`:

| User owns total | Imported qty | Default action       | Move | New   |
| --------------- | ------------ | -------------------- | ---- | ----- |
| 0               | N            | `import_new`         | 0    | N     |
| K < N           | N            | `import_delta`       | 0    | N - K |
| K ≥ N           | N            | `skip_already_owned` | 0    | 0     |

"User owns total" sums across **all locations**:

- Non-deck placed rows (drawers, binders, boxes, "other")
- Deck rows (every deck the user owns)
- Pending rows (the previous import is still un-filed)

A card sitting in a deck is owned. A card waiting in pending is owned. A card the user filed in a binder is owned. The inventory model says they're all `InventoryRow` records with quantity; the sync logic respects that without any special-case carve-out.

### 4.1 Per-printing match (per `card_id`, not per name)

Matching is strict per `(user_id, card_id, finish)`. Same Teysa Karlov from a different set is a different `card_id` and is NOT counted toward the import. Reasoning:

- The user's inventory tracks specific printings. Treating different printings as fungible risks the case where a user genuinely owns two different printings and the system says "you already own this, skip."
- Helvault/Moxfield exports include set+collector_number; the parser resolves to a specific `card_id`. Matching on `card_id` is the truthful comparison.
- Per-name fuzzy matching exists as a "future polish" (see open questions §8.2) — not in scope here.

### 4.2 Destination still matters (delta goes somewhere)

The reconciliation determines _whether_ to import new copies and _how many_. The destination dropdown still determines _where_ those new copies land (auto-sort drawers, a specific binder, a deck). The two questions are independent:

- "Should I import this card?" → reconciliation result
- "Where should it go?" → destination dropdown

For deck destinations, the merge-into-existing-deck-row behavior from v3.16.14 still applies — new copies that land in a deck location merge with any existing matching deck row.

### 4.3 Never auto-remove inventory

The reconciliation only ever ADDS or SKIPS. If the import says "0 of card X" and the user has 4 in their inventory, the system does NOT remove 4 from inventory. Reasoning:

- Many exports are partial (only certain sets, only recently-modified cards). The absence of a card in an export usually doesn't mean the user lost it.
- Inventory removal is irreversible without a real undo path. Sync auto-removal is dangerous.
- If a user wants to "true up" their inventory to match Helvault exactly, that's a separate flow ("reset inventory to match this CSV") — out of scope here. Probably v4 work.

So: the import is a one-way add. Existing inventory is read-only from the import's perspective. The reconciliation panel may show "your inventory has 12 cards this import doesn't mention; review separately if you sold some" as an informational tail at the bottom of the panel, but it doesn't act on that.

### 4.4 Match across decks too

A card in a deck counts toward "owned" for sync purposes. If the user has 1 Sol Ring in deck A and Helvault says they own 1 Sol Ring, the import recognizes the deck row as the same physical copy. Default action: `skip_already_owned`. The user can override to `import_new` if they're saying "I bought a second Sol Ring and it should live in my collection separately from the one in deck A."

### 4.5 Defaults vs overrides

Default per-row action:

- 0 owned, importing N → `import_new` (no friction; user expected to add)
- K < N owned, importing N → `import_delta` (skip the K already there, add N-K)
- K ≥ N owned, importing N → `skip_already_owned` (no-op)

Summary above the per-row table:

- "Importing 247 cards: 38 new, 12 partial-import (your collection is short on these), 197 already owned (will skip)."

Single button — Import — applies the defaults. The `<details>` expansion lets the user flip any row to `import_new` if they really did acquire a second copy. Same UX shape as v3.16.13.

---

## 5. UI walk-through

Reuses the import preview + reconciliation pipeline already shipped in v3.16.13/14. Net new template/route work is small.

### 5.1 The reconciliation panel — generalized

For ANY destination (deck or non-deck), after the user picks a destination the HTMX endpoint computes reconciliation and swaps the panel into the preview. The empty state (no destination yet) stays empty.

**For deck destinations** — unchanged from v3.16.14:

- Per-row matches by `(card_id, finish)` against non-deck inventory (movable copies)
- Target-deck and other-deck copies surfaced informationally
- Defaults: `move_existing` / `move_existing_plus_new` / `import_new`

**For non-deck destinations** — new behavior:

- Per-row matches across ALL the user's inventory (decks + non-deck + pending)
- One scalar per row: `total_user_owned`
- Defaults: `skip_already_owned` / `import_delta` / `import_new`

The function signature unifies these. New function (or extended existing):

```python
def reconcile_import_against_inventory(
    session: Session,
    user_id: int,
    parsed_rows: list[dict],
    target_destination: TargetDescriptor,  # ("deck", deck_id) | ("location", loc_id) | ("auto_sort", None)
) -> list[dict]:
```

Returns per-parsed-row dicts with destination-aware recommendations. For deck destinations the output shape stays the same as today (`matches`, `target_deck_matches`, `other_deck_matches`, etc.). For non-deck destinations the output has different fields:

```python
{
    "line_number": int,
    "card_id": int | None,
    "scryfall_id": str,
    "finish": str,
    "quantity_needed": int,
    "total_user_owned": int,        # NEW: sum across all locations
    "owned_breakdown": [             # NEW: where the existing copies live
        {"location_name": str, "location_type": str, "quantity": int},
        ...
    ],
    "recommended_action": str,       # "skip_already_owned" | "import_delta" | "import_new"
    "recommended_skip_qty": int,     # qty to NOT import (because already owned)
    "recommended_new_qty": int,      # qty to import as new
}
```

Reuses the same `_import_reconciliation.html` partial; the template branches on `destination_kind` (deck vs other) to render the right summary + per-row options.

### 5.2 Per-row UI when destination is non-deck

Mockup:

```
+-- Reconciliation -----------------------------------------------+
| Importing 247 cards into "Auto-sort to drawers":                |
|   38 will be imported new                                       |
|   12 partial-import (your collection is short on these)         |
|   197 already owned — will be skipped                           |
|                                                                 |
| ▸ Review individually (override per card)                       |
+-----------------------------------------------------------------+

[Expanded:]

Card             Import  You own           Action
─────────────────────────────────────────────────────────────────
Sol Ring         1×      1 (1 in Bello)   [Skip — already owned (default) ▼]
Lightning Bolt   4×      2 (Drawer 2)     [Import 2 more (default) ▼]
Counterspell     4×      0                (Import 4 new)
Teysa Karlov     1×      1 (in Teysa deck) [Skip — already owned (default) ▼]
...
```

Action select per row:

- `skip_already_owned` (default when `total_user_owned >= quantity_needed`) — no-op
- `import_delta` (default when `0 < total_user_owned < quantity_needed`) — import `quantity_needed - total_user_owned`
- `import_new` (override; or default when `total_user_owned == 0`) — import full `quantity_needed`

Hidden fields per row encode the actual skip/new split:

- `reconcile_action[]`
- `reconcile_skip_qty[]`
- `reconcile_new_qty[]`

The same naming convention as deck reconciliation. The commit handler reads them and dispatches.

### 5.3 Commit handler — generalize the dispatch

Extend `_commit_deck_import_with_reconciliation` (or split into `_commit_import_with_reconciliation`) to handle both action types. For non-deck destinations:

- `skip_already_owned` → no `InventoryRow` created. Optionally write a `TransactionLog` of event_type `import_skipped` for audit trail.
- `import_delta` → create a new row for `recommended_new_qty` copies via `persist_import_rows`, place via `place_imported_rows`.
- `import_new` → same as today: full qty.

Non-deck destinations still go through `persist_import_rows` + `place_imported_rows` for the rows that DO get imported. The drawer-sorter still fires for drawer-sorter users.

### 5.4 Result page — generalize the counts

The result page currently shows: cards imported, cards moved (deck only), cards merged (deck only), failed rows. Add:

- **Cards skipped** — qty `skip_already_owned` rows accounted for. New stat card when `> 0`.

Message:

> Imported 38 cards (12 unique), 197 skipped (already in your collection), 12 added to make up the difference.

### 5.5 Single-card manual import — different default

The manual single-card flow (`/import/manual/preview`) reuses the same reconciliation panel but **flips the default action**.

Reasoning: CSV/paste-list imports are usually sync ("here's my collection state"), where skip-by-default is correct. Single-card manual entry is usually acquisition ("I just bought a Sol Ring at the LGS"), where skip-by-default would force an override click on every single addition — meaningful friction on a high-frequency flow.

So for the manual flow specifically:

- The reconciliation panel still appears
- The "You own N of this" notice still shows ("You already own 1 Sol Ring in Drawer 1")
- The action default flips to **`import_new`** (acquisition semantics)
- The skip option is still available in the dropdown for the rare case where the user did mean to re-add an existing card

The function output is identical between CSV and manual flows; only the template's pre-selected default differs. Implementation: pass a `manual_mode` boolean (or equivalent) into the partial render context; the template branches the `selected` attribute on the action `<select>` accordingly.

This is a behavior change vs today's manual flow (which currently silently duplicates without any reconciliation panel). After this change, the panel always appears, and the default matches the typical user intent for each flow.

### 5.6 What about the destination dropdown defaulting to "auto-sort" for drawer-sorter users?

The reconciliation panel fires on every destination change, including the initial render. If the default destination is "auto-sort to drawers" and the user has overlapping inventory, the panel populates immediately. The user sees the reconciliation BEFORE clicking Import.

If they pick a different destination, the panel re-renders with the same recommendations (the destination doesn't change WHICH rows are owned, only where new copies land).

---

## 6. Migration / rollout

### 6.1 Schema migration: none

The proposal uses the existing `InventoryRow` schema. No new columns, no migrations. Pure code change.

### 6.2 Data migration: none

Existing inventory state is unchanged. The new behavior only affects future imports. Past duplications (from re-imports under the old behavior) stay where they are — the proposal doesn't retroactively merge them.

**Explicit non-goal: reconciliation prevents NEW duplicates, it doesn't retroactively clean old ones.** Users who already accumulated duplicate inventory rows from past re-imports (under the pre-v3.16.15 behavior) will still see those duplicates after this lands. The reconciliation system uses the duplicated counts as part of "what the user owns" (since both rows are real `InventoryRow` records), so subsequent re-imports won't re-duplicate them — but it also won't shrink them.

Users who want to clean up legacy duplicates have two paths:

- Manually merge duplicate rows via the existing inventory UI (delete row, add to existing)
- Wait for the optional cleanup script (§6.3) if/when it ships

Communicate this clearly in the release notes for v3.16.15: "Reconciliation prevents duplicates going forward. If you have duplicate rows from past imports, they need to be cleaned up manually for now."

### 6.3 Optional cleanup script

For users who already accumulated duplicates from past re-imports, write a one-shot script:

```
scripts/dedupe_user_inventory.py --user_id=N [--dry-run]
```

Scans for `(user_id, card_id, finish)` triples appearing in multiple non-deck rows, merges them into the oldest row, deletes the duplicates. Logs everything as `TransactionLog` events of type `dedupe_merge`. Dry-run mode prints what it would do without committing.

This is out of scope for the initial implementation. Defer until a user actually asks for it.

### 6.4 Transition

Single deploy. Before: re-imports duplicate. After: re-imports recognize existing inventory and skip/delta.

No flag, no rollout period. The change is strictly safer than the current behavior (worst case: a user expecting acquisition semantics gets sync semantics, overrides per-row, takes 30 extra seconds).

### 6.5 Rollback

Revert the commit. The reconciliation function generalization stays in place (it's additive), but the import flow stops calling it for non-deck destinations. Existing data is unaffected.

---

## 7. Implementation scope

### 7.1 Estimate: 2 sessions

**Session A — generalize the reconciliation function + extend partial template.**

- Extend `find_inventory_matches_for_deck_import` (or split it) to support non-deck destinations. New signature accepting a `target_destination` descriptor. Returns the appropriate output shape per destination type.
- Update the partial template to branch on destination kind: keep the deck path verbatim, add the non-deck path with `total_user_owned` + skip/delta/new actions.
- New scenarios in the smoke script: L (user owns 4 in drawer, imports 4 to "auto-sort" → skip_already_owned), M (user owns 2, imports 4 → import_delta), N (user owns 0 in non-deck but 1 in a deck, imports 1 → skip_already_owned because deck rows count), O (single-card manual import where user already owns it).
- Verify all 30 existing scenarios still pass.

**Session B — extend commit handler + result page + edge cases.**

- Generalize the commit dispatcher to handle skip / delta / new actions per row.
- Update result template + counts (`skipped_count` new field).
- Edge cases: imported qty is 0 (skip silently), user has more pending than the import asks for (still skip — they're owned), cross-finish (foil vs normal handled as separate keys per existing schema).
- Manual import flow gets the same treatment.

### 7.2 Things deliberately not in scope

- Per-card-name fuzzy match (open question §8.2).
- Auto-remove inventory when import has fewer copies than app (§4.3 — never).
- "Reset my inventory to match this export" sync feature (§4.3 — v4).
- Retroactive dedupe of existing duplicate rows (§6.3 — optional separate script).
- Smart detection of "this is an acquisition list vs a sync list" (§3 — can't be inferred; user signals via per-row override).

---

## 8. Open questions

### 8.1 Where do delta-import copies land? (DESIGN-APPROVED FOR V1; POLISH TARGET)

When the user imports 4 Lightning Bolts and already owns 2 in Drawer 1, the recommended action is `import_delta` with `recommended_new_qty=2`. Those 2 new copies go to the destination the user picked (auto-sort, a specific binder, etc.) — they do NOT auto-merge with the existing Drawer 1 row.

This is consistent with deck imports (which create separate rows by location), but slightly weird for the user who might expect "all my Lightning Bolts in one row."

Two options:

- **Default (approved for V1): place new copies fresh at the destination.** Matches deck behavior, matches what `place_imported_rows` does today. Drawer-sorter users will see resort sweep them together later anyway.
- **Alternative: auto-merge into the existing non-deck row at the destination** (mirror the v3.16.14 deck auto-merge for non-deck destinations too). Cleaner data — never two rows for the same card+finish in the same non-deck location. But changes the semantics of `place_imported_rows` and could affect other call sites.

**V1 decision: default-place.** Drawer-sorter users won't notice — the next `resort_collection` pass consolidates duplicate rows in a drawer. **Manual-placement users (binders/boxes) WILL see permanent scattered duplicates** until the polish lands; if a user reports this, prioritize implementing the auto-merge path as a v3.16.X follow-up. Track this as a known polish target rather than a deferred-indefinitely future feature.

### 8.2 Per-name fallback for the per-printing match

Strict per-printing (current §4.1 recommendation) misses cases where the user has the same card from a different set. Common scenarios:

- User has Sol Ring (Commander 2014). Imports Helvault saying "Sol Ring (CMM)." Different `card_id`. Recommendation says `import_new` — duplicates a card the user functionally already has.
- User typed "1 Sol Ring" into the paste list once (parser picks a printing fuzzy-resolution); later imports a Helvault with the right printing. Same problem.

The fix: detect same-name-different-printing matches and surface them as informational ("You also own 1 Sol Ring in [printing] (Drawer 2). Still import this printing? [Yes (default) / No, treat as same card]"). Per-name fallback adds complexity to the reconciliation function and template. Defer to a v3.16.X polish.

### 8.3 What about `finish` mismatches?

Same card, different finish: Helvault has 1 foil Sol Ring, user has 1 non-foil Sol Ring. Different `(card_id, finish)` keys. Currently the reconciliation matches strictly per-finish, so this is treated as a new acquisition. Correct — these ARE physically different cards.

But: a "lazy" Helvault export might omit the finish column. The parser defaults to `normal`. If the user actually has the foil version, the import duplicates as `normal` and doesn't recognize the foil. Edge case; let users override.

### 8.4 Pending rows and re-import

If the user has 3 pending rows from a previous Helvault import they haven't placed yet, and they re-import the same Helvault, what happens?

Under the proposal: pending rows count toward `total_user_owned`. The reconciliation says "you already own these — skip." The pending rows stay pending; nothing new is created. Correct.

Under current behavior: the existing `persist_import_rows` merge-with-pending logic kicks in, doubling each pending row's quantity. That's a different bug, but it's actually compatible with the new sync semantics: the doubled qty doesn't ship anywhere new, just bloats pending. The new behavior cleanly supersedes it.

Worth noting: the proposed reconciliation should NOT trigger the existing `persist_import_rows` pending-merge if the action is `skip_already_owned`. The simplest implementation: route `skip_already_owned` rows out of `persist_import_rows` entirely. Only `import_new` and `import_delta` rows go through `persist_import_rows`.

### 8.5 What does the user see when there's nothing to do?

If the user imports a Helvault that's 100% overlap with their existing inventory (pure re-import), every row says `skip_already_owned`. The reconciliation panel summary: "247 cards already owned — nothing will be imported. [Review individually]." The Import button still works — clicking it produces a no-op import (zero rows added, optional `import_skipped` audit log).

Should we offer a one-click "Skip all" or just rely on the default? Current proposal: clicking Import with all-defaults IS skip all. Single button, predictable.

### 8.6 Drawer-sorter behavior with reconciliation

When `skip_already_owned` rows don't create new `InventoryRow` records, `resort_collection` has nothing new to sort. The drawer state is unchanged. Correct.

When `import_delta` creates fewer new rows than the import would have today, drawer-sorter still runs over those new rows. Less work for the sorter. Fine.

### 8.7 Should "destination" still be required for sync imports?

If the user is doing a pure re-import (every row will skip), the destination dropdown's value is _initially_ irrelevant — no new rows will be created, so no placement happens.

**But the destination is still required.** Reason: the user can flip any row from `skip_already_owned` to `import_new` via the per-row override. That changes the row from a no-op to a real import that needs a destination. Making the destination conditional on "any row is import_new" creates an awkward mid-flow prompt: "wait, where should this go?" right when the user is reviewing rows.

Keeping destination required upfront means the form always knows where overridden imports would land. The user picks a destination first, sees reconciliation (which may show zero new rows), then decides — no surprise prompts.

For all-skip imports the destination value is recorded but unused. No harm, no extra UX cost.

---

## 9. Decision points for review

Before implementation, confirm:

1. **Per-printing match by default, not per-name** (§4.1) — strict comparison; per-name as future polish (§8.2). OK?
2. **Match counts deck rows toward owned** (§4.4) — a card in a deck is owned; re-import won't duplicate it into a drawer. OK?
3. **Never auto-remove inventory** (§4.3) — import is one-way add/skip; reset-to-match-export is v4 territory. OK?
4. **Delta-import places fresh, doesn't auto-merge with existing non-deck rows** (§8.1) — consistent with current `place_imported_rows`; revisit if drawer-sorter users see scattered duplicates. OK?
5. **Manual single-card import gets the same reconciliation panel** (§5.5) — small UX cost on the most common "I bought one card" path; override via expansion. OK?
6. **Two-session implementation** (§7) — Session A = function + UI + smoke, Session B = commit dispatch + edge cases. OK?

The rest (per-name fuzzy match, retroactive dedupe script, reset-to-match flow) are deferred follow-ups that don't block this work.
