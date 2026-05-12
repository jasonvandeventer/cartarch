![Mana Archive](app/static/icons/wordmark-512.png)

Self-hosted web application for managing a physical Magic: The Gathering collection.

**Current version: v3.16.6** · [Platform repo](https://github.com/jasonvandeventer/mana-archive-platform)

---

## Features

### Collection

- Browse and search your full inventory with Scryfall-style boolean syntax
- Keywords: `t:creature`, `c:WU`, `cmc:>3`, `o:"draw a card"`, `id:gb`, `price:>=5`, `is:foil`, `qty:>1`, and more
- Full boolean logic: `OR`, `AND`, `-negation`, `(grouping)`, quoted multi-word values
- Sort by name, type, mana cost, color, or price

### Imports

- **CSV upload** — auto-detects Scanner App, Helvault (free/pro), and Moxfield collection CSV formats
- **Paste list** — parses Moxfield deck exports, MTGA, MTGO, and standard `N CardName (SET) #` format; also accepts bare `SET COLLECTOR` lines (`MH3 145`, `MH3 145 2`, `2 MH3 145`, `*F*` for foil) so you can add cards by set + collector number alone
- Import directly to a deck or storage location at commit time
- **Inline create** — "+ Create new deck" / "+ Create new location" popouts on the import preview screen create the destination via JSON endpoints and pre-select it in the dropdown without leaving the wizard
- Import complete screen shows total cards imported + unique-row count, and a "Go to [destination]" button that links straight to the deck or location

### Decks

- Create and manage Commander (or any format) decks; edit name, format, and notes inline
- Mark commanders; commander cards appear in a dedicated panel above the deck grid
- Full Scryfall-style search within a deck
- **Analytics panel**: mana curve, card type breakdown, color pip counts, avg CMC, "deck peaks at turn X" insight
- **Health panel**: ramp/draw/removal/board-wipe density vs recommended thresholds; pip strain analysis (colored pip demand vs land color sources)
- **Token panel**: auto-discovers tokens produceable by the deck via Scryfall `all_parts`; click a token image to view its detail page
- **Synergy classification**: cards split into Direct / Supporting / Unrelated based on commander themes (death triggers, tokens, sacrifice, +1/+1 counters, tribal subtypes)
- **Win condition detection**: live integration with CommanderSpellbook to surface combos present in the deck
- **Bracket Estimator V2** (v3.15.0+): multi-stage pipeline producing a 1-5 bracket with explainable findings. Mechanics floor (Game Changer count via Scryfall `is:gamechanger`, mass land denial, extra-turn chains) + 5-question intent survey + combo role classification (none/incidental/backup/primary/compact via Commander Spellbook). Mechanics-vs-intent mismatch warnings; multi-dimensional confidence (tagging coverage, mechanics clarity, intent alignment, combo detection depth)
- **Role tag system** with 10 tags (Ramp, Draw, Tutor, Removal, Wipe, Protection, Engine, Synergy, Threat, Hate); auto-detected from oracle text and commander themes; **Retag** button re-runs detection over already-tagged rows additively
- Click any health metric count to filter the deck grid to just those cards

### Organization

- Drawer/slot system for physical organization (gated per-user)
- Custom storage locations: create, edit (name/type/parent/sort order), and delete
- Move cards between locations from the location detail page or deck detail page
- **Bulk move**: select multiple cards from a location or deck and move them in one action; destination picker includes both storage locations and other decks; drawer-sorter users get a "Return to Sorter" option that bulk-returns rows to pending and triggers auto-placement
- Return cards from decks to pending/collection
- **CSV export**: download your full collection or any individual location as a CSV (Name, Set, Collector Number, Finish, Quantity, Location)

### Pricing & Card Data

- Live Scryfall pricing (USD regular, foil, etched) per card and deck totals
- Background price refresh loop keeps data fresh
- Card attributes: colors, color identity, mana cost, CMC, oracle text, type line

### Multi-user

- **Self-service registration** — users sign up with email + display name; no admin involvement required
- **Update Profile form** at `/account` lets any user change their email and display name without admin DB access
- Admin panel: create/delete users, toggle admin/active, reset passwords
- Display names shown throughout the UI; email used as login identifier
- Per-user data isolation; drawer sorter is opt-in per username

### Game Tracker

- Log Commander games: format, starting life total, 2–8 players with optional user + deck linkage
- **Full in-browser life tracking**: ±1/±5/±10 life buttons, per-player color coding
- **Commander damage matrix**: track damage dealt per commander, auto-adjusts receiver's life total
- **Poison and experience counters** with danger/warning thresholds
- **Turn counter** and recent action history bar
- **Undo**: reverses last action (including both sides of commander damage)
- **Elimination toggle**: mark players as eliminated; auto-detects winner when 1 player remains
- State persisted to `localStorage` — survives page refresh mid-game
- End Game records placements, final life totals, and turn count; W/L record shown on each deck's detail page

### Tokens

- **Lightweight token catalog** at `/tokens` — track physical tokens you own (Pest x12, Treasure x30, etc.) separate from card inventory
- **Scryfall integration** on the new-token form: live name autocomplete, "Look up exact (set + collector)" button (auto-tries the `t`-prefix for token sets — `BIG #0006` resolves to the Golem in `tbig`), and "Search by name" picker that returns multiple matches as a visual image grid for disambiguation; DFC tokens show both faces side-by-side in the picker
- **Storage location reuse** — tokens go in any StorageLocation but are excluded from drawer-sorter automation
- **Double-sided token support** — real DFC tokens (Goblin // Treasure) auto-detected via Scryfall's `card_faces`; for sets where Scryfall stores each face as a separate single-sided record (TMH3 etc.), the "Double-sided" checkbox reveals a Back face fieldset with its own set + collector + "Look up back" button, supporting cross-set pairings (TBLB front, TBLC back)
- **Bulk add** at `/tokens/bulk-add` — paste a list of tokens; field count per line picks the type (2 = single, 3 = single+qty, 4 = DFC, 5 = DFC+qty). Per-row Scryfall lookups create the inventory rows
- **Deck Tokens-Needed** table on each deck detail page: declare what the deck needs (Pest x10, Food x8) and see Owned / Missing status pulled from your token inventory

### Sets

- Browse cards by set; token panel renders by default and includes substitute cards (`s{set_code}` like SZNR) appended after regular tokens
- Owned/Missing badges on every token tile sourced from your token inventory by `(set_code, collector_number)` match

### Mobile

- Full mobile responsiveness across every page except the live game tracker (which is intentionally tablet-landscape-first)
- Below 768px the top-bar nav collapses to a 5-tab bottom bar (Home / Collection / Decks / Games / More) with a "More" overlay containing Import, Pending, Locations, Tokens, Sets, Drawers/Audit/Admin (gated), Account, Logout
- "More" tab shows a red badge with the user's pending-placement count (capped at `99+`)
- 44px tap-target floor enforced on phone/tablet-portrait; tracker buttons exempt by design
- Tables scroll horizontally inside their panels; popouts (Edit, Bracket, inline-create) become viewport-centered modals on phones with semi-transparent backdrop, body scroll lock, auto-injected × close button, and dismiss on backdrop tap / Escape / × — see [docs/mobile_patterns.md](docs/mobile_patterns.md)
- Mobile fundamentals applied globally: 16px input font-size (prevents iOS auto-zoom on focus), `overflow-x: hidden` below 768px (page never horizontal-scrolls; tables still scroll internally), `box-sizing: border-box` on every element including pseudo-elements, `viewport-fit=cover` + `env(safe-area-inset-bottom)` for notched devices, `overflow-wrap: anywhere` on text containers, and a `min-height: 44px` floor on link-styled tap targets in nav, filter, hero, and pagination surfaces

---

## Stack

| Layer         | Technology                                    |
| ------------- | --------------------------------------------- |
| Web framework | FastAPI + Jinja2                              |
| Database      | SQLite (via SQLAlchemy)                       |
| Styling       | Custom CSS (no framework)                     |
| Card data     | [Scryfall API](https://scryfall.com/docs/api) |
| Runtime       | Docker / Kubernetes (K3s)                     |
| GitOps        | ArgoCD + ArgoCD Image Updater                 |

---

## Architecture

This repo contains **application code only**. Platform/infrastructure lives separately:

- **App repo** — this repo (FastAPI app, templates, migrations)
- **Platform repo** — [mana-archive-platform](https://github.com/jasonvandeventer/mana-archive-platform) (Kubernetes manifests, ArgoCD config)

CI builds and pushes a Docker image to GHCR on any `v*.*.*` tag. ArgoCD Image Updater detects the new tag (semver strategy) and syncs the cluster automatically.

---

## Local Development

```bash
docker compose -f docker-compose.dev.yml up --build
```

App available at `http://localhost:8000`.

### Git hooks

After cloning, activate the pre-commit lint check and post-commit auto-tagger:

```bash
git config core.hooksPath .githooks
```

The post-commit hook tags HEAD automatically whenever the commit message starts with `vX.Y.Z:`.

### Migrations

Migrations run automatically on startup via `run_migrations()` in `on_startup()`. To add a migration, drop an idempotent script in `scripts/` and register it in `scripts/run_migrations.py`.

---

## Data Storage

- **Local**: SQLite file in `/data`
- **Kubernetes**: Longhorn persistent volume mounted at `/data`

No database files are stored in this repository.
