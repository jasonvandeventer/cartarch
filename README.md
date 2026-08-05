![Cartarch](app/static/brand/logo/cartarch-horizontal.svg)

Self-hosted web application for managing a physical Magic: The Gathering collection. (Identifies as **Cartarch** in user-facing UI as of v3.27.6; the in-repo project identifier was aligned to `cartarch` app-side on 2026-07-06. Some infrastructure names may still carry the old identifier pending the full rename near actual public launch.)

**Current version: v4.13.7** · [Platform repo](https://github.com/jasonvandeventer/vanfreckle-platform)

---

## Engineering

This is a production system, not a tutorial. It runs on a personal Kubernetes cluster serving real users, with the operational discipline that implies.

- **1,390+ automated tests** with a CI release-record guard that blocks deployments from unverified commits
- **Full CI/CD release pipeline**: git tag → GitHub Actions build → GHCR publish → ArgoCD Image Updater promotion → PreSync Alembic migration hook → additive migrate-before-deploy discipline → post-deploy verify/soak job
- **CloudNativePG PostgreSQL** with WAL archiving to R2 object storage; backup chain verified through completed full-restore drills
- **Blue/green cluster migration** from k3s to Talos with simultaneous SQLite-to-PostgreSQL data migration, zero data loss, and minimal planned downtime (write-freeze + tunnel repoint)
- **Production incident response**: root-caused and structurally prevented a pod-availability incident (synchronous I/O blocking the async handler under a single-writer DB transaction); postmortem documented
- **Auth hardening**: OAuth-gated service exposure behind Cloudflare Access, timing-oracle-resistant registration/reset flows, NIST SP 800-63B-aligned password policy
- **Self-hosted image mirror** (525,000+ card images) with daily automated price data ingest

Platform infrastructure: [vanfreckle-platform](https://github.com/jasonvandeventer/vanfreckle-platform)

---

## North Star

Cartarch is the source of truth for the playgroup. Authoritative data about who owns what, what's in which deck, and how decks have performed in our games lives here. External services (Scryfall, Commander Spellbook, EDHREC) are integrated as enrichment for that data, not as replacements.

Practical implications:

- Recommendation features ground their suggestions in the user's collection first, the playgroup's data second, and external aggregators last.
- Analytics compare a deck against the user's other decks and the playgroup's game history, not against community averages.
- External service data appears as inline enrichment (per-card hover, combo detection, inclusion percentages) rather than as primary navigation surfaces.
- Features that would route users away from Cartarch's data toward an aggregator's data are scrutinized closely. Enrichment is welcome; replacement is not.

See [roadmap.md](roadmap.md) for the prioritized backlog.

---

## Screenshots

![Deck detail page](docs/screenshots/deck-detail.jpg)

_Deck detail — hero with deck stats, Analytics (mana curve, card types, color pips), Health (functional density + pip strain), Synergy classification, dead-cards / upgrade targets, and tokens panels._

![Collection with boolean search](docs/screenshots/collection.jpg)

_Collection page with Scryfall-syntax search applied (`t:creature c:WU cmc:<=3`)._

![Live game tracker](docs/screenshots/game-tracker.jpg)

_Live game tracker — tablet-oriented life/poison/commander-damage UI with per-seat rotations._

See [docs/screenshots/](docs/screenshots/) for capture guidelines and additional shots.

---

## Features

### Public surface

- **Landing page** at `/` for anonymous visitors — separate marketing surface (no app shell, no sidebar). Renders the hero ("The ruler of your collection"), four feature highlights, a 3-tile screenshot showcase, beta-interest + support + changelog cards, and a footer with Privacy / Terms / Contact links. Open Graph + Twitter meta tags make shared links render a branded card. Authenticated users at `/` get the existing dashboard unchanged — the route branches on auth state via a new `get_optional_current_user` dependency
- **Brand assets** live at `app/static/brand/` — favicon set (`.ico` + vector `.svg` + apple-touch-icon + PWA 192/512/maskable), 19 SVG logo variants, OG image. PWA-installable via `app/static/manifest.json` with `theme_color`/`background_color="#081321"`
- **`/privacy` + `/terms`** are placeholder stubs today so the landing-page footer links aren't dead; final policy text replaces them before public launch

### Dashboard

- Left-sidebar app shell with grouped nav (Overview / Collection / Storage / Play / System). Mobile (<768px): sidebar hides, the existing bottom-tab bar takes over
- **At a Glance** tile row on the populated dashboard surfaces existing data:
  - **Collection Value** — `SUM(quantity × finish-aware price)` over placed inventory; pending shown as an explicit sub-stat (placed-only headline canon — pending is never folded silently into the number)
  - **Decks Owned** — total deck count + Commander-format breakdown
  - **Recent Activity** — last 8 TransactionLog rows with card name, event type, date
- All three tiles share the same canonical unit: card-count = `SUM(InventoryRow.quantity)` everywhere (Collection / Drawers / Decks / dashboard tiles all reconcile). Drawers page reports cards, not rows, as a consequence (a drawer holding 3 rows of a 4-of reads 12 cards, not 3 rows)
- **Quick Actions** grid below the tiles for first-class flows (Pending, Import, Collection, Drawers/Locations, Decks, Games)
- Empty-state for brand-new accounts: welcome + first-step CTA + numbered next-steps replaces the wall-of-zeros that the populated dashboard would otherwise show

### Collection

- Browse and search your full inventory with Scryfall-style boolean syntax
- Keywords: `t:creature`, `c:WU`, `cmc:>3`, `o:"draw a card"`, `id:gb`, `price:>=5`, `is:foil`, `qty:>1`, and more
- Full boolean logic: `OR`, `AND`, `-negation`, `(grouping)`, quoted multi-word values
- Sort by name, type, mana value, color, set, rarity, price, quantity available, or **owned count** (count-sorted view groups all printings of a high-count name together — three-level grouping name → printing → location)
- **Shared sort control (v3.36.11)**: the same Sort dropdown (field + asc/desc) appears on every card-listing surface — Collection, Decks, Locations, Showcases, and shared views — backed by one source of truth (`app/sort_spec.py`); it composes with the active search/filters and isn't stored as a preference

### Decklist Check

- Paste a decklist at `/decklist` (Moxfield / MTGA / MTGO format); see what's already in your collection, what's missing, and where each owned copy lives
- Four buckets: **Have it** (owned ≥ wanted), **Partial** (own some but fewer than wanted; shows shortfall), **Missing** (own none), **Basic lands** (set aside separately — basic-land counts aren't a meaningful trade question)
- Each Have / Partial result shows the per-printing locations, sorted **tradeable-first**: copies in `managed`/`sink` locations (loose, sortable) surface ahead of copies in `manual` locations (decks, display cases, sentimental boxes) so you see actionable inventory before "would-have-to-break-something" inventory
- Reuses the import flow's paste parser — a list that imports cleanly via `/import` matches cleanly here
- Local-only matching — pasted card names that don't match your inventory become Missing results; never falls back to a Scryfall lookup
- Stateless (no saved wantlists in v1)

### Imports

- **CSV upload** — auto-detects Scanner App, Helvault (free/pro), and Moxfield collection CSV formats
- **Paste list** — parses Moxfield deck exports, MTGA, MTGO, and standard `N CardName (SET) #` format; name-only lines with a quantity (`1 Mizzix of the Izmagnus`) resolve to the default printing, or add a `(SET)` to pin one; also accepts bare `SET COLLECTOR` lines (`MH3 145`, `MH3 145 2`, `2 MH3 145`, `*F*` for foil) so you can add cards by set + collector number alone. All lookups are batched (resolves a whole pasted list in a fixed number of requests, never one per line)
- Import directly to a deck or storage location at commit time
- **Inline create** — "+ Create new deck" / "+ Create new location" popouts on the import preview screen create the destination via JSON endpoints and pre-select it in the dropdown without leaving the wizard
- Import complete screen shows total cards imported + unique-row count, and a "Go to [destination]" button that links straight to the deck or location
- **Import-to-deck reconciliation** — when the destination is a deck, the preview page shows a reconciliation panel: cards already in your collection (drawer, binder, box, pending) are _moved_ into the deck instead of duplicated; new copies are imported only for cards you don't already own. Per-row override available behind a "Review individually" expand. Stale-match fallback: if inventory changes between preview and commit, the affected quantity is re-imported and surfaced as a warning on the import-result screen
- **Cards already in a deck are surfaced too** — copies in the destination deck show as "Already in this deck: N — import will merge into the existing row" (singleton-correct: a Commander deck never ends up with two rows for the same printing). Copies in OTHER decks show as informational ("In another deck: N in [deck name]") without auto-cannibalizing the source deck

### Decks

- Create and manage Commander (or any format) decks; edit name, format, and notes inline
- **Add card panel** on deck detail — type a card name, pick a printing from the Scryfall autocomplete, click Add. If you already own the card it's moved from your collection automatically; otherwise a new copy is imported. Mobile-first single-column layout
- Mark commanders; commander cards appear in a dedicated panel above the deck grid
- Full Scryfall-style search within a deck (HTMX-powered partial update: clicking Apply / pressing Enter swaps just the card grid in place, scroll position preserved, address bar updates for shareable URLs; no-JS fallback to the full-page GET form is preserved)
- **Analytics panel**: mana curve, card type breakdown, color pip counts, avg CMC
- **Health panel**: ramp/draw/removal/board-wipe density vs recommended thresholds; pip strain analysis (colored pip demand vs land color sources); consistency score
- **Synergy classification**: cards split into Direct / Supporting / Unrelated based on commander themes (death triggers, tokens, sacrifice, +1/+1 counters, tribal subtypes)
- **Dead-cards / upgrade-targets panel**: surfaces unrelated cards as replacement candidates
- **Token panel**: auto-discovers tokens produceable by the deck via Scryfall `all_parts`; click a token image to view its detail page
- **Role tag system** with 10 tags (Ramp, Draw, Tutor, Removal, Wipe, Protection, Engine, Synergy, Threat, Hate); auto-detected from oracle text and commander themes with per-tag source + confidence tracking (auto/medium vs user/high vs auto/certain); **Retag** button re-runs detection over already-tagged rows additively; **Review tags** panel on deck detail surfaces auto/medium suggestions for one-click confirm or remove
- Click any health metric count to filter the deck grid to just those cards
- **Bracket estimate** per deck at `/decks/{id}/bracket`, backed by `bracket_v2_service` with Commander Spellbook combo detection. Decks carry a bracket chip on the Decks page; declaring a bracket below the computed floor is flagged rather than silently accepted, and a deck that changed since its last evaluation re-estimates automatically
- **Brew mode** — a deck built from cards you may not own. Unowned cards become proxy rows inside the deck, so the list is complete while your collection totals stay honest; a buy-list splits what you have, what you're missing, and what you already own in another deck
- **Collection-aware brew generator** at `/recommendations/commander` — pick an owned commander and it assembles a legal 100-card deck from cards you own, with a stated reason per card. Deterministic and entirely offline: every signal is a column already stored, so no external call happens on the request path
- **Variant groups** link builds of the same deck that share one physical copy of many cards. Both builds render the full shared decklist with shared cards badged, without duplicating a single inventory row

### Sessions and the benched-deck house rule

- **A session is one evening at one table**, and it belongs to a **playgroup, not a date**. Two different meetups on the same day are two sessions; a game played outside the group is in neither. Sessions end when a member ends them, never on a timer
- **Win a game and that deck is benched for the rest of the session** — the picker labels a benched deck rather than blocking it, because the rule belongs to the table, and a night it gets waived should still be recorded honestly. Bench state is computed from the games themselves, never stored, so correcting a mis-recorded result corrects the bench
- **Decks carry a session record beside the game record** — sessions won over sessions played. The house rule caps game win rate structurally (a deck can win at most once per session, and the decks playing most games per night are the ones that keep losing), so for a deck that keeps winning the game figure is a floor rather than a measurement

### Play profiles

- **"How to pilot this deck"** on every deck page — the deck's plan, what it protects, what it spends freely, and what it fetches first when a tutor resolves. The AI deck simulator plays each deck from exactly these notes, so they are the input to its strength ratings, not a description of them
- Profiles ship with the app and reseed at every deploy, but **an edited profile is never overwritten** — saving marks it as yours
- A profile Cartarch inferred rather than a pilot writing it is **badged as unreviewed**, because an unverified plan and a stated one are not the same evidence

### Watchlist

- Per-user list of cards to track (acquire later / compare prices on / remember)
- **Two identity modes per row** — watch a specific printing (`card_id` FK to `cards.id` — useful for collectors after a particular promo or set version) OR a card name (printing-agnostic — matches the "I want a Sol Ring" mental model). Exactly one identity mode populated per row; both can be active independently for the same card
- Add a card to the watchlist from any card detail page (`/cards/{id}`); four button states show whether you're already watching this printing and/or any printing of the same name
- `/watchlist` page shows every watched card with Card / Watch type / Owned / Added / Note columns. Owned count splits placed and pending (printing-specific watches show the printing's count; name watches aggregate across all printings)
- Optional note per row for context ("for the Bello deck", "$3 target", etc.); inline edit via a popout

### Organization

- **Re-file & number** any box or binder — sorts it by set and collector number and writes a slot on every card, so a bulk box is walkable on the shelf the way the drawers are. Explicitly a button, never an import side effect: renumbering shifts every card after the insertion point, so it waits until you're ready to physically re-file
- Drawers and boxes share **one** definition of set-and-collector order, so a re-filed box and a sorted drawer are filed the same way

- Drawer/slot system for physical organization — activates for any user who creates a sorter rule or a drawer location
- Custom storage locations: create, edit (name/type/parent/sort order), and delete
- Move cards between locations from the location detail page or deck detail page
- **Bulk move**: select multiple cards from a location or deck and move them in one action; destination picker includes both storage locations and other decks; drawer-sorter users get a "Return to Sorter" option that bulk-returns rows to pending and triggers auto-placement
- **Drawer-vs-Bulk routing (v3.38.0)** _(drawer-sorter users)_: cheap, non-staple surplus is kept out of prime drawer slots and routed to a **Bulk** location. One predicate protects basics, anything you run in a deck, and anything worth more than $1 (threshold configurable); everything else, once you already keep one findable copy in the drawers, routes its extras to Bulk. Applied two ways from the same logic: a retroactive **"cull cheap drawer dupes to Bulk"** action on the Collection page (with a confirmation screen showing exactly what moves; all drawers or just one), and **automatic routing at import time** on the auto-sort path (the reconcile preview shows "N → drawers · M → Bulk" before you commit). The hand-tuned drawer sorter is untouched — routing is a new step in front of it. The Bulk location must be `manual` mode so the sorter doesn't re-absorb it
- Return cards from decks to pending/collection
- **CSV export**: download your collection or any individual location as a CSV. The collection export honors the active filter (search, facets, location, price range) so you get exactly the rows you're viewing — not always the whole collection — and includes a finish-aware Price column plus a Scryfall ID join key (columns: Name, Set, Collector Number, Finish, Quantity, Location, Location Type, Language, Role, Tags, Is Proxy, Scryfall ID, Price)

### Pricing & Card Data

- **Daily MTGJSON price ingest** (USD regular, foil, etched) per card and deck totals — one row per printing+finish, with per-provider retail prices resolved in a fixed order and a manual per-card override. Scryfall is still the source for card *metadata*; it no longer writes prices, so a metadata refresh can't clobber them
- A transient provider miss keeps the last known number rather than blanking it, so staleness shows as an old price instead of a missing one
- Card attributes: colors, color identity, mana cost, CMC, oracle text, type line

### Multi-user

- **Self-service registration** — users sign up with email + display name; no admin involvement required. `POST /register` returns a byte-identical response (same 303 + `Location: /login` + body) for both fresh-email and duplicate-email submissions; the duplicate path runs an equivalent-cost throwaway `hash_password()` so a side-channel timing oracle can't distinguish the two paths
- **Self-service password recovery** at `/forgot-password` — email-driven reset via Resend (the project's outbound transactional email path). Tokens are SHA-256 hashed at rest (raw token only ever in the emailed link), 30-minute expiry, single-use, invalidate-on-new-request. POST `/forgot-password` returns an identical neutral response for registered vs unregistered emails; the send is asynchronous via a daemon thread so there's no timing leak. Rate-limited per-email AND per-IP at 5 requests/hour
- **Update Profile form** at `/account` lets any user change their email and display name without admin DB access
- **Shared password strength validator** — 8-char minimum, 256-char maximum, no composition requirements (NIST SP 800-63B aligned); applied at `/register`, `/account/change-password`, AND `/reset-password` so the three password-set paths can't drift
- Admin panel: create/delete users, toggle admin/active, reset passwords
- Display names shown throughout the UI; email used as login identifier
- Per-user data isolation; the drawer sorter is opt-in **by setup, not by account** — it activates once a user has at least one sorter rule or one drawer location, so nobody is on a hard-coded list

### API

- **Read-only JSON API** at `/api/v1` for scripts, spreadsheets and bots. Generate a token on `/account` and send it as `Authorization: Bearer <token>`; four endpoints cover identity (`/me`), your collection (`/collection`), your decks (`/decks`) and one deck's card list (`/decks/{id}`). Full reference at `/docs`
- The collection endpoint accepts the **same `?search=` grammar the site's search box uses**, plus the colour/type/status/price facets — so a bot answering "do I own a Rhystic Study?" fetches one card rather than the whole collection
- `/api/v1/decks/{id}` names the deck's commanders from the app's one shared definition, so a deck that records its commander without tagging a card in the list still reports it. The plain-text export deliberately does not — that one is a round-trippable list of cards you own
- Owner-scoped and read-only by construction: a token reaches only its own user's data, nothing can be written through it, and another user's deck id is indistinguishable from one that does not exist
- The token *is* the toggle, like the public deck and wishlist links: the API is off until you generate one, revoking clears it, and regenerating invalidates the old one immediately

### Playgroups, sharing & trading

- **Playgroups** are the social unit: join by code, and membership is what scopes the people picker, shared games, shared showcases and shared wishlists. A user belongs to many
- **Showcases mirror a location live** — point one at your trade box and it stays in step: file a card there and it appears, move one out and it goes. No snapshot, no sync, nothing to re-run. Hand-picked cards coexist with mirrored ones and a card in both shows once; stop mirroring and its cards leave at once, because nothing was ever copied
- **Showcases** — curate a subset of your inventory (a whole location, or the whole collection) and expose it read-only to one playgroup. A showcase item *references* an inventory row rather than copying it, so quantities and prices stay live. The read-only view is a sanitised projection: no notes, no storage locations, no ownership internals
- **Wishlist sharing**, two ways and both names-only: an unguessable public link for anyone, or shared to a playgroup so co-members see it alongside theirs. Prices, targets and ownership counts are deliberately hidden — it reads as a gift registry. A logged-in viewer additionally sees which of the cards *they* already own
- **Trades** are recorded, not brokered: propose from a co-member's showcase or from their wishlist, pick cards from both sides, and the running balance totals each side live. Proposing from a wishlist seeds the *offered* side, so there is no such thing as a one-sided gift trade. Cards already offered to someone show a `[pending]` badge on their wishlist — aggregate only, never who proposed it
- **Public deck links** — publish any deck read-only to an unguessable URL. The token *is* the toggle: revoke by clearing it. The public page is a whitelisted projection carrying name, format, commanders and the card list, with no price, ownership, proxy or tag data
- **Decklist check** at `/decklist` — paste a list and see, per card, whether you own it and where it is, sorted so loose copies surface ahead of ones you'd have to break a deck to get

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

**Companion mode — the table runs on a tablet, everyone else uses their phone**

- **Go Live** promotes a game to a server-authoritative live state streamed over SSE, so the tablet and every phone see the same board. Games never taken live keep the offline `localStorage` tracker unchanged
- **Players claim their own seat before the game starts**, by scanning a QR on the tablet or typing the short code — and playgroup members skip the code entirely, since a linked game with a free seat is listed on their phone. Claiming attaches the seat to their account, so results are attributed correctly however they spelled their name that night
- **Typing a commander is enough**: the name resolves to that player's existing deck for that commander, or creates a placeholder that anchors the game's record. An unrecognised name still claims the seat and reports back rather than failing the join
- A seated player controls **their own** seat from their phone — life, counters, their deck — while the tablet keeps override authority over every seat. Ending your turn is restricted to the player whose turn it is
- **Who goes first is decided on the game page, after everyone has joined** — tap a player or roll for one. Skipping it starts from seat one
- Supplemental format trackers for **Planechase**, **Archenemy**, and multiplayer **Momir Basic**

**Analytics**

- **Per-game**: a stepped life chart sampled per life-changing event (not per round, which used to discard every intra-round swing), truncated where a seat was eliminated, plus a turn-pace strip
- **Cross-game** at `/games/analytics`: pod dynamics, turn pace, and elimination timing. Every average ships its sample size, and a game with no recorded event stream is excluded rather than counted as a zero
- **Playgroup record**: per-player wins, losses, and games played across the games linked to that playgroup, grouped by account rather than by the name typed on the night

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
- **Two-layer responsive treatment for wide tables**:
  - **Column-priority hiding** (v3.27.15) — `.col-priority-low` columns drop at ≤980px (laptop-narrow / tablet); `.col-priority-mid` columns drop at ≤768px (tablet portrait / phone). Applied uniformly across Decks, Watchlist, Admin, Games, Locations, Audit, and Tokens so the columns that remain stay readable without sideways scrolling being the primary interaction
  - **Stacked-card layout** at ≤480px (true phones) — six-column tables (decks, locations, games, card-detail inventory) flip via opt-in `class="stacking-table"` + `data-label` attributes; each row renders as a self-contained card with label/value pairs and action buttons grouped at the bottom
  - **Horizontal scroll** inside `.table-wrap` panels stays as the final-fallback floor for tables intrinsically wider than the available width even after column drops
- Popouts (Edit, inline-create) become viewport-centered modals on phones with semi-transparent backdrop, body scroll lock, auto-injected × close button, and dismiss on backdrop tap / Escape / × — see [docs/mobile_patterns.md](docs/mobile_patterns.md)
- Inventory card thumbnails compact further on true phones (130px below 480px, 170px at 480-768, 138px on desktop); drawer pills enforce a 44px tap-target floor
- Mobile fundamentals applied globally: 16px input font-size (prevents iOS auto-zoom on focus), `overflow-x: hidden` below 768px (page never horizontal-scrolls; tables still scroll internally), `box-sizing: border-box` on every element including pseudo-elements, `viewport-fit=cover` + `env(safe-area-inset-bottom)` for notched devices, `overflow-wrap: anywhere` on text containers, and a `min-height: 44px` floor on link-styled tap targets in nav, filter, hero, and pagination surfaces

---

## Stack

| Layer         | Technology                                    |
| ------------- | --------------------------------------------- |
| Web framework | FastAPI + Jinja2                              |
| Database      | PostgreSQL (CloudNativePG, via SQLAlchemy)    |
| Styling       | Custom CSS (no framework)                     |
| Card data     | [Scryfall API](https://scryfall.com/docs/api) |
| Runtime       | Docker / Kubernetes (Talos)                   |
| GitOps        | ArgoCD + ArgoCD Image Updater                 |

---

## Architecture

This repo contains **application code only**. Platform/infrastructure lives separately:

- **App repo** — this repo (FastAPI app, templates, migrations)
- **Platform repo** — [vanfreckle-platform](https://github.com/jasonvandeventer/vanfreckle-platform) (Kubernetes manifests, ArgoCD config)

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

The PostgreSQL schema is owned by **Alembic** (`alembic/versions/`) as of the v4.0.0 cutover — the boot-time SQLite migrator (`run_migrations()`) was retired then. For local SQLite dev the legacy scripts in `scripts/` still apply.

---

## Data Storage

- **Production**: PostgreSQL (CloudNativePG on the Talos cluster), via `DATABASE_URL`
- **Local dev**: SQLite file in `/data` (the default when `DATABASE_URL` is unset)

> Renamed 2026-07-06 (`mana_archive` → `cartarch`). Existing local dev DBs need: `mv data/mana_archive.db data/cartarch.db`

No database files are stored in this repository.
