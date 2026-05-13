# Mana Archive Roadmap

Mana Archive is a private collection, deck, and game-tracking tool for a small Commander playgroup (7-10 users). It is intentionally self-hosted and not currently planned for public release — there is no SaaS, no marketing site, and no signup funnel. The app stays simple, opinionated, and tuned to the playgroup's actual workflows.

This roadmap reflects current priorities. Specific timing depends on available time and what the playgroup surfaces in real use. Items can move between tiers as demand changes.

---

## Current Status

As of v3.16.23, the app covers:

- **Collection management** — full inventory with Scryfall-syntax search (`t:`, `c:`, `cmc:`, `o:`, `id:`, `price:`, `is:foil`, `qty:`, boolean operators, parentheses, quoted values), sort by name/type/color/mana cost/price, per-user data isolation.
- **Deck management** — Commander (or any format) decks with analytics (mana curve, card types, color pips, average/peak turn), health (ramp/draw/removal/wipe density, pip strain), Bracket V2 estimation, Commander Spellbook combo integration, synergy classification, role tags, in-page Scryfall-syntax search via HTMX, and a mobile-friendly "Add card to this deck" panel.
- **Game tracker** — Commander game logging with ±1/±5/±10 life buttons, commander damage matrix, poison/experience counters, turn counter, undo, elimination, fixed 8-seat topology with per-seat position picker, layout-aware card rotation, deck record (W/L) on each deck's detail page.
- **Imports** — CSV (Scanner App, Helvault free/pro, Moxfield collection), paste list (Moxfield deck exports, MTGA/MTGO, bare set+collector), manual entry, all with deck-import reconciliation (move owned copies vs import new) and collection-import reconciliation (skip vs delta vs new, with deck-located duplicates surfaced for user decision).
- **Tokens** — separate inventory catalog with Scryfall autocomplete, exact-printing lookup, double-faced token support (including cross-set DFC pairings), bulk add, and per-deck Tokens-Needed table.
- **Sets** — browse by set with owned/missing badges, completion percentages, substitute card handling.
- **Multi-user** — self-service registration with email + display name, per-user data isolation, admin panel for user lifecycle, account self-service for profile and password.
- **Mobile** — full responsiveness across every page except the live game tracker (which is tablet-landscape-first by design), 5-tab bottom nav below 768px with a "More" overlay, viewport-centered popovers, stacking tables on phones, 44px tap target floor.
- **Imports/inventory hygiene** — auto-merge at the destination on `place_imported_rows` (no more accidental duplicate rows), tag preservation when pulling cards into a deck, reconciliation against the user's full inventory across drawers/binders/boxes/pending/decks.

Stack: FastAPI + Jinja2 + SQLite via SQLAlchemy + HTMX (self-hosted). Deployed via the separate platform repo (K3s + ArgoCD), but the platform layer is out of scope for this roadmap.

---

## Tier 1: Near-term (this week to next)

Items with concrete demand or that close known gaps. Each is contained scope.

- **Foreign-language card support.** Track non-English printings in inventory. Add `language` column to InventoryRow, language metadata flows through imports (CSV + manual), small language badge ("JP", "DE", etc.) on card displays, `lang:` filter in the search syntax. Cards display in English art with a badge indicating physical language. No localized images, no foreign-name search in v1.

- **Auto-merge on `move_inventory_row_to_location`.** The manual card-move flow doesn't merge at destination the way `place_imported_rows` does (v3.16.17). Same dup-row issue when a user manually moves a card to a location that already has the same `(card, finish)`. About 30 lines, same fix pattern.

- **Pending page polish.** When an HTMX confirm empties a group, the group's `<details>` stays in the DOM until next page load. Cosmetic. Also tidy the empty-state copy ("No cards pending placement").

- **Per-rarity set completion.** The per-set page shows overall completion percentage. Add per-rarity breakdown (commons / uncommons / rares / mythics) so users can see where their gaps actually are. For Commander collectors, mythic and rare completion is the meaningful signal; overall percentage obscures it.

- **Drawer-sorter placement for tokens, foreign-language cards, and premium basics.** Personal workflow customization for the drawer-sorter user. Updates the Drawer 6 layout to top-to-bottom: numeric sets, foreign-language cards, premium basics (full-art / foil / alt-art / snow), plain basics, tokens, proxies. Premium basics support the "play with the nice ones first" workflow. Depends on the foreign-language support feature shipping first.

