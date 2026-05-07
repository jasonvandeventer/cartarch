# Mana Archive — Claude Context

## Current version: v3.14.14

## Stack: FastAPI + Jinja2 + SQLite + K3s/ArgoCD

## Non-negotiable constraints

- InventoryRow is the single source of truth
- StorageLocation is the canonical location system (decks = type="deck")
- SQLite until v4 — do NOT suggest PostgreSQL changes
- No service layers unless already present
- Do NOT break existing routes or templates (live system)

## Current phase

Active user onboarding. Self-service registration in place — users sign up independently with email + display name.

## Architecture notes

### Drawer sorter

`DRAWER_SORTER_USERNAMES = frozenset({"jason.v", "test"})` in `app/dependencies.py` gates the automatic 6-drawer card sorter (`resort_collection`). Only these users get drawer/slot auto-assignment on import and access to the Drawers page, Audit page, and "Apply Drawer Sorter" button.

All other users manage their own StorageLocations and pick placement manually.

To add a user to the auto-sorter, update `DRAWER_SORTER_USERNAMES` in `app/dependencies.py` (one place — it's injected as a Jinja2 global and imported into `main.py`).

### Import destination

All import paths (CSV, paste list, and manual) present a **Destination** dropdown at commit time. For drawer-sorter users the first option is "Auto-sort to drawers" (existing behaviour); any other selection places cards directly into that StorageLocation and skips pending entirely. For other users, a location must be chosen.

**Decks appear as destinations** in the dropdown (as a separate `<optgroup>`), using the deck's `storage_location_id` as the value. This lets users import directly into a deck without the placement step. `place_imported_rows()` in `inventory_service.py` handles bulk placement to any location, including deck locations.

The `decks` list (from `list_decks()`) is passed to all three preview templates: `import_preview.html`, `manual_preview.html`.

### Security (added v3.4.6)

- CSRF protection: session token via `CsrfRequired` dependency on all POST routes; `{{ csrf_token }}` hidden field in every form
- Open redirect prevention: `safe_redirect_url()` in `main.py` validates Referer before redirect
- ValueError handler: returns clean 400 instead of 500 stack trace
- Session secret: startup check refuses to boot in production without `SESSION_SECRET_KEY` env var

### Shared rendering

`render()`, `CsrfRequired`, `get_csrf_token()`, and `get_current_user()` all live in `app/dependencies.py`. Do not redefine them elsewhere.

### StorageLocation auto-creation

`_get_or_create_drawer_location()` in `inventory_service.py` bootstraps missing drawer StorageLocations on first confirm. Prevents 500s for users whose drawer rows don't exist yet.

### DeckItem removed (v3.5)

`DeckItem` model and `deck_items` table are gone. Deck cards have always been `InventoryRow` records with `storage_location_id` pointing to the deck's StorageLocation — DeckItem was dead code after the v3.4 migration. The drop is in `scripts/migrate_v3_5_drop_deck_items.py`.

### Commander role (v3.5)

`InventoryRow.role` (nullable String(32)) marks a card's role within a deck. Currently only value used is `"commander"`. Set via `POST /decks/rows/{row_id}/toggle-commander`. In `deck_detail.html`, cards with `role=="commander"` appear in a separate **Commander(s)** panel above the main deck grid. Added via `scripts/migrate_v3_5_inventory_role.py`.

### Migrations

Idempotent migration scripts live in `scripts/`. `scripts/run_migrations.py` is the runner — add new migrations there in order. Each migration is tracked by name in the `schema_migrations` SQLite table. `run_migrations()` is called from `on_startup()` in `main.py`, so every deploy automatically applies pending migrations before the app serves traffic.

## Telemetry query

kubectl exec -n mana-archive deploy/mana-archive -- \
python -c "from sqlalchemy import text; from app.db import engine; \
conn = engine.connect(); \
rows = conn.execute(text('''
SELECT u.username,
COUNT(DISTINCT ir.id) as inventory_rows,
COUNT(DISTINCT d.id) as decks
FROM users u
LEFT JOIN inventory_rows ir ON u.id = ir.user_id
LEFT JOIN decks d ON u.id = d.user_id
GROUP BY u.username
ORDER BY inventory_rows DESC
''')).fetchall(); \
print('\n'.join(str(r) for r in rows)); conn.close()"

### Import format support (v3.6)

Two import paths on `/import`:

**CSV upload** — `parse_scanner_csv()` auto-detects format from column headers:

- **Scanner App** (default): `scryfall_id / set_code / collector_number / finish / quantity`
- **Helvault** (free & pro): detected by `extras` column (→ `finish`). Resolved via Scryfall ID.
- **Moxfield** collection CSV: detected by `Edition` column. Maps `Edition → set_code`, `Foil → finish`, `Count → quantity`. Resolved via set+collector number.

**Paste card list** — `parse_text_list()` parses `N CardName (SET) Collector#` format:

- Accepts Moxfield deck exports, MTGA, MTGO, and any standard text list format.
- Lookup priority: set+collector → exact name+set → fuzzy name.
- `fetch_card_by_name()` in `scryfall.py` handles exact then fuzzy Scryfall `/cards/named` lookups.
- Section headers (Deck, Sideboard, Commander, etc.) are silently skipped.
- MTGA foil marker (`*F*`) is detected and mapped to finish=foil.

Both paths normalize to the same row dict and feed into `persist_import_rows()` via the shared preview → commit flow. `format_name` is shown on the preview page.

### UI/UX consistency (v3.7)

All templates use a consistent panel/section structure:

- Hero sections: `class="panel page-hero"` (large) or `class="panel hero-panel compact-hero"` (compact)
- Action/filter strips: `class="panel controls-panel"` (block layout; form inside uses `.filter-row` for flex)
- Content panels: `class="panel"` with `<h3 class="panel-title">` inside
- Tables: globally styled — no extra class needed, just bare `<table>`
- CSS utilities in `style.css`: `.controls-panel`, `.btn-danger-small`, `.finish-badge`, `.warning-text`

Templates updated in v3.7: `decks.html`, `import.html`, `import_preview.html`, `manual_preview.html`, `audit.html`, `sets.html`, `set_detail.html`, `pending.html`, `locations.html`, `drawers.html`, `card_detail.html`, `login.html`, `register.html`, `manual_search_results.html`, `import_result.html`.

### Admin and account (v3.7)

- `GET /admin` — admin-only page (gated by `User.is_admin`). Shows all users with card count, deck count, last activity. Actions: toggle active/inactive, toggle admin, reset password, create new user, delete user (cascade-deletes all their data).
- `GET /account` + `POST /account/change-password` — available to all authenticated users; password change requires current password verification.
- `require_admin` dependency in `app/dependencies.py` — raises 403 if `current_user.is_admin` is false.
- Admin/Account links appear in nav. Admin link is gated by `current_user.is_admin`.
- Migration `v3_7_admin_user` ensures `users.is_admin` column exists and seeds `jason.v` as admin.
- Delete user cascade order: TransactionLog → InventoryRow → ImportBatch → Deck → StorageLocation → User.

### Card attributes (v3.8)

- `Card` model gains `colors` (space-sep WUBRG, e.g. `"W U"`), `color_identity` (space-sep WUBRG, `""` = colorless — distinct from `colors` for cards with colored abilities/land types), `mana_cost` (e.g. `"{2}{W}"`), `cmc` (float). Migration `v3_8_card_attrs` adds colors/mana_cost/cmc; migration `v3_8_8_color_identity` adds color_identity. Refresh loop backfills cards with NULL `color_identity`.
- Extended search syntax everywhere: `c:WU`, `cmc:>3`, `mana:{W}`, on top of existing `t:`, `o:`, `s:`, `r:`, `finish:`.
- New sort options on collection and location detail: Type, Color (WUBRG order), Mana Cost.
- Location detail now has search + sort controls (previously unsorted, no search).
- Deck detail search upgraded from plain substring to full Scryfall-style syntax.
- Unified card display via `_macros.html` `inventory_card` macro — collection, location detail, deck detail all render from one place.
- Import resort is now a background daemon thread (non-blocking); explicit "Apply Drawer Sorter" stays synchronous.
- Pre-commit hook at `.githooks/pre-commit` mirrors CI lint checks. New developers run `git config core.hooksPath .githooks`.
- Token tracking on set detail: "Show Tokens" toggle fetches `t{set_code}` token set; tokens tracked ownership-only (no USD price).

### Deck analytics (v3.8.4)

- `compute_deck_analytics(rows)` in `deck_service.py` — takes a list of unfiltered `InventoryRow` ORM objects and returns mana curve (bucketed 0–6+, lands excluded), card type breakdown (Creature → Planeswalker → Battle → Instant → Sorcery → Enchantment → Artifact → Land → Other), color pip counts (parsed from `mana_cost`), and average CMC.
- `compute_deck_tokens(rows)` in `deck_service.py` — returns deduplicated list of `{name, type_line, image_url, set_code, collector_number, scryfall_id}` dicts for tokens produceable by the deck. Calls `fetch_deck_tokens()` in `scryfall.py`: Pass 1 batch-fetches deck cards to collect token stubs from `all_parts` (component="token"); Pass 2 batch-fetches the token cards themselves for `image_uris` and set info. Cached per unique card set keyed on `(_DECK_TOKEN_CACHE_VERSION, frozenset)` — bump version when dict shape changes.
- `deck_detail_page` in `main.py` always runs a separate unfiltered query for analytics so the panel reflects the full deck even when the search filter is active. Analytics are `None` for empty decks.
- Analytics panel in `deck_detail.html` — 3-column layout: Mana Curve | Card Types | Color Pips. Avg CMC shown as a prominent stat. Collapses to 2-col when no pips, stacks on mobile. Vertical dividers between columns.
- Tokens panel in `deck_detail.html` — separate panel below the deck card grid. Responsive image grid (`token-image-grid`); clicking a token image opens `/tokens/{scryfall_id}` (internal detail page); clicking the name opens Scryfall in a new tab.
- `GET /tokens/{scryfall_id}` — token detail page using `token_detail.html`. Fetches token data via `fetch_card_by_scryfall_id` (cached). Shows large image, type line, oracle text, Scryfall link. No inventory section (tokens not owned).
- Remove-from-deck override fields (drawer/slot) collapsed into a `<details class="return-details">` — "Remove from Deck" summary expands to reveal override inputs + "Confirm Remove" submit. Only shown for drawer-sorter users on non-commander rows.
- CSS: `.analytics-grid`, `.analytics-section`, `.analytics-avg-cmc`, `.analytics-curve`, `.curve-col/.curve-bar/.curve-count/.curve-label`, `.analytics-row`, `.arow-label/.arow-bar-wrap/.arow-bar/.arow-count`, WUBRG gradient bars, `.token-image-grid/.token-card/.token-card-img/.token-card-placeholder/.token-card-name/.token-card-type`, `.return-details/.return-summary`.

### Brand assets and header layout (v3.8.3)

- `app/static/icons/` — actual brand PNGs at 15 sizes (16–1024px) using card-frame app icon design; wordmark PNGs at 256/512/1024px; `favicon.ico` built from 16/32/48px icons.
- `base.html` favicon chain: `favicon.ico` (legacy) → `icon-32x32.png` (PNG fallback) → `icon-180x180.png` (Apple touch icon).
- Header restructured to two-column flex layout: left column = wordmark + nav stacked; right column = version pill + logout stacked. This aligns logout with the nav row at the far right.
- Brand area uses `wordmark-1024.png` displayed at 44px height (`class="brand-wordmark"`) — no separate icon + text elements.
- CSS classes: `.header-left` (flex-column, space-between), `.brand-wordmark` (height 44px, auto width). Removed `.brand-row`, `.brand-icon`, `.brand-text`.
- `list_decks()` in `deck_service.py` now sums `InventoryRow.quantity` (total copies) for `card_count` instead of counting distinct rows.

### Location page deck management (v3.8.2)

- `POST /locations/create-deck` — creates a proper `Deck` record + linked `StorageLocation` from the Locations page; redirects back to `/locations`. Form has name + format dropdown (same options as Decks page).
- Orphaned deck locations (type="deck", no linked `Deck` record, no rows) now show a Delete button in the Locations table. `delete_location()` allows deletion when no `Deck` references the location; blocks with a clear error if a live `Deck` still owns it.
- `get_location_summary()` computes `is_deletable` per location (used by template to show/hide Delete); avoids duplicating the logic in the template.

### Deck/location UX fixes (v3.8.1)

- `GET /locations/{id}` redirects to `/decks/{deck_id}` when `location.type == "deck"` — eliminates duplicate access path.
- Deck detail gains sort controls (Name, Type, Mana Cost, Price) matching location detail.
- Import destination dropdowns (`import_preview.html`, `manual_preview.html`) exclude `type="deck"` locations from the Storage Locations optgroup — deck locations only appear under the Decks optgroup.
- `delete_location()` in `location_service.py` + `POST /locations/{id}/delete` route — deletes empty non-root, non-deck locations. Delete button appears in locations table only when `row_count == 0`.
- `location_types` in create form excludes "deck" — prevents creating orphaned deck-type StorageLocations not linked to a Deck record.
- Collection card actions collapsed into a `<details class="card-actions-drawer">` — cards show only info by default; "Actions ▾" expands Remove, Add to Deck, Move, and Sell/Trade/Delete/Refresh inline.
- Fixed deck card tile overflow: deck actions section now uses `flex-direction: column`; removed misapplied `compact-form-grid` class from return form.

### Boolean search logic (v3.8.5)

- `apply_collection_search_filters()` in `inventory_service.py` now parses full Scryfall-style boolean logic. Public signature unchanged — all three search surfaces (collection, location detail, deck detail) get the upgrade for free.
- **Operators**: `OR` (explicit), `AND` (explicit or implicit between adjacent terms), `-` prefix for negation (e.g. `-t:land`, `-folio`).
- **Grouping**: parentheses `(t:creature OR t:planeswalker)` for complex expressions.
- **Quoted multi-word values**: `t:"legendary creature"`, `o:"draw a card"`, `"lightning bolt"`.
- Implementation: `_tokenize_search()` → flat token list; `_term_to_clause()` → single SQLAlchemy clause; `_parse_search_expr()` / `_parse_and_expr()` / `_parse_atom()` → recursive-descent parser building nested `and_()` / `or_()` / `not_()` clauses.
- Malformed queries fall back to no-filter rather than 500.

### Search polish (v3.8.6)

- `OR` / `AND` keywords are now case-insensitive (`or`, `Or`, `OR` all work).
- `not:X` is syntactic sugar for `-is:X` (double-negation via `-not:X` cancels correctly).
- New keywords in `_term_to_clause()`:
  - `is:foil` / `is:nonfoil` / `is:etched` — finish filter; `not:foil` inverts
  - `is:commander` — cards flagged as commander in a deck
  - `n:` / `name:` — explicit name prefix (same as bare word, useful in complex expressions)
  - `qty:`/`q:`/`quantity:` — numeric quantity filter (e.g. `qty:>1` to find duplicates)
  - `price:`/`usd:` — numeric price filter against `Card.price_usd` cast to float (e.g. `price:>=5`)
- `id:` color identity filter: "within" subset check — excludes cards containing any color not in the given set. Uses `Card.color_identity` (exact Scryfall field, added v3.8.8); cards with `NULL` identity are excluded until backfilled.
- Placeholder text in all three search inputs updated to show real example queries with boolean syntax.

### Legality filter (v3.9.6)

- `Card.legalities` (TEXT, nullable) — JSON-encoded dict from Scryfall `legalities` field (e.g. `{"commander": "legal", "modern": "not_legal"}`). Added via migration `v3_9_6_legalities`. `NULL` = not yet fetched; backfilled by the price refresh loop.
- `_normalize_card_payload()` in `scryfall.py` now includes `"legalities": json.dumps(raw.get("legalities") or {})`. `refresh_card_from_scryfall()` writes `card.legalities`; price refresh loop does too.
- `get_card_legality(card, format_name) -> str | None` in `deck_service.py` — parses JSON, lowercases format name, returns legality value ("legal", "not_legal", "banned", "restricted") or None.
- Deck detail items dict includes `"legality_status": get_card_legality(row.card, deck.format)`.
- New search keywords in `_term_to_clause()`:
  - `legal:FORMAT` — cards legal in that format (e.g. `legal:commander`)
  - `banned:FORMAT` — cards banned in that format (e.g. `banned:modern`)
  - Both use SQLite `json_extract(legalities, '$.format')` comparison.
- Legality badge in `_macros.html` in deck context — shown only when status is not "legal" and not None: red "Banned", orange "Restricted", amber "Not Legal".
- CSS classes: `.legality-badge`, `.legality-banned`, `.legality-restricted`, `.legality-not-legal`.
- Deck format values are Title Case in UI ("Commander", "Modern") — `get_card_legality()` lowercases before JSON key lookup, so they match Scryfall's lowercase keys.

### Deck health (v3.9.0)

- `compute_deck_health(rows)` in `deck_service.py` — takes unfiltered `InventoryRow` ORM objects and returns four functional-density metrics plus pip strain analysis.
- **Functional density metrics** (each: `{count, cards, threshold}`):
  - **Ramp**: non-land cards with `"add {"` in oracle text + any non-basic card with a land-tutor pattern (`search your library for ... land`). Threshold: 10.
  - **Draw**: cards matching `draw (a|an|x|N|two–six|that many) cards?`. Threshold: 10.
  - **Removal**: cards matching `(destroy|exile) target ... (creature|artifact|enchantment|planeswalker|permanent)`. Threshold: 8.
  - **Board Wipes**: `destroy all`, `exile all creatures/permanents`, `all creatures get -N/-N`, `deals N damage to each creature`. Threshold: 2.
  - `count` = number of distinct card names; `cards` = sorted list for the expandable UI.
- **Pip strain** (`pip_strain` dict keyed by color letter):
  - `demand` = sum of colored pips of that color across all non-land `mana_cost` (quantity-weighted).
  - `sources` = sum of quantities of land cards whose `color_identity` contains that color.
  - `ratio` = demand/sources (or `None` if no sources); `strained = ratio > 2.5 or ratio is None`.
  - Only colors with nonzero demand are included.
- **Deck Health panel** in `deck_detail.html` — two-column layout: left = Functional Density rows (bar + count/threshold + expandable card list); right = Pip Strain rows (pip symbol + bar + demand/sources/ratio). Color-coded: green (at/above threshold), yellow (≥60%), red (<60%); strained pips shown in red.
- CSS classes: `.health-grid`, `.health-metrics`, `.health-pips`, `.health-row`, `.health-pip-row`, `.health-bar(-ok|-warn|-low)`, `.health-count(-ok|-warn|-low)`, `.health-cards-details`, `.health-cards-list`, `.pip-sym(-w|-u|-b|-r|-g)`.

### Self-service onboarding and display names (v3.10.6)

- `User.display_name` (String(64), nullable) — friendly name shown in the UI. Migration `v3_11_display_name` adds the column.
- Login uses `username` field (stored as email for new registrations). All UI shows `display_name or username` as the fallback — existing accounts (e.g. `jason.v`) continue to work unchanged.
- `POST /register` now requires email format (server-side `@` + domain check), collects Display Name, and auto-derives display name from email prefix if left blank.
- Login page links to `/register` — no admin involvement needed for new users.
- Admin Create User form accepts Display Name + Email fields.

### Editable locations and decks (v3.10.6)

- `update_location()` in `location_service.py` — edits name, type, parent_id, sort_order. Blocked on `root` and `deck` types.
- `update_deck()` in `deck_service.py` — edits name, format, notes. Also renames the linked `StorageLocation` to keep the import destination dropdown in sync.
- Routes: `POST /locations/{id}/edit`, `POST /decks/{id}/edit`.
- UI: floating `<details>` popout per row on `locations.html` and `decks.html` — uses `.inline-details` / `.edit-popout` / `.btn-like` CSS classes. The popout uses `position: absolute` so it overlays the table rather than pushing rows.

### Move cards from deck detail (v3.10.7–v3.10.8)

- `deck_detail_page` now fetches and passes `locations` to the template.
- Per-card **Move to Location** dropdown added to deck card actions in `_macros.html` (inside `show_deck_actions` block, after Remove from Deck). Uses `rejectattr("type", "equalto", "deck")` + `rejectattr("id", "equalto", deck.storage_location_id)` to split into Storage Locations / Decks optgroups, excluding the current deck.
- **Bulk Move** panel on deck detail — collapsible `<details>` above the card grid, same checklist pattern as location detail. Routes to `POST /decks/{id}/bulk-move`.
- Both dropdowns show `<optgroup label="Storage Locations">` and `<optgroup label="Decks">` with the current deck excluded from the deck group.

### Move cards from location detail (v3.10.6)

- `location_detail_page` now fetches and passes `locations` and `decks` to the template.
- `location_detail.html` calls `inventory_card` with `show_collection_actions=true` — gives full per-card actions (Move, Add to Deck, Remove, Sell, Trade, Delete, Refresh).
- **Bulk Move panel** — collapsible `<details>` above the card grid; shows a scrollable checkbox list of all cards in the location + a destination picker. "Select all" toggle via inline `onclick`. Submits to `POST /locations/{id}/bulk-move` which loops `move_inventory_row_to_location()` for each selected `row_id`.

### Win condition detection (v3.11.0)

- `app/spellbook.py` — `fetch_deck_combos(main_names, commander_names)` POSTs to `https://backend.commanderspellbook.com/find-my-combos/` with `{"commanders": [{"card": name}], "main": [{"card": name}]}`. Returns `{included, almost}`.
- `included` = combos where all pieces are in the deck. `almostIncluded` from the API response is ignored.
- 1-hour in-memory cache keyed on `(_COMBO_CACHE_VERSION, frozenset(all_names))`. Bump `_COMBO_CACHE_VERSION` in `spellbook.py` to invalidate.
- `compute_deck_combos(all_rows)` in `deck_service.py` — extracts commander vs main card names from `row.role`, calls `fetch_deck_combos`. Called from `deck_detail_page` on the unfiltered `all_deck_rows`.
- Each combo dict: `id, card_names, owned, missing, description, results, prerequisites, mana_needed, popularity`.
- **Win Conditions panel** in `deck_detail.html` — "Complete combos in this deck" section + "One card away" section. Each combo shows card pills (missing card styled in amber), result badges (green), expandable "How it works" with step-by-step description and setup prerequisites.
- CSS classes: `.combo-panel`, `.combo-section-label`, `.combo-item`, `.combo-item-almost`, `.combo-cards`, `.combo-card-name`, `.combo-card-missing`, `.combo-results`, `.combo-result-badge`, `.combo-details`, `.combo-summary`, `.combo-description`, `.combo-prereq`.

### Commander theme extraction (v3.11.11)

- `extract_commander_themes(commander_rows)` in `deck_service.py` — parses all commander oracle texts and returns a structured theme dict consumed by synergy, and in future by recommendations and health calibration.
- **Card types**: detected via positive patterns (`"whenever you cast a/an {type}"`, `"{type}s you control"`, `"{type} spells"`, etc.). Removal context (`"destroy/exile/counter target … {type}"`) is excluded to avoid false positives.
- **CMC gate**: `_CMC_MIN_RE` / `_CMC_MAX_RE` extract numeric thresholds from `"mana value N or greater/less"` phrases (e.g. Bello → `{"min": 4}`).
- **Non-X exclusions**: `_NON_SUBTYPE_RE` captures `"non-Aura"`, `"non-Human"` etc. → `excluded_subtypes` set applied when matching deck cards.
- **Mechanics**: counters, tokens, graveyard, sacrifice, discard detected from oracle text patterns.
- **Tribal subtypes**: extracted from commander type line but only included if the subtype also appears in oracle text (e.g. Edgar Markov mentions "Vampire" → tribal; Bello does not mention "Halfling" → no tribal).
- `card_matches_theme(card, themes)` — checks tribal, card type (with exclusions + CMC gate), and mechanics; used in `compute_deck_synergy()`.
- `extract_commander_themes` is the shared foundation for v3.12 owned recommendations and future health calibration.

### Commander Synergy score (v3.11.10)

- `compute_deck_synergy(all_rows, combos)` in `deck_service.py` — classifies each non-commander card into three buckets and returns counts, percentages, and card lists.
- **Direct**: card appears in a complete Spellbook combo, tagged Combo or Payoff, or shares a creature subtype with any commander (tribal match). Subtypes extracted from commander type line after "—".
- **Supporting**: tagged Ramp, Draw, Removal, Wipe, Tutor, or Protection; or is a Land. Direct takes priority if both apply.
- **Unrelated**: neither of the above.
- Returns `None` if no commander is tagged or deck is empty.
- **Synergy panel** in `deck_detail.html` — between Health and Combos panels. Shows a stacked horizontal bar (blue=direct, green=supporting, gray=unrelated) and three expandable stat blocks (dot + label + count/pct + scrollable card list in two columns). Tribal match note shown when commander has creature subtypes.
- CSS classes: `.synergy-bar`, `.synergy-seg`, `.synergy-seg-direct/supporting/unrelated`, `.synergy-stats`, `.synergy-stat`, `.synergy-stat-details`, `.synergy-dot`, `.synergy-stat-label`, `.synergy-stat-count`, `.synergy-stat-pct`, `.synergy-card-list`, `.synergy-subtype-note`.

### Commander Bracket estimation (v3.11.5)

- `compute_deck_bracket(all_rows, combos)` in `deck_service.py` — floor-based bracket estimator using multiple deck signals; returns `{bracket: 1-5, reasons: [...], signals: {...}}`.
- **Signal frozensets**: `_FAST_MANA` (Mana Crypt, Mox Diamond, Chrome Mox, Mox Opal, Jeweled Lotus, Grim Monolith, Mana Vault, Lotus Petal, Ancient Tomb), `_FREE_INTERACTION` (Force of Will, Force of Negation, Mana Drain, Fierce Guardianship, Deflecting Swat, Flusterstorm, Mental Misstep, Pact of Negation, Commandeer), `_MASS_LAND_DENIAL` (Armageddon, Ravages of War, Jokulhaups, Devastation, Obliterate, Decree of Annihilation, Catastrophe, Ruination, Boom // Bust).
- **Floor logic** (signals raise the minimum bracket):
  - Tutors (non-basic-land search) → floor 2
  - 1+ complete combo, 1+ mass land denial, or 1+ extra turn card → floor 3
  - Any fast mana or free interaction → floor 4
  - 2+ fast mana + 1+ free interaction + 2+ combos → bracket 5
- **Bracket badge** in deck detail hero stats — `<details class="bracket-details">` with `<summary class="bracket-badge bracket-N">` colored per bracket (1=green, 2=blue, 3=yellow, 4=orange, 5=red); click/open shows `.bracket-popout` with reasons list.
- CSS classes: `.bracket-details`, `.bracket-badge`, `.bracket-1` through `.bracket-5`, `.bracket-popout`, `.bracket-popout-title`, `.bracket-reasons`.

## Deployment and versioning

- CI builds and pushes to GHCR on any tag matching `v*.*.*`. Untagged commits run lint only.
- ArgoCD Image Updater (semver strategy) watches GHCR and writes the new tag to `.argocd-source-mana-archive.yaml` in `mana-archive-platform`, which ArgoCD then syncs to the cluster.
- **Version convention**: always bump the patch number — never use `-N` suffixes. `v3.8.9` → hotfix → `v3.8.10`. Semver treats `-N` as a pre-release (sorts _below_ the base tag) so the Image Updater ignores it.
- **Tagging is automatic**: the `.githooks/post-commit` hook tags HEAD whenever the commit message starts with `vX.Y.Z:`. No separate `git tag` step needed.
- New developers must run `git config core.hooksPath .githooks` to activate both the pre-commit lint check and the post-commit auto-tag.

## Roadmap

- v3.7: Import-to-deck, decks list redesign, full UI/UX consistency pass, admin CRUD, account page — **shipped**
- v3.8: Card attrs (colors/cmc/mana_cost), async resort, extended search, unified card macro, token tracking, pre-commit hook — **shipped**
- v3.8.1: Deck/location UX fixes, collection action drawer — **shipped**
- v3.8.2: Location page deck creation, orphaned deck location cleanup — **shipped**
- v3.8.3: Brand assets (real PNG icon pack + wordmark), header two-column layout, deck total-copy count — **shipped**
- v3.8.4: Deck analytics panel (mana curve, card types, color pips, avg CMC) — **shipped**
- v3.8.5: Boolean search logic (OR, AND, NOT/-, parentheses, quoted multi-word values) — **shipped**
- v3.8.6: Search polish — case-insensitive OR/AND, not: keyword, is:/qty:/price:/name: keywords, updated placeholders — **shipped**
- v3.8.7: id: color identity filter bug fixes — NULL colors excluded by SQLite NOT LIKE; refresh loop now also picks up cards with NULL colors; one-time backfill via individual + set/collector Scryfall fallback fixed ~1,400 stale scryfall_ids — **shipped**
- v3.8.8: `color_identity` column on `Card` — proper Scryfall `color_identity` field (space-sep WUBRG, `""` = colorless, `NULL` = not yet fetched); `id:` filter now uses this instead of approximating from `colors`; migration `v3_8_8_color_identity` adds column; refresh loop and all card-write paths updated — **shipped**
- v3.8.9: Deck token panel (image grid, `/tokens/{scryfall_id}` detail page), collapse remove-from-deck overrides into `<details>`, post-commit auto-tag hook — **shipped**
- v3.8.10: Collection location filter now works for non-drawer locations (decks, custom storage); stats (total value, total cards, matching rows) also scoped correctly — **shipped**
- v3.9.0: Deck health panel — ramp/draw/removal/board-wipe density counts with recommended thresholds and expandable card lists; pip strain analysis (colored pip demand vs land color sources, ratio >2.5 flagged as strained) — **shipped**
- v3.9.1: Health metric chips link to filtered deck card list — **shipped**
- v3.9.2: Fix health_filter= param name mismatch — **shipped**
- v3.9.3: Enhanced mana curve — stacked bars (ramp/spells), avg threat turn estimate, dead-hand risk indicator (% CMC≥5) — **shipped**
- v3.9.4: Consistency score — draw/ramp/tutor/curve-smoothness/coverage → 0-100 score with label (Consistent engine → Glass cannon) and optional descriptor; compact header in health panel — **shipped**
- v3.9.5: Card role tagging — user-defined per-row tags (Ramp, Draw, Removal, Combo piece, Payoff, Protection, etc.); multi-role support; schema migration; unlocks deeper analytics — **shipped**
- v3.9.6: Legality filter — `Card.legalities` JSON column; `legal:FORMAT` / `banned:FORMAT` search keywords; legality badge (Banned/Restricted/Not Legal) on deck cards when format is set — **shipped**
- v3.9.7: Legalities backfill — added `Card.legalities == None` to refresh loop stale filter so existing cards get legalities populated — **shipped**
- v3.9.8: Auto-tag untagged deck rows from oracle text on deck load (Ramp/Draw/Removal/Wipe) — **shipped**
- v3.9.9: Mana pip size 20→24px, added drop-shadow — **shipped**
- v3.10.0–v3.10.4: Mana pip SVGs — iterative replacement; final v3.10.4 uses Scryfall card-symbols SVGs directly (`svgs.scryfall.io/card-symbols/{W,U,B,R,G}.svg`). Structure: colored circle background + `#0D0F0F` positive-space symbol path. Colorless (C) still uses Scryfall CDN in `_macros.html` — **shipped**
- v3.10.5: Fix missing `.stack-form` CSS — labels and inputs were rendering inline in all browsers — **shipped**
- v3.10.6: Self-service onboarding, fully editable locations/decks, move cards from location detail — **shipped**
- v3.10.7: Move cards feature on deck detail — per-card Move to Location dropdown + Bulk Move panel — **shipped**
- v3.10.8: Move destination dropdowns include other decks; Storage Locations / Decks optgroups — **shipped**
- v3.10.9: Fix partner commander color identity — union all commanders' `color_identity` (not `.first()` + `colors`); affects both decks list and deck detail header — **shipped**
- v3.11.0: Win condition detection — CommanderSpellbook API integration; `app/spellbook.py` POSTs deck card list to `/find-my-combos/`; shows complete combos in deck + "one card away" near-combos (missing exactly 1 card, top 10 by popularity); 1-hour in-memory cache keyed on card set; combo panel in `deck_detail.html` with card pills, result badges, and expandable step-by-step description — **shipped**

### Mana pip SVG notes

- Local files at `app/static/mana/{W,U,B,R,G}.svg` — downloaded directly from `svgs.scryfall.io/card-symbols/`.
- Structure: `<circle fill="<mana-color>"/>` + `<path fill="#0D0F0F"/>` (positive-space symbol).
- Rendered at 24×24px via `.mana-pip` CSS class with drop-shadow filter.
- Colorless (C) pip still served from Scryfall CDN in `mana_pips` macro in `_macros.html`.
- To update: re-download from Scryfall CDN; the B symbol uses `fill-rule="evenodd"` for skull detail holes.

- v3.11.1: Collapse deck card actions behind "Actions" toggle — wraps tag editor, Mark/Remove Commander, Remove from Deck, and Move to Location inside `<details class="card-actions-drawer">` to match collection card behavior; tag role badges remain always visible — **shipped**
- v3.11.2: Remove "one card away" near-combos from Win Conditions panel — show only complete combos present in the deck; trim almostIncluded processing from spellbook.py — **shipped**
- v3.11.3: Fix resort_collection and list_pending_rows including deck cards — both functions now outerjoin StorageLocation and exclude rows where type="deck"; deck cards no longer appear in Pending Placement; migration `v3_11_3_clear_deck_pending` clears is_pending on any deck rows already incorrectly flagged — **shipped**
- v3.11.4: Tag current HEAD to trigger CI build including recovery script and linter-reformatted templates; no functional changes — **shipped**
- v3.11.5: Commander Bracket estimation — floor-based 1-5 bracket estimator using fast mana, free interaction, combos, tutors, mass land denial, extra turns; bracket badge with color-coded popout reasons in deck detail hero — **shipped**
- v3.11.6: Bracket badge on decks list — `list_decks()` computes bracket per deck (full Spellbook combo data via cache); Bracket column added to decks table — **shipped**
- v3.11.7: Decks list bracket uses full combo data (same as deck detail) — `list_decks()` calls `compute_deck_combos` + `compute_deck_bracket`; Spellbook in-memory cache means warm loads add zero API calls — **shipped**
- v3.11.8: Bracket 1 reason + deck export — Bracket 1 now shows a reason ("no tutors, fast mana…") in its popout; `GET /decks/{id}/export` returns a plain-text download in standard `N CardName (SET) #collector` format with Commander/Deck sections; Export button in deck detail hero — **shipped**
- v3.11.9: Health score on decks list — `list_decks()` also computes `compute_consistency()`; Health column shows the 0-100 badge (same `.consistency-badge.cs-*` classes) with label as tooltip — **shipped**
- v3.11.10: Commander synergy score — `compute_deck_synergy(all_rows, combos)` classifies each non-commander card as Direct (combo piece, Combo/Payoff tag, or shares commander creature subtype), Supporting (engine tags or land), or Unrelated; stacked bar + three expandable stat blocks in deck detail between Health and Combos panels — **shipped**
- v3.11.14: Remove "creature" from card type detection — generic "creature" caused false positives whenever oracle text described something becoming a creature (e.g. Bello "is a 4/4 Elemental creature"); tribal synergy is already handled by the subtype mechanism — **shipped**
- v3.11.15: Lazy-load slow deck panels — `deck_detail_page` now returns immediately (analytics/health/consistency only); bracket badge, synergy, combos, and tokens load via `GET /decks/{id}/panels` HTML fragment endpoint after page paint; JS uses `DOMParser` + `outerHTML`/`replaceWith` to swap bracket placeholder in hero and panels container below the card grid; `_deck_panels.html` fragment template; `{% block extra_scripts %}` added to `base.html`; panels endpoint runs `compute_deck_tokens` + `compute_deck_combos` in parallel (`ThreadPoolExecutor`); results disk-cached in `/data/panels_cache/{deck_id}.json` (24h TTL, keyed on hash of card set + quantities, survives restarts); also fixed catastrophic regex backtracking in `_CARE_ABOUT_PATTERNS` — `(?:\w+[-\w]* )*` replaced with `[^.;]*` making `compute_deck_synergy` drop from ~50s to <1ms — **shipped**
- v3.11.20: Batch Scryfall lookups on import preview — `parse_scanner_csv` (3-pass: parse → batch by ID via `bulk_refresh_prices` + batch by set/collector via `bulk_fetch_by_set_number` → apply) and `parse_text_list` (2-pass: parse all lines → batch-fetch all set+collector pairs, then individual name lookups for the rest) now make O(N/75) requests instead of O(N); `persist_import_rows` also batch-fetches new cards not yet in the local DB via `bulk_refresh_prices` instead of one call per missing card — **committed**
- v3.11.19: Optimise resort_collection for large batches — pre-load all 6 drawer StorageLocations in one query (was 6 separate queries); compute `assign_drawer` once per row instead of twice; replace N individual ORM UPDATE statements with a single `executemany` SQL batch; only write audit log entries for cross-drawer physical moves, not same-drawer slot renumbering (eliminates O(N) inserts for bulk imports) — **shipped**
- v3.11.18: Fix import resort race condition — resort now runs synchronously (same session, same request) in both CSV and manual commit handlers for drawer-sorter users, replacing the background thread approach; eliminates race condition where `/pending` loaded before the background thread committed, causing cards to display as "Drawer - · Slot ?" — **shipped**
- v3.11.17: Auto-resort on non-deck import for drawer-sorter users — both CSV and manual commit handlers now fire `_bg_resort` after `place_imported_rows()` whenever the target location is not a deck and the user is in `DRAWER_SORTER_USERNAMES`; previously resort only ran on the "Auto-sort" (no location) path — **shipped**
- v3.11.16: Death trigger synergy detection + collection/location CSV export — `extract_commander_themes` adds `death_triggers` mechanic when commander oracle contains `"dying"` or a `when(?:ever)?[^.;]*\bdies` pattern (catches Teysa, Erebos, etc.); `card_matches_theme` adds matching check so cards with "when/whenever X dies" triggers are classified Direct; token detection also triggers on `"tokens you control"` without requiring `"create"`; `_PANELS_CACHE_VERSION` bumped to 2 to invalidate stale synergy caches; `GET /collection/export` returns full user collection as CSV; `GET /locations/{id}/export` returns that location's cards as CSV; both use columns Name/Set/Collector Number/Finish/Quantity/Location; Export CSV buttons added to collection controls panel and location detail hero — **shipped**
- v3.11.13: Fix theme extraction for compound noun structures — `each` pattern now handles modifier words (`each non-Equipment artifact`); conjunction patterns now handle `and` as well as `or` (`artifact and non-Aura enchantment`); both fixes required for Bello-style oracle text — **shipped**
- v3.11.12: Fix theme extraction missing "X or Y" card types — add `{t} or \w+` and `\w+ or {t}` patterns so "enchantment or artifact" correctly detects both types — **shipped**
- v3.11.11: Commander theme extraction — `extract_commander_themes()` parses commander oracle text for card types cared about (positive pattern matching, removal context excluded), CMC gates (mana value N or greater/less), non-X subtype exclusions, mechanics (counters/tokens/graveyard/sacrifice/discard), and tribal subtypes (only when mentioned in oracle text); `card_matches_theme()` applies themes to classify deck cards; `compute_deck_synergy()` now uses these instead of ad-hoc subtype matching; "Detected:" note in synergy panel shows extracted signals — **shipped**
- v3.12.0: Dead card detection — `compute_dead_cards(all_rows, synergy)` in `deck_service.py` flags Unrelated cards (per synergy classification) that have no user-assigned role tag; oracle text patterns add sub-reasons: `win-more` (`for each creature/token/permanent you control`) and `board-dependent` (sacrifice a creature, tap untapped creatures, convoke); **Upgrade Targets** panel in `_deck_panels.html` shows count + expandable card list with sub-reason tags; CSS: `.dead-cards-panel`, `.dead-cards-note`, `.dead-cards-details`, `.dead-cards-summary`, `.dead-cards-list`, `.dead-card-name`, `.dead-card-tag` — **committed**
- v3.13: Average turn impact — estimate when cards are typically playable and when they matter; "deck peaks at turn X" summary
- v3.13.0: Game tracker — `Game` + `GameSeat` models; migration `v3_13_games`; `app/game_service.py` (`create_game`, `get_game`, `list_games`, `end_game`, `delete_game`, `get_deck_record`); routes `GET/POST /games`, `GET /games/new`, `GET/POST /games/{id}`, `POST /games/{id}/end`, `POST /games/{id}/delete`; `games.html` (history table with winner), `game_new.html` (JS seat builder, 2–8 players, deck selection), `game_detail.html` (client-side life tracker with ±1/±5 buttons, End Game form with per-seat placement + final life); W/L record shown in deck detail hero when games exist; Games nav link in `base.html`; CSS: `.life-tracker-grid`, `.life-card`, `.life-total`, `.life-controls`, `.life-btn`, `.end-game-row`, `.seat-row`, `.deck-record` — **shipped**
- v3.13.1: Full game tracker redesign — `game_detail.html` rebuilt as LifeTap-style app: ±1/±5/±10 life buttons, per-player commander damage matrix (collapsible `<details>`, auto-decrements receiver's life), poison (☣) and experience (⚡) counters with thresholds, turn counter + recent action history bar, undo (reverses last action including both sides of cmd damage), elimination toggle (☠ grays card), auto-win detection fills placements + opens End Game when 1 player alive, localStorage persistence keyed by `mana-game-{gameId}`, 8 per-player colors via CSS `--player-color`; `game_new.html` expanded to include all active users in seat dropdowns (not just current user) with deck list filtered per selected user via `decksByUser` JS map; seat 0 pre-populated with current user and their decks; auto-fills player name from display name; `main.py` `game_new_page` now fetches all active users + all decks grouped by `user_id`; CSS: `.tracker-grid`, `.tracker-card`, `.tc-life`, `.tc-eliminated`, `.tc-cmd-grid`, `.tc-counter-row`, `.turn-bar`, `.history-bar` — **shipped**
- v3.13.2: Tablet-optimized game tracker layout — full-screen fixed overlay covers nav; JS `LAYOUTS` config per player count (2–8) sets CSS `grid-template-areas` and applies seat rotations so bottom-row cards face near players (0°) and top-row cards face far players (180°); CSS mirrors `flex-direction` for rotated cards so +/- buttons read intuitively from both sides; floating `.game-topbar` pill at top center (turn, undo, End Game, ← Games); floating `.game-histbar` at bottom center; End Game is a `.end-game-modal` overlay; touch-friendly life buttons (`min-height: 48px`); life total scales via `clamp(2.5rem, 8vh, 7rem)` — **shipped**
- v3.13.4: Fix empty space in compass/triangle/long-table layouts — remove `.` placeholder cells; Triangle now `"top top" / "l r"`, Compass now `"top top" / "l r" / "bot bot"`, 5/7-player long tables restructured to sides-only (2+2+1 and 3+3+1) with no unassigned grid cells — **shipped**
- v3.13.5: Fix Compass corner gaps — side players (`l`/`r`) now span full height via repeated grid-area names (`"l top r" "l bot r"`); top/bot fill the center column between them; Triangle similarly collapsed to `"l top r"` single-row so all three cells are full-height — **shipped**
- v3.13.6: Fix rotated side-player cards not filling their grid cells — add `.card-slot` wrapper div as the actual grid item; `.tracker-card` is `position: absolute; inset: 0; margin: auto` inside it; JS `sizeRotatedCards()` sets `width = slotHeight; height = slotWidth` for 90°/270° cards so the card fills the cell correctly after CSS `rotate()`; resize listener re-runs on orientation change — **committed**
- v3.13.3: Playmat backgrounds, seat-orientation picker, 90°/270° rotations, touch polish — `.game-app` dark green felt background (radial gradient + dot-pattern texture); each player card has a per-color radial glow gradient on `#13180f` base; `LAYOUT_OPTIONS` per player count with 2 presets each: standard split and "Long Table" variant (2 side players at 90°/270°); `⊞ Layout` button in topbar opens `.layout-picker-popover` with named layout buttons; chosen layout saved to `mana-layout-N` localStorage key; CSS rules for 90° and 270° rotated cards mirror life-section flex-direction; counter buttons enlarged to 32px with `touch-action: manipulation` and `:active` scale feedback; life buttons `min-height: 52px` with press animation — **committed**
- v3.14.0: Fixed 8-seat topology + per-seat position picker — replaced dynamic layout presets with a fixed 3×3 grid (P1-P3 top row, P8 left end, P4 right end, P7-P5 bottom row, `tc` center); `GameSeat.grid_position` column (migration `v3_14_seat_position`) stores the assigned position; `game_new.html` gains a click-to-assign seat diagram showing the physical table topology — click a "Seat X" pill to enter assignment mode, click a position on the diagram to assign; default positions auto-populated per player count; `game_detail.html` reads stored `grid_position` from DB, falls back to default positions, then renders only the used seats with empty placeholder divs for unused cells; clockwise turn order badge (`.tc-seat-badge`) shows turn number; C/P/E counter pills (`.tc-cpill-row`); center `tc` cell shows turn counter + active player name + `→ Next` button; `POSITION_ROTATE` maps each position to 0°/90°/180°/270° so cards face the nearest player; CSS: fixed `grid-template-areas: "p1 p2 p3" / "p8 tc p4" / "p7 p6 p5"`, `.card-slot-center`, `.turn-center`, `.turn-center-round`, `.turn-center-name`, `.turn-next-btn`, `.tc-seat-badge`, `.tc-active-turn`, `.tc-cpill-row`, `.tc-cpill`, `.seat-pos-pill`, `.seat-diagram-wrap`, `.seat-diagram`, `.sd-top-row`, `.sd-mid-row`, `.sd-bot-row`, `.sd-seat`, `.sd-seat-filled`, `.sd-seat-pending`, `.sd-seat-inactive`, `.sd-center` — **shipped**
- v3.14.14: Expanded role-tag auto-detection from oracle text — patterns in `deck_service.py` now catch significantly more cards. RAMP: split into `_RAMP_LAND_RE` (anchored to "your library" so opponent-search effects like Demolition Field/Strip Mine/Ghost Quarter/Path to Exile don't trigger Ramp; basic-land subtypes added so Nature's Lore is detected) and `_RAMP_NON_LAND_RE` (mana abilities `add {`/`add one mana`/`add ... mana of any`, treasure tokens, cost reduction, additional land drops, mana doublers — gated to non-land cards). REMOVAL: now catches counterspells, bounce (`return target ... to owner's hand`), damage to target/any target, debuffs (`gets -X/-X`), fight effects, edicts, and land destruction. WIPE: adds bounce wipes (`return all ... to`), `-X/-X` variants, mass edicts (`each player sacrifices`), `each opponent` damage, and Overload-keyword cards. DRAW: adds impulse draw (`exile the top ... may cast/play`), wheel effects (`each player draws`), and reveal-and-put-into-hand patterns (Dark Confidant). PROTECTION: newly auto-detected — `you control` near hexproof/indestructible/shroud/protection from, `gains {keyword}`, damage prevention, `would die ... instead`, regenerate target. Trigger-condition guard: `_TRIGGER_DRAW_RE` strips `Whenever a player draws a card,` style trigger conditions before re-checking, so Sheoldred and Underworld Dreams aren't tagged Draw, but Mangara and Skullclamp (where the draw is the trigger consequence) still are. `compute_deck_health`/`compute_consistency`/`compute_deck_analytics` updated to use the new ramp pattern; all three already honor user role tags from v3.14.12. New tags Engine/Synergy/Threat/Hate stay user-only (too contextual). Auto-tag-on-deck-load (v3.9.8) only re-tags rows with NULL tags, so existing tagged rows aren't disturbed — but the suggestions list in the tag editor reflects the new patterns immediately — **committed**
- v3.14.13: Redesigned role tag taxonomy — `CARD_ROLE_TAGS` becomes `[Ramp, Draw, Tutor, Removal, Wipe, Protection, Engine, Synergy, Threat, Hate]`. Combo and Payoff are dropped; legacy values are silently filtered out by `get_row_tags()` (so existing rows persist Combo/Payoff in their JSON until re-saved, but never surface in UI or analytics). New tags: Engine (sac outlets, free-activation engines, strategy enablers — Carrion Feeder, Ashnod's Altar, Skullclamp); Synergy (cards that benefit from your strategy executing — replaces Payoff conceptually but narrower in spirit); Threat (win conditions and pressure pieces); Hate (meta disruption — Leyline of the Void, Bojuka Bog). `compute_deck_synergy` Direct bucket now keys on Synergy + Threat tags (was Combo + Payoff); Supporting bucket keys on Ramp/Draw/Removal/Wipe/Tutor/Protection/Engine/Hate. New tags are user-only (no `suggest_card_roles` auto-detection — Engine/Threat/Hate are too contextual to detect reliably). CSS: removed `.card-tag-combo`/`.card-tag-payoff`; added `.card-tag-engine` (#c46a20), `.card-tag-synergy` (#b04080), `.card-tag-threat` (#c43a2a), `.card-tag-hate` (#4a5060) — **committed**
- v3.14.12: Health/consistency/curve metrics honor user role tags — `compute_deck_health`, `compute_consistency`, and `compute_mana_curve` in `deck_service.py` now union `get_row_tags(row)` with the existing oracle-text regex matches. Previously a card manually tagged "Removal" wouldn't count toward the Health panel's removal threshold unless its oracle matched the strict `(destroy|exile) target ... (creature|artifact|enchantment|planeswalker|permanent)` pattern, missing counterspells, bounce, damage-based removal, edicts, debuffs, etc. Now the regex handles automatic detection and the user tag acts as a manual override (Ramp/Draw/Removal/Wipe in `compute_deck_health`; Ramp/Draw/Removal/Tutor in `compute_consistency`; Ramp in the curve split). Loop also no longer short-circuits on empty oracle so tag-only matches still work; basic lands are still skipped. Panels cache version unchanged (health/consistency/curve aren't cached) — **committed**
- v3.14.11: Centered player names + MTG auto-elimination — `.tc-names` (player name + deck label) moved out of `.tc-header` into `.tc-life-center`, sitting centered just above the life total. `.tc-header` now right-aligns its single child (elim button or placement badge). New `checkElimination(seatId)` helper auto-sets `state.eliminated[seatId]=true` when the seat hits any MTG loss condition: life ≤ 0, poison ≥ 10, or cmd damage from any single commander ≥ 21. Called from `adjustLife`, `adjustPoison`, `adjustCmd` after history push and before save/render. Auto-elim is one-way (dead stays dead per MTG rules); `toggleEliminate` still works for manual revive — **committed**
- v3.14.10: Fix end-seat (side player) over-constrained CSS positioning — base `.tracker-card` has `position:absolute; inset:0; margin:auto`, which when combined with JS-set explicit width/height creates an over-constrained layout that resolves differently across browsers (often as a thin sliver clipped to almost nothing). Override for 90°/270° rotated cards: `inset: auto; margin: 0; top: 50%; left: 50%; transform: translate(-50%, -50%) rotate(...)` — unambiguously centers the card at the slot center via translate, then rotates around that origin. Affects 3-, 5-, 6-, 7-, 8-player layouts (any with an endcap seat) — **committed**
- v3.14.9: Force synchronous reflow + 25%-wide tall side strips — `sizeRotatedCards()` was running only via `requestAnimationFrame` after `buildDynamicGrid()`, but for the tall-side rotated cards the slot's `offsetWidth/offsetHeight` could still be stale (or 0 on first paint), leaving the rotated card at default `inset:0` which produced an invisible thin sliver. Now: read `gridEl.offsetHeight` (forces a sync reflow), call `sizeRotatedCards()` synchronously, then schedule a second RAF call for any post-paint adjustments (fullscreen transitions, orientation changes). Bumped sides from 6/36 (~17%) to 9/36 (25%) so the side player is visually substantial — **committed**
- v3.14.8: Side seats are ALWAYS tall narrow strips — removed the conflict-check that was making side players "short" (middle-row only) when p1/p3/p5/p7 were also present; now `tallP4`/`tallP8` are simply truthy whenever the side seat exists. Top/bottom row players always live in the inner column range and coexist with the tall side strips. Switched grid from 24 cols to 36 cols so 1/2/3 inner-row players all divide cleanly with 6-col (~16.7%) sides. 7-/8-player layouts now have full-height sides; 3/5/6-player layouts now reliably show their right/left endcap player. Middle row is never used for cells now (TC remains floating). Existing games with stored positions still render correctly under the new logic without re-clearing — **committed**
- v3.14.7: Tall side seats + floating turn-counter overlay — `buildDynamicGrid()` upgraded from a 6-col to a 24-col template; right/left endcap players (p4/p8) now span ALL active rows as narrow ~17%-wide vertical strips when there's no edge-cell conflict (no p3/p5 for tall p4, no p1/p7 for tall p8); when a side is "short" (8-player layout) it stays in the middle row only. The `slot-center` is no longer a grid cell — it's a fixed-position floating overlay (`position: fixed; top:50%; left:50%; transform: translate(-50%,-50%)`) so the turn counter doesn't consume a quadrant. Middle row is omitted from `gridTemplateRows` entirely when no short side player needs it, freeing the freed vertical space for top/bottom players. 5-player default position map now `['p1','p2','p4','p6','p7']` (Seat 5 at bot-left so p4 can be tall on the right). 6-player default now `['p1','p2','p4','p6','p7','p8']` (both sides tall). All button backgrounds (`.tc-life-btn`, `.tc-cpill-btn`, `.tc-elim-btn`, `.tc-ctr-btn`, `.turn-next-btn`) now solid `#000` for contrast against the colored playmat. `.tracker-card` padding bumped from `0.75rem 1rem` to `1.25rem 1.5rem` so player labels and commander-damage rows sit further from the playmat edges — **committed**
- v3.14.6: Fix grid-area shorthand wiping out per-slot grid placement + uniform seat polish — `buildDynamicGrid()` was setting `slotEl.style.gridRow`/`gridColumn` and _then_ `slotEl.style.gridArea = ''`, but `gridArea` is the shorthand for both, so the trailing clear blanked the values just set; reordered to clear `gridArea` first in both the top/bottom-row loop and `setSlot()` (the TC `placeTc()` already had this order, which is why TC was the only slot that worked). Also: removed `flex-direction: row-reverse` from `.tc-header` for 180°/90° rotations so the elim/revive button stays at every player's top-right (kept on `.tc-life-section`/`.tc-life-btn-col` since those reverses keep + buttons on consistent sides per POV). Card backgrounds now use a stronger player-color tint (radial gradient at 38% center fading to a 20% player-color base instead of plain `#13180f`) so seats are easily identifiable from across the table — **committed**
- v3.14.5: Fix default seat positions — old `SEAT_POSITIONS`/`DEFAULT_POSITIONS` for 4–7 players used endcaps (`p4`/`p8`) which forced rotated side seats and left the middle row half-empty; new defaults seat players along the long sides (4 = `['p1','p2','p6','p5']`, 6 = `['p1','p2','p3','p7','p6','p5']`, etc.) so the top/bottom rows fill evenly and rotations stay at 0°/180°; updated in both `game_detail.html` (live tracker) and `game_new.html` (creation defaults). Existing games keep their stored `grid_position`; clear via `UPDATE game_seats SET grid_position = NULL WHERE game_id = N;` to apply new defaults — **committed**
- v3.14.4: Fix tc slot auto-placement bug — removed `style="grid-area:tc"` from center slot HTML (named area "tc" didn't exist in dynamic grid, causing auto-placement after player slots = turn counter stranded at bottom of screen); added `placeTc()` helper that always clears `gridArea` before assigning `gridRow`/`gridColumn` — **committed**
- v3.14.3: Game tracker layout polish — tracker grid cleared from topbar/histbar (`top: 42px; bottom: 38px`); commander damage scrollable within card (`overflow-y: auto`); P/E pills with inline ☣/⚡ label + −/+ buttons; pill row constrained to inner edges of life button columns via `.tc-life-center` wrapper; dynamic equal-size player mats via `buildDynamicGrid()` — 6-column grid, equal columns per seat, empty rows collapsed; portrait still uses 2-col auto-flow — **committed**
- v3.14.2: Game tracker fullscreen + portrait fix — launch overlay on active games with "⛶ Go Fullscreen" / "Continue without fullscreen" buttons (satisfies browser user-gesture requirement); `⛶/⊠` toggle button in topbar; `fullscreenchange` + `webkitfullscreenchange` listeners update button icon and re-run `applyLayout()`; fixed `isPortrait()` to use `window.matchMedia('(orientation: portrait)')` so JS matches CSS media query exactly; added `portraitMQ.addEventListener('change', applyLayout)` for reliable orientation-change detection — **shipped**
- v3.14.1: Portrait-mode responsive game tracker — `@media (orientation: portrait)` switches `.tracker-grid` to 2-column `grid-auto-flow` layout; JS `applyLayout()` replaces `initGrid` + `sizeRotatedCards` and re-runs on every resize, setting `gridArea='auto'` + `order` (clockwise sort) + `rotate=0` in portrait and restoring topology positions + rotations in landscape; empty placeholder cells hidden in portrait via `display:none`; portrait turn row (T{N} · player name · → button) added to topbar, hidden in landscape via CSS, synced by `render()` — **shipped**
- v4.0: PostgreSQL migration
- v4.1: Playgroup meta adjustment — track win/loss vs specific decks, common threats, avg game length; suggest curve/removal/hate adjustments
