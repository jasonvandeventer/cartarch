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

## North Star

Mana Archive is the source of truth for the playgroup. Authoritative data about who owns what, what's in which deck, and how decks have performed in our games lives here. External services (Scryfall, Spellbook, EDHREC) are integrated as enrichment for that data, not as replacements.

Practical implications for feature decisions:

- Recommendation features ground their suggestions in the user's collection first, the playgroup's data second, and external aggregators last.
- Analytics compare a deck against the user's other decks and the playgroup's game history, not against community averages.
- External service data appears as inline enrichment (per-card hover, combo detection results, EDHREC inclusion percentages) rather than as primary navigation surfaces.
- Features that would route users away from Mana Archive's data toward an aggregator's data are scrutinized closely. Enrichment is welcome; replacement is not.

---

## Tier 1: Near-term (this week to next)

Items with concrete demand or that close known gaps. Each is contained scope.

- **Foreign-language card support.** Track non-English printings in inventory. Add `language` column to InventoryRow, language metadata flows through imports (CSV + manual), small language badge ("JP", "DE", etc.) on card displays, `lang:` filter in the search syntax. Cards display in English art with a badge indicating physical language. No localized images, no foreign-name search in v1.

- **Auto-merge on `move_inventory_row_to_location`.** The manual card-move flow doesn't merge at destination the way `place_imported_rows` does (v3.16.17). Same dup-row issue when a user manually moves a card to a location that already has the same `(card, finish)`. About 30 lines, same fix pattern.

- **Pending page polish.** When an HTMX confirm empties a group, the group's `<details>` stays in the DOM until next page load. Cosmetic. Also tidy the empty-state copy ("No cards pending placement").

- **Per-rarity set completion.** The per-set page shows overall completion percentage. Add per-rarity breakdown (commons / uncommons / rares / mythics) so users can see where their gaps actually are. For Commander collectors, mythic and rare completion is the meaningful signal; overall percentage obscures it.

- **Drawer-sorter placement for tokens, foreign-language cards, and premium basics.** Personal workflow customization for the drawer-sorter user. Updates the Drawer 6 layout to top-to-bottom: numeric sets, foreign-language cards, premium basics (full-art / foil / alt-art / snow), plain basics, tokens, proxies. Premium basics support the "play with the nice ones first" workflow. Depends on the foreign-language support feature shipping first.

- **Deck view list mode with grouping.** Add a list/grid toggle to the deck detail page card display. List mode renders cards as text rows grouped by a user-selectable axis (type, mana value, role tag, color, subtype). Card image shown on hover (desktop) or tap (mobile). Sub-group counts surfaced inline ("Creatures (35) · Humans (6)"). The existing image grid stays available as the alternate view. User preference persisted per-user (not per-deck). Addresses a direct request from the playgroup: Moxfield-style scanning is the use case being filled.

- **In-place card editing in decks.** Users currently have to delete a card and re-add it to swap printings, change finish (foil/etched/nonfoil), or adjust basic land counts. Add inline edit affordances on cards in a deck:
  - Change printing: a dropdown control on each card (or row, in list view) offers a "Switch printing" option. Selecting it opens a modal listing every printing of that card. Printings the user already owns are sorted to the top of the modal as a separate section ("In your collection") above the full printings list. Selecting a printing updates the `InventoryRow` in place, preserving the card's place in the deck and any user-applied tags. The "owned at top" sort is a deliberate source-of-truth feature: Mana Archive knows the user's inventory authoritatively and surfaces owned options first, which is something generic deckbuilders cannot do well.
  - Change finish: each printing in the switch-printing modal shows nonfoil / foil / etched options as toggleable buttons, restricted to the finishes that actually exist for that printing. Foil availability is queried from Scryfall's per-printing data. The toggle also indicates which finishes the user owns of that specific printing.
  - Adjust basic land quantity: basic land rows show +/- controls to bump count up or down without re-entering the card name. Bumping past zero removes the row; bumping past available inventory creates a new row from the imported-cards path.

  Two users requested this directly. The edit affordance lives on both the existing image-grid view and the new list view (see the "Deck view list mode with grouping" item above), so the two features are sequenced together — implement the list view first since the inline-edit control fits more naturally in a list row than overlaid on a card image.

## Tier 2: Significant features (next 1-2 months)

Larger arcs that need design conversations before implementation. Both have design docs in place; both are sequential rather than parallel.