## Tier 2: Significant features (next 1-2 months)

Larger arcs that need design conversations before implementation. Both have design docs in place; both are sequential rather than parallel.

- **Analytics overhaul.** Replace Bracket V2's single power score with a three-layer data-first display: objective composition signals (tutors, fast mana, board wipes, combos, game changers), empirical play record (win rate, average finishing position, games played), and comparative context within the playgroup (percentile within active decks). Design doc at [analytics_overhaul.md](analytics_overhaul.md). Multi-session refactor that deletes `bracket_v2_service.py`, the intent survey, and related CSS. Addresses a known structural issue with the current scoring (commander identity not weighted, scoring feels arbitrary).

- **Deck playtester.** Single-player playtest mode integrated with the existing game tracker. Virtual hand / library / battlefield / graveyard / exile zones; draw, tap, shuffle, mulligan; optional persistence and replay. The "single-player game tracker plus card zone management" framing keeps scope tractable, but this is still 6-8 sessions across a few weeks. Addresses the only Moxfield-retention driver named by users.

## Tier 3: Real but lower-priority

Items that are documented and scoped but not urgent.

- **Deck Reconciliation Session 3.** Edge cases in the deck reconciliation flow: multi-source moves, concurrent edits between preview and commit, brand-new decks with no existing rows. Documented in [deck_collection_model.md](deck_collection_model.md) as deferred. Unlikely to bite normal usage; address when a specific case surfaces.

- **Wishlist support.** Optional `is_wishlist` flag on InventoryRow for tracking cards a user wants but doesn't own. Architecture already supports it. Wait for an explicit user request rather than building speculatively.

- **Token import-from-deck flow.** Bulk-add tokens to inventory based on what a user's decks produce. Discussed early in the project, deferred when user feedback prioritized other work.

## Tier 4: Future / Someday

Items that are interesting but speculative or low-impact.

- **"What changed since I last looked" per-deck view.** Surface price drift, legality flips, new combos detected by Spellbook since the user's last visit. Would lean on the existing TransactionLog.

- **Set completion progress across the catalog.** A cross-set dashboard ("you're 73% on MH3, 41% on LCI") rather than only the per-set page.

- **Mobile deck-editing additional polish.** The CubeCobra-style panel works; if users surface specific gaps (e.g., a "Remove/Replace" section), follow up.

---

## Explicitly NOT Planned

Items that are sometimes assumed to be on the roadmap but are not:

- **PostgreSQL migration.** SQLite is the permanent choice for this app. The current single-instance, small-user-base architecture doesn't justify the operational complexity of a separate database server. This would only be revisited if the app were opened to public users, which is not currently planned.

- **Phone-based card scanner.** Explored May 2026, shelved due to accuracy ceiling at 100k+ card scale. CSC100 export path remains the bulk-ingest workflow. May revisit if a cloud recognition API becomes acceptable or if hardware acceleration changes the on-device math.

- **App UI internationalization.** The interface is English-only; language support refers to card data, not the app itself. The playgroup is English-speaking.

---

## Roadmap Discipline

New ideas should be classified before becoming work:

- **Blocking bug:** fix immediately if it affects data integrity, imports, login/session, deployment, or normal collection use.
- **Tier 1 candidate:** real near-term demand from actual usage; contained scope.
- **Tier 2 candidate:** larger feature with a clear motivating case; needs a design doc before implementation.
- **Tier 3+ candidate:** real but not urgent; document and revisit.
- **Interesting distraction:** don't implement unless it becomes repeated real pain.

When in doubt, ship for the current user base rather than for hypothetical future users. Adoption signal beats speculation.

---

## Design Docs

Implementation references kept alongside this roadmap:

- [analytics_overhaul.md](analytics_overhaul.md) — Tier 2 analytics redesign.
- [deck_collection_model.md](deck_collection_model.md) — Refined Model A for deck/collection reconciliation; documents the deferred Session 3 work.
- [collection_import_sync.md](collection_import_sync.md) — Sync-semantics model for full-collection re-imports.
- [mobile_patterns.md](mobile_patterns.md) — Living reference for mobile UI primitives (popovers, cache-busting, tap targets).
- [mobile_audit.md](mobile_audit.md) — Historical snapshot of the v3.16.3 mobile sweep.
- [v3_storage_and_multi-user_plan.md](v3_storage_and_multi-user_plan.md) — Architectural record of the v3 storage/multi-user transition.
