# Storage Locations

**Last Updated:** 2026-05-28
**Status:** Current (v3.30.x)

## Purpose

`StorageLocation` is the canonical abstraction for where a card physically or logically lives. Every `InventoryRow` belongs to a location. Decks, drawers, binders, boxes, and bulk containers are all modeled as locations — there is no separate "drawer" or "deck" primitive. The drawer sorter, import pipeline, and collection view all operate through this abstraction.

## Core Models

### `StorageLocation`
The central entity. User-scoped: every location has a `user_id` FK and is invisible to other users.

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `user_id` | FK → users | Required, indexed |
| `name` | String(255) | Unique per user |
| `type` | String(64) | See types below |
| `parent_id` | FK → storage_locations | Self-referential; nullable |
| `mode` | String(16) | See modes below; default `managed` |
| `sort_order` | Integer | Display ordering |
| `note` | Text | Nullable; operator notes |
| `capacity` | Integer | Nullable; card count limit |

**Types:** `root`, `drawer`, `binder`, `box`, `deck`, `other`

**Modes** (v3.26.2):
- `managed` — sorter places cards into and moves cards out of this location
- `manual` — sorter leaves contents alone; no new placement
- `sink` — sorter drains out of but never places into (catch-all)
- `ignored` — invisible to the sorter entirely

### `InventoryRow`
One physical card holding. Every row must have a `storage_location_id` (nullable at the DB level for legacy data, but enforced at the service layer for new rows).

Key fields: `card_id`, `user_id`, `storage_location_id`, `finish`, `quantity`, `is_pending`, `drawer`, `slot`, `language`, `is_proxy`, `tags`.

### `Deck`
Metadata wrapper over a `StorageLocation` of `type="deck"`. The `Deck` row holds name, format, intent fields, and blurb. The cards themselves live in `InventoryRow` rows pointing at the linked `StorageLocation`. The FK is `Deck.storage_location_id`.

Every `type="deck"` `StorageLocation` must have a paired `Deck` row. The two are created atomically by `deck_service.create_deck` — `location_service.create_location` refuses `type="deck"` explicitly (v3.30.17 guard).

## Invariants

These are enforced at the service layer, not the DB layer (SQLite-until-v4 posture — no `CHECK` constraints).

1. **Location names are unique per user.** `create_location` raises `ValueError` if a location with the same name already exists for the user.

2. **`type="deck"` locations are created only through `deck_service.create_deck`.** `create_location` raises `ValueError("create_location refuses type='deck'...")` if called with `type="deck"` directly. This ensures the paired `Deck` row always exists.

3. **`root` locations cannot be edited or deleted.** Both `update_location` and `delete_location` raise `ValueError` if the target is `type="root"`.

4. **`deck` locations are edited and deleted through the Decks page.** `update_location` and `delete_location` reject `type="deck"` with an explicit redirect message.

5. **Locations with children cannot be deleted.** `delete_location` checks for child locations and raises `ValueError` if any exist.

6. **Non-empty locations require a redirect destination to delete.** If `destination_id` is None and the location has inventory rows, deletion is refused. If `destination_id` is provided, rows are moved one at a time before the location is removed.

7. **A location cannot be its own parent.** `update_location` raises `ValueError` if `parent_id == location_id`.

8. **Sorter targets and sources are derived from mode, not type.** `is_sortable_target` and `is_sortable_source` consult `SORTABLE_TARGET_MODES` and `SORTABLE_SOURCE_MODES` respectively. The SQL filter in `resort_collection` uses the same constants — the Python predicate and DB query are always aligned.

9. **`capacity_pct` is clamped at 100.** The over-capacity flag (`is_over_capacity`) fires at ≥95% fill, not 100%.

## Major Flows

### 1. Creating a location
The caller passes `name`, `type`, `mode`, `parent_id`, and optional `note`/`capacity` to `location_service.create_location`. The service validates the name (non-empty, unique per user), type (must be in `VALID_LOCATION_TYPES`, must not be `deck`), mode (must be in `VALID_LOCATION_MODES`), and parent (must exist and belong to the same user). A `StorageLocation` row is inserted and returned. For `type="deck"`, callers must use `deck_service.create_deck` instead, which creates both the `StorageLocation` and the `Deck` row atomically.

### 2. Moving cards between locations
`inventory_service.move_inventory_row_to_location` accepts a row ID, user ID, and destination location ID. The service validates that both the row and destination belong to the user, updates `InventoryRow.storage_location_id`, and writes `updated_at`. The `delete_location` redirect path calls this function per-row in a loop — each move commits individually, making the operation safely re-runnable if interrupted.

### 3. Drawer sorter placement
`inventory_service.resort_collection` queries `InventoryRow` for unplaced cards (those in `sortable source` mode locations) and assigns them to `managed`-mode locations according to the sorter's slot-assignment algorithm. The sorter's target and source filters use `SORTABLE_TARGET_MODES` and `SORTABLE_SOURCE_MODES` directly — adding a new mode to those constants updates both the Python predicates and the SQL filters simultaneously.

### 4. Location summary computation
`location_service.get_location_summary` returns a list of dicts with quantity, total value, capacity fill percentage, last-touched timestamp, and deletability flags for each location. The `last_touched_at` value comes from a single batched `GROUP BY` query across all the user's `InventoryRow` rows (keyed by `storage_location_id`), not per-location queries — added in v3.28.6 to eliminate N+1 on the locations page.

### 5. Deck-location pairing
A deck's physical cards are `InventoryRow` rows with `storage_location_id` pointing at the deck's `StorageLocation`. The `Deck` row holds metadata only. When a deck is deleted, its `StorageLocation` is deleted with it; the inverse is also enforced — `delete_location` refuses to delete a `type="deck"` location that has a linked `Deck` row, routing the user to the Decks page instead.

## Known Constraints

- **Nullable columns that should be NOT NULL.** Several columns (`Game.format`, `Game.status`, `GameSeat.user_id`) are `nullable=True` at the DB level because SQLite cannot `ALTER COLUMN` to tighten nullability without a full table rebuild. These are deferred to v4 (Postgres migration). Python-side defaults and service-layer validation enforce the non-null contract on new rows.

- **No DB-level `CHECK` constraints on enums.** `type`, `mode`, `role`, `status`, and similar enum columns are validated at the service layer only. Invalid values can exist in the DB if inserted outside the service layer (e.g. direct SQL). All `CHECK` constraints are reserved for v4.

- **`PRAGMA foreign_keys` is OFF.** SQLite foreign key enforcement is disabled project-wide. FKs are documentary and v4-Postgres forward-compat. Cascade deletes are handled explicitly in service code and `routes/admin.py`.

- **`type="deck"` guard is v3.30.17.** The `create_location` guard blocking direct deck-type creation was added in v3.30.17 after v3.30.15's `auto_create_locations` bypass produced orphan deck-locations. Pre-v3.30.17 data may contain `type="deck"` `StorageLocation` rows without a paired `Deck` row — these are flagged as `is_orphaned_deck` in `get_location_summary`.

- **Self-referential hierarchy has no depth limit.** The parent-child tree on `StorageLocation` is unbounded. No cycle detection beyond the immediate "cannot be own parent" guard.
