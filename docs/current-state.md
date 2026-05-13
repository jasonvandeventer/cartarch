# Current State

Snapshot of what's shipped and how the app is used today. For the prioritized backlog, see [ROADMAP.md](ROADMAP.md).

## What Mana Archive Is

A self-hosted web application for managing a physical Magic: The Gathering collection, deck-building against that collection, and tracking Commander games. Used by a small private playgroup (7-10 users). Not public-facing; no signup funnel; no SaaS plans.

Current version: **v3.16.23**.

## Stack

| Layer         | Technology                                          |
| ------------- | --------------------------------------------------- |
| Web framework | FastAPI + Jinja2 templates                          |
| Database      | SQLite via SQLAlchemy (per-user data isolation)     |
| Front-end     | Server-rendered HTML, HTMX 1.9.12 (self-hosted), vanilla JS |
| Styling       | Hand-written CSS, no framework                      |
| Card data     | Scryfall API (cached locally; bulk-fetched in batches) |
| Combo data    | Commander Spellbook API (cached in-memory per deck) |

InventoryRow is the single source of truth for owned cards. StorageLocation is the canonical location model — decks are themselves `type="deck"` storage locations referenced by a Deck record. SQLite is the permanent choice (see ROADMAP "Explicitly NOT Planned").

## Shipped Features

### Collection

- Full inventory with Scryfall-syntax search: `t:`, `c:`, `cmc:`, `o:`, `id:`, `price:`, `is:foil`, `qty:`, `legal:`, `banned:`, boolean operators (`OR`, `AND`, `-negation`, parentheses, quoted multi-word values).
- Sort by name, type, color (WUBRG), mana cost, or price.
- CSV export of full collection or individual locations.
- Custom storage locations (drawer, binder, box, other), editable, with parent/sort-order.
- Optional drawer-sorter auto-placement gated per-user via `DRAWER_SORTER_USERNAMES`.
- Move cards between locations (manual + bulk) from collection, location detail, and deck pages.

### Decks

- Create/edit Commander or any-format decks; inline edit of name, format, notes.
- Commander role tagging; commanders render in a dedicated panel above the deck grid.
- Full Scryfall-syntax search within a deck via HTMX (in-place card-grid swap, URL-shareable).
- Analytics: mana curve (stacked ramp/spells, peak turn), card types, color pips, average CMC, dead-hand risk.
- Health panel: ramp/draw/removal/wipe density vs thresholds, pip strain analysis, link-to-filter from each metric.
- Synergy classification (Direct/Supporting/Unrelated) driven by commander theme extraction.
- Bracket Estimate V2 with 5-question intent survey, multi-dimensional confidence bars, and combo-role detection (none/incidental/backup/primary/compact via Commander Spellbook).
- Win Conditions panel: complete combos detected in deck via Commander Spellbook.
- Dead-card / upgrade-target panel for cards classified as Unrelated by synergy.
- Role-tag editor (10 tags: Ramp/Draw/Tutor/Removal/Wipe/Protection/Engine/Synergy/Threat/Hate); Retag button re-runs auto-detection additively.
- Mobile-friendly "Add card to this deck" panel with Scryfall autocomplete (50 printings per name), single-card reconciliation (move owned → deck vs import new).
- Deck Tokens Needed table.
- Plain-text deck export in standard `N CardName (SET) #` format.

### Imports

- **CSV** — auto-detects Scanner App, Helvault (free & pro), and Moxfield collection CSV formats.
- **Paste list** — Moxfield deck exports, MTGA, MTGO, standard `N CardName (SET) #collector` format, and bare `SET COLLECTOR` lines (e.g. `MH3 145`).
- **Manual entry** — single-card by Scryfall ID or set+collector, or via Scryfall name search returning every printing through pagination (up to 500 results per query).
- **Deck-import reconciliation** — when destination is a deck, the preview shows a per-row panel: move existing copies vs import new; auto-merges into existing deck rows; surfaces target-deck and other-deck breakdowns.
- **Collection-import reconciliation** — when destination is a drawer/binder/box/auto-sort, the preview shows skip/delta/new actions per row; deck-located duplicates auto-expand the per-card panel since deck copies are allocated and shouldn't silently auto-skip.
- **Inline create** — "+ Create new deck/location" popouts on the import preview create the destination via JSON endpoints and pre-select it.
- **Import-result page** — surfaces cards imported / moved / merged / skipped counts, stale-match warnings for concurrent edits, and a type-aware "View deck" / "View location" / "View pending" button.

