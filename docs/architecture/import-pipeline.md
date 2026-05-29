# Import Pipeline

**Last Updated:** 2026-05-28
**Status:** Current (v3.30.x)

## Purpose

The import pipeline turns external card data (CSV exports from scanner apps, Helvault, Moxfield, or pasted text lists from MTGA/MTGO) into `InventoryRow` records in the user's collection. It operates in two distinct stages: **parse/preview** (no DB writes, identifies valid and invalid rows, resolves location names) and **persist/commit** (writes rows to the DB, creates locations if needed).

The pipeline is deliberately side-effect-free during preview. Every Scryfall network call is batched — no per-row live fetches occur in the request path. This is the **v3.23.9 request-path-immunity principle**, added after a 5,758-row import caused a 524 timeout.

## Core Models

### `ImportBatch`
Created at the start of every persist operation. Records filename, row count, and timestamp. Every `TransactionLog` entry from an import references its `batch_id`.

### `InventoryRow`
The output of a successful import. Two placement outcomes:

- **Pending** (`is_pending=True`, `storage_location_id=None`, `drawer=None`, `slot=None`) — row lands in the pending queue for the drawer sorter to place later. Used when no Location column is present or the user doesn't resolve it.
- **Placed** (`is_pending=False`, `storage_location_id` set) — row lands directly in a resolved location. Used when the CSV carries a Location column that the user resolves at the preview step.

Imported rows never receive `drawer` or `slot` values directly — placement is always delegated to `resort_collection` to avoid slot collisions with existing rows.

### `StorageLocation`
May be created during the persist step (`auto_create_locations`) when a CSV's Location column names a location that doesn't exist yet. For `type="deck"`, creation routes through `deck_service.create_deck` so the paired `Deck` row is created atomically.

## Input Formats

| Format | Detected by | Identity |
|---|---|---|
| Scanner App / internal | `scryfall_id` column present | Scryfall UUID |
| Helvault | `extras` header present | set + collector |
| Moxfield | `edition` header present | set + collector |
| Text list (MTGA/MTGO) | `parse_text_list` entry point | set + collector, or bare name |

Header normalization (`normalize_header`) maps all known column name variants to canonical internal keys. The `HEADER_ALIASES` dict is the single source of truth for accepted column names.

## Major Flows

### 1. CSV parse — preview (`parse_scanner_csv`)

Three passes, no DB writes, no per-row network calls.

**Pass 1 — Row parsing.** Reads the CSV, normalizes headers and finish values, coerces language strings to Scryfall codes, validates `role` and `is_proxy` fields. Invalid `role` or `is_proxy` values route the row to `invalid_rows` immediately with an explicit reason. Quantity defaults to 1 on parse failure. All rows are buffered in `pre_rows`.

**Pass 2 — Batch Scryfall resolution.** Rows with a `scryfall_id` are batched to `bulk_refresh_prices`. Rows with only `set_code` + `collector_number` are batched to `bulk_fetch_by_set_number`. Both fetchers run once per import, not once per row. Results are stored in `id_map` and `set_map`.

**Pass 3 — Output row assembly (no network).** Each row is matched against Pass 2's maps. If a card was found, it becomes a `valid_row`. If not found, it becomes an `invalid_row` with a reason distinguishing "transient batch failure (retry)" from "genuinely unknown card." Nothing in Pass 3 calls Scryfall.

### 2. Location resolution — preview (`resolve_location_names`)

Called after `parse_scanner_csv` when the CSV carries a Location column. Distinct location names from the valid rows are extracted and matched against the user's existing `StorageLocation` records (case-insensitive, single query, no N+1).

Each distinct name resolves to one of three statuses:
- **`clean`** — exactly one existing match. Row will land there on commit.
- **`ambiguous`** — two or more existing matches. Preview surfaces a picker for the user to choose.
- **`missing`** — no existing match. Preview surfaces an auto-create confirm with an editable type dropdown.

The `csv_type_hint` from the CSV's Location Type column is used only for `missing` resolutions at auto-create time. For `clean` and `ambiguous` resolutions, the existing location's type wins (Decision 12).

### 3. Text list parse — preview (`parse_text_list`)

Same three-pass structure as CSV parse. Accepts Moxfield deck export format (`1 Card Name (SET) 145`), MTGA format (`1x Card Name (SET) 145`), bare set+collector pairs (`MH3 145`), and `*F*` foil / `*XX*` language markers. Section headers (`Deck`, `Sideboard`, `Commander`, etc.) are skipped. Bare card names without set+collector cannot be batch-resolved and surface as `invalid_rows` with an explanation — there is no batch name endpoint on Scryfall.

### 4. Commit (`persist_import_rows`)

Called after the user confirms the preview. Writes rows to the DB.

1. Creates an `ImportBatch` record.
2. For any rows still missing a `scryfall_id` after preview, runs a batch set+collector fallback (safety net for direct API callers that bypass preview).
3. Resolves `Card` records from the DB, inserting missing ones via `bulk_refresh_prices`. Uses `card_constructor_kwargs` to strip Scryfall-only keys (e.g. `produced_tokens`) that have no `Card` ORM column — v3.30.21 hotfix.
4. For each resolved row:
   - If `line_number` is in `line_to_location_id` (user resolved it at preview): creates an `InventoryRow` with `is_pending=False` and `storage_location_id` set.
   - Otherwise: checks for an existing pending row with the same `(user_id, card_id, finish, language)` key and merges (bumps quantity) or creates new pending.