- **Tag system accuracy overhaul.** The auto-tagger (`suggest_card_roles` in `deck_service.py`) and the user-applied row tag system (`InventoryRow.tags`) are the foundation for Synergy, Health, the existing Upgrade Targets feature, and the future AI recommendation engine. Users have surfaced concerns that these are not reliable in practice.

  Scope:
  - Audit the existing auto-tagger rules against a sample of 100 real-world cards. Document false positives and false negatives.
  - Define a more precise tag taxonomy. The current 10-tag system stays, but with clearer rules for ambiguous cases: hybrid mana costs, cards with multiple modes, cards whose role depends on commander.
  - Add a tag confidence indicator (high / medium / low) so downstream consumers (Synergy, Health, Upgrade Targets, AI engine) can choose to use only high-confidence tags.
  - Add a per-deck "review tags" workflow that surfaces low-confidence auto-tags for the user to confirm, edit, or dismiss.

  Design doc: [tag_system_overhaul.md](tag_system_overhaul.md). Sequencing: tag work ships first, analytics overhaul ships against the cleaner tag base, AI recommendations ship after both.

- **Analytics overhaul.** Replace Bracket V2's single power score with a three-layer data-first display: objective composition signals (tutors, fast mana, board wipes, combos, game changers), empirical play record (win rate, average finishing position, games played), and comparative context within the playgroup (percentile within active decks). Design doc at [analytics_overhaul.md](analytics_overhaul.md). Multi-session refactor that deletes `bracket_v2_service.py`, the intent survey, and related CSS. Addresses a known structural issue with the current scoring (commander identity not weighted, scoring feels arbitrary).

  Source-of-truth framing: the three-layer display (composition signals, play record, playgroup-relative context) is anchored in the user's own data and the playgroup's actual game history, not aggregate community data. External services (EDHREC inclusion percentages, Spellbook combos) are integrated as enrichment for the user's data, not as replacements. This is consistent with the source-of-truth positioning Mana Archive holds for the playgroup.

- **Deck playtester.** Single-player playtest mode integrated with the existing game tracker. Virtual hand / library / battlefield / graveyard / exile zones; draw, tap, shuffle, mulligan; optional persistence and replay. The "single-player game tracker plus card zone management" framing keeps scope tractable, but this is still 6-8 sessions across a few weeks. Addresses the only Moxfield-retention driver named by users.

## Tier 3: Real but lower-priority

Items that are documented and scoped but not urgent.

- **AI-powered deck upgrade suggestions (Phase 1 of AI engine).** Replaces the current Upgrade Targets feature, which users have flagged as inaccurate. Takes the deck's commander, strategy as expressed through tags and themes, and the user's collection as input. Returns a ranked list of suggested additions and cuts, with each suggestion grounded in cards the user already owns (loose in storage, or in other decks that could be cannibalized) before suggesting cards to acquire. Optionally takes a free-text "deck intent" string from the user ("big stompy land creatures, multiple combats") as additional context for the LLM prompt.

  Depends on: analytics overhaul (Tier 2) for reliable composition signals; tag system accuracy overhaul (Tier 2) for trustworthy tags. Both must ship first.

  Scope: ships in 1-2 weekends after dependencies are satisfied. Single LLM API call with a thoughtful prompt. Provider abstraction (Phase 2 of the AI engine) can wait. This Tier 3 placement is a promotion of the existing Phase 1 in the Tier 4 AI engine arc.

- **Deck Reconciliation Session 3.** Edge cases in the deck reconciliation flow: multi-source moves, concurrent edits between preview and commit, brand-new decks with no existing rows. Documented in [deck_collection_model.md](deck_collection_model.md) as deferred. Unlikely to bite normal usage; address when a specific case surfaces.

- **Wishlist support.** Optional `is_wishlist` flag on InventoryRow for tracking cards a user wants but doesn't own. Architecture already supports it. Wait for an explicit user request rather than building speculatively.

- **Token import-from-deck flow.** Bulk-add tokens to inventory based on what a user's decks produce. Discussed early in the project, deferred when user feedback prioritized other work.

## Tier 4: Future / Someday

Items here are speculative and may never be implemented. Capturing them keeps the architecture from accidentally foreclosing the option, but none of these are commitments.

- **"What changed since I last looked" per-deck view.** Surface price drift, legality flips, new combos detected by Spellbook since the user's last visit. Would lean on the existing TransactionLog.