### Game Tracker

- Log Commander games (format, starting life, 2-8 players, optional user + deck linkage).
- Full in-browser life tracking: ±1/±5/±10 buttons, per-player color coding.
- Commander damage matrix (auto-adjusts receiver's life).
- Poison and experience counters with thresholds.
- Turn counter, action history bar, undo (reverses both sides of commander damage).
- Auto-elimination on life ≤ 0, poison ≥ 10, or commander damage ≥ 21 from any single source.
- Fixed 8-seat 3×3 topology with per-seat position picker; card rotations applied so cards face each seat's player.
- Playmat backgrounds, per-player color glow, fullscreen mode.
- End Game records placements + final life + turn count; W/L record shown on each deck.
- State persists to `localStorage` keyed by game ID.

### Tokens

- Separate inventory catalog (`token_inventory` table, distinct from card `InventoryRow`).
- Scryfall integration on the new-token form: live autocomplete, exact lookup, name search with visual disambiguation, DFC support including cross-set pairings.
- Bulk-add page with paste-list shape detection (2/3/4/5 fields per line).
- Per-deck Tokens-Needed table with owned/missing status.
- Set-detail token panel with completion percentage including substitute cards.

### Multi-user and Account

- Self-service registration (email + display name).
- Per-user data isolation across all tables.
- Admin panel: create/delete users, toggle active/admin, reset passwords.
- Account page: change email, display name, password.
- Drawer-sorter access gated per-user via a frozen username set.

### Mobile

- Full responsiveness across every page except the live game tracker (tablet-landscape-first by design).
- Below 768px: 5-tab bottom nav (Home / Collection / Decks / Games / More); pending-count badge on the More tab.
- 44px tap-target floor on phone/tablet-portrait.
- Stacking-table pattern for six-column tables below 480px.
- Popovers become viewport-centered modals on phones with backdrop, body-scroll-lock, Escape/× dismissal.
- 16px input font-size (prevents iOS auto-zoom), `overflow-x: hidden`, `viewport-fit=cover`, `env(safe-area-inset-bottom)`.

## Data Storage

- **Local development**: SQLite file under `./dev-data/`.
- **Production**: SQLite file on a Longhorn-backed persistent volume mounted at `/data` in the cluster.
- Migrations run on startup via `run_migrations()` (idempotent, tracked in `schema_migrations`).
- No database files in this repository.

## Deployment

Application code lives here; cluster manifests live in the separate platform repo. CI builds and pushes to GHCR on any `v*.*.*` tag (auto-tagged by `.githooks/post-commit` when the commit message starts with `vX.Y.Z:`). ArgoCD Image Updater (semver strategy) detects the new tag and syncs the cluster automatically.

Operational concerns (cluster topology, storage classes, backup runbooks, monitoring) are intentionally out of scope for this document — they live in the platform repo where they can be maintained alongside the manifests.

## Active Usage Context

Single primary admin user plus 7-10 trusted playgroup members. Real workloads observed:

- Bulk inventory imports (CSV from Helvault/Moxfield) once or twice per user, then incremental adds via manual entry and paste-list.
- Active deck-building against owned inventory; reconciliation routinely moves owned copies into decks rather than duplicating.
- Live game tracking on a tablet during weekly playgroup nights, with placements logged at end-of-game.
- Mobile usage for browsing collection, building decks during downtime, and adding cards via the autocomplete panel.

This usage shape drives what gets prioritized; see [ROADMAP.md](ROADMAP.md) for the active backlog.
