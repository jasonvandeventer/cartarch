# Deckbooks → Cartarch integration boundary

How this local prototype could later become a Cartarch feature, and what was
built to keep that path open.

## What already lines up

- **Images.** The prototype uses the exact mirror URL contract the app uses
  (`mirror_image_url`) plus the `scryfall_cards` metadata cache. Integration =
  call the app's `mirror_image_url` / `img_fallback` macro directly instead of
  the local `image_resolver` copy. Zero data migration for images.
- **Identity by UUID.** Every printing reference is a Scryfall UUID + finish —
  the same `(scryfall_id, finish)` shape the app keys inventory rows and trades
  on. No name-based identity to untangle.
- **Roles.** `models.ROLES` mirrors Cartarch's role vocabulary shape; the deck
  data carried none, so init derives a starting role (editable). A real
  integration would read the deck's own `role` tags where present.

## Eventual schema (when it graduates to the DB)

The JSON files map cleanly onto tables hanging off the existing `Deck`:

```
Deck
 └── Deckbook            (1:1 with a deck; edition + identity)
      ├── DeckbookCard        ← decisions.json rows (deck_card_id → InventoryRow / card)
      │    ├── PrintingDecision   (status, selected/museum printing, verdict, reasoning)
      │    └── AcquisitionRecord  (owned/installed/source/price/condition)
      └── DecisionRevision   ← revisions.json (append-only)
```

`deckbook.json` → `Deckbook` + `DeckbookEdition`; `decisions.json` → one
`DeckbookCard` (+ its `PrintingDecision`/`AcquisitionRecord`) per row;
`revisions.json` → `DecisionRevision`. Every FK is a Scryfall UUID or an
`InventoryRow` id, both already first-class in the app.

## What integration would add (deferred, not blocking)

Collection-aware ownership (read `InventoryRow` directly instead of a copied
`acquisition.target_owned`), a shareable public URL (same token pattern as deck
share links), PDF export, and multi-deckbook management. None of these change
the data model above — they consume it.

## Boundary rules honored by the prototype

1. No production DB writes — reads are read-only URI connections.
2. No second image cache — the mirror is reused, not reconstructed.
3. No server paths in persisted data — only Scryfall UUIDs, so the JSON is
   portable to any environment (or into the DB) unchanged.
4. Decoupled package — `deckbooks/` imports nothing from `app/`, so it cannot
   destabilize the live app; the app's test suite and CI are unaffected.