- **Set completion progress across the catalog.** A cross-set dashboard ("you're 73% on MH3, 41% on LCI") rather than only the per-set page.

- **Mobile deck-editing additional polish.** The CubeCobra-style panel works; if users surface specific gaps (e.g., a "Remove/Replace" section), follow up.

### AI-backed recommendation engine

A multi-phase feature spanning deck building, collection analysis, and playgroup meta. Phases 2-6 are aspirational. Phase 1 has been promoted to Tier 3 based on user demand and its dependency chain (analytics overhaul + tag system overhaul). Three motivations:

1. **Personal utility.** Deck-building creativity support — suggest cards from the user's collection that fit a deck's themes, propose new deck ideas from owned cards, budget-constrained upgrade paths per deck.
2. **Learning project.** Productionizing LLM-powered features is a career-relevant skill (provider abstraction, cost management, caching, observability, error handling, RAG patterns). This feature is an opportunity to build that skill against a real personal use case.
3. **Possible commercialization path.** If Mana Archive ever moves toward public use, AI-powered features are the kind of differentiator that justifies a paid tier. Not a commitment to that path; just an option the architecture should not foreclose.

**Scope (phased):**

- _Phase 1:_ Deck upgrade suggestions. Given a deck and the user's collection, suggest cards from the collection that fit the deck's themes. Simple LLM API call with a thoughtful prompt; ships in 1-2 weekends. (promoted to Tier 3 — see Tier 3 entry above).
- _Phase 2:_ Architectural foundation. Provider abstraction (so the app can swap between Claude, OpenAI, local models), caching by deck-contents hash, request/response logging, cost tracking. Doesn't change user-visible behavior; builds the foundation for everything that follows.
- _Phase 3:_ Embedding-based card similarity. Compute and cache embeddings for cards. Use vector similarity for candidate selection, then LLM for ranking and explanation (retrieval-augmented generation pattern).
- _Phase 4:_ Combo and synergy discovery beyond what CommanderSpellbook surfaces. Use the LLM to identify unexpected interactions among cards in the user's collection.
- _Phase 5:_ Playgroup meta analysis. Given the play record data from the analytics overhaul, suggest deck adjustments based on what's winning or losing in the playgroup over time.
- _Phase 6:_ New deck ideas from collection. "Here are five Commander decks you could build with what you own." More speculative; deepest creative use of the LLM.

**Dependencies:**

- Analytics overhaul must ship first (Phase 5 requires the play-record data the overhaul produces; phases 1-4 don't strictly require it but benefit from the cleaner deck-data surface the overhaul provides).
- A design doc is required before Phase 1 implementation, covering: provider choice and abstraction approach, prompt design, cost modeling, caching strategy, observability, hallucination handling (LLMs sometimes suggest cards that don't exist), and the success metric for v1.

**Architectural notes:**

- LLM API costs scale per-use, unlike feature engineering costs. At playgroup scale (~10 users, ~10 recommendations/week each), costs are negligible — probably under $5/month. At public scale this would need a cost-control strategy (per-user quotas, free tier limits, paid tier features).
- Cache by deck-contents hash; same deck shouldn't generate the same recommendation request twice within a TTL window.
- Log every recommendation request and response for quality analysis and future fine-tuning data. Treat this as observability from day one, not as something added later.
- Provider abstraction lives behind a single interface so swapping Claude → OpenAI → local model is a configuration change, not a refactor.

**Status:** Phase 1 promoted to Tier 3. Phases 2-6 remain aspirational with no timeline; revisit after Phase 1 ships and behaviour in production is observed.

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

- [tag_system_overhaul.md](tag_system_overhaul.md) — Tier 2 tag system accuracy overhaul.
- [analytics_overhaul.md](analytics_overhaul.md) — Tier 2 analytics redesign.
- [deck_collection_model.md](deck_collection_model.md) — Refined Model A for deck/collection reconciliation; documents the deferred Session 3 work.
- [collection_import_sync.md](collection_import_sync.md) — Sync-semantics model for full-collection re-imports.
- [mobile_patterns.md](mobile_patterns.md) — Living reference for mobile UI primitives (popovers, cache-busting, tap targets).
- [mobile_audit.md](mobile_audit.md) — Historical snapshot of the v3.16.3 mobile sweep.
- [v3_storage_and_multi-user_plan.md](v3_storage_and_multi-user_plan.md) — Architectural record of the v3 storage/multi-user transition.