5. Writes `TransactionLog` entries for every row.
6. Commits once per import.

The return dict includes `placed_row_ids_by_location` so the route handler can run `place_imported_rows` per resolved location to merge placed rows with any existing placed copies at that location.

### 5. Location auto-creation (`auto_create_locations`)

Called by the commit route before `persist_import_rows` when the preview step produced `missing` resolutions the user confirmed. Creates one `StorageLocation` per name using `create_location`. For `type="deck"`, routes through `deck_service.create_deck` so the paired `Deck` row is created atomically (v3.30.17 fix).

Handles three edge cases:
- **Location already exists** (race between preview and commit): uses the existing row, no duplicate.
- **Deck name already exists for this user** (`uq_decks_user_name`): reuses the existing deck's `storage_location_id`.
- **Legacy `decks.name` UNIQUE auto-index conflict** (pre-v3.1.0 installs): `IntegrityError` is caught, session is rolled back, the name is skipped. The row falls through to `target_location_id` behavior. Self-heals when v4 drops the legacy constraint.

## Invariants

1. **No per-row Scryfall calls in the request path.** All resolution is batched in Pass 2. A row that Pass 2 does not resolve becomes an `invalid_row` — it never triggers a live fetch. This is the v3.23.9 request-path-immunity principle.

2. **Imported rows never receive drawer/slot positions.** Both placement branches (`is_pending=True` and `is_pending=False`) set `drawer=None` and `slot=None`. Slot assignment is delegated to `resort_collection` to avoid collisions.

3. **`is_pending=False` rows must have a `storage_location_id`.** The placed branch in `persist_import_rows` only fires when `line_to_location_id` provides a resolved location ID. There is no code path that creates a placed row without a location.

4. **Invalid `role` values fail at parse time, not silently.** `parse_role` returns `(None, False)` for any non-empty, non-`"commander"` value. The row is routed to `invalid_rows` with an explicit reason. No silent coercion (spec Decision 9).

5. **Invalid `is_proxy` values fail at parse time, not silently.** `parse_proxy_bool` returns `(False, False)` for any value other than `"true"`, `"false"`, or empty. The row is routed to `invalid_rows` with an explicit reason (spec Decision 11).

6. **Language codes that are unrecognized default to `"en"`.** `normalize_language` never persists garbage values — any unrecognized input returns `"en"`. This is intentional: imports from third-party tools with variant language strings should not fail.

7. **The pending-merge key does not include `is_proxy`.** When merging into an existing pending row, the key is `(user_id, card_id, finish, language, False, None, None, True)` — `is_proxy` is hardcoded to `False`. The `role`/`tags`/`is_proxy` of the imported row are silently ignored when merging into an existing pending copy (v3.30.15 Decision 5 — a known limitation, deferred to a "round-trip merge semantics" follow-up).

8. **`notes` is always NULL on import.** Pre-v3.30.15, the Location column string was dumped into `InventoryRow.notes` (data loss). v3.30.15 stopped this. Existing rows with location strings in notes from older imports are not touched.

## Known Constraints

- **Bare card names cannot be batch-resolved.** Scryfall has no batch name endpoint. Paste-list lines without a set+collector suffix surface as `invalid_rows`. Users must add set+collector identifiers or use the name-search import UI.

- **Legacy `decks.name` UNIQUE auto-index on pre-v3.1.0 installs.** The `sqlite_autoindex_decks_1` index (from when `Deck.name` was `unique=True`) was not dropped at v3.1.0 because SQLite cannot drop inline-UNIQUE auto-indexes without a table rebuild. On affected installs, `auto_create_locations` catches the `IntegrityError` and skips the conflicting name. Fixed in v4 when the Postgres migration rebuilds the table.

- **Pending-merge semantics are coarse.** The merge key is `(user_id, card_id, finish, language)` — it doesn't distinguish `role`, `tags`, or `is_proxy`. A repeated import of the same card will merge quantities but silently discard the second row's role/tags/is_proxy. Deferred to the "round-trip merge semantics" follow-up.

- **`card_constructor_kwargs` strip is required.** `bulk_refresh_prices` may return keys (e.g. `produced_tokens`) that exist in the `scryfall_cards` cache table but not in the `Card` ORM model. `card_constructor_kwargs` strips these before `Card(**payload)` — required after v3.30.11 added `produced_tokens` to the scryfall cache. Without it, every cache-miss add-card or import path raises `TypeError` (v3.30.21 hotfix).

- **Duplicate detection is quantity-additive, not blocking.** When `compute_duplicate_counts_for_resolved` finds existing placed rows matching imported rows at the same location, it surfaces a warning count in the preview UI. The import still proceeds — duplicates are merged via `place_imported_rows`, not rejected. The "duplicate warning" means "this will add to an existing quantity," not "this will create a duplicate row."
