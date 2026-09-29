# Code duplication review — 2026-09-29

Reviewed application Python, first-party JavaScript/CSS, templates, and Python scripts using exact-block and function-similarity scans, followed by caller and behavior inspection. Vendored assets, historical migrations, and test fixture repetition are not consolidation targets. This is a review of unnecessary duplication, not a guarantee that every similar line has been eliminated.

## Consolidated

- **Import commits:** CSV/text and manual imports now share single-destination dispatch, placement, sorting, and result rendering in `routes/imports.py`. CSV auto-sort still redirects to Pending; manual auto-sort still renders its result. Per-row destination imports retain their separate behavior.
- **Audit lists:** extras and out-of-scope scans share the owner check, query, and printing/finish aggregation. Their scan types remain separate.
- **Card metadata refresh:** supplied and fetched payloads use one update block. Missing trait keys still preserve existing values, and complete cards do not trigger a fetch.
- **Legacy dashboard:** removed the unused three-tile aggregate implementation and its obsolete route comment. The active dashboard remains the only implementation.
- **Legacy deck presentation:** removed the unused deck-items view builder; the live deck route owns its current inventory-based presentation.
- **Remaining legacy presentation:** removed three uncalled drawer-summary, drawer-detail, and card-detail builders. Active routes already produce their own payloads.

The cleanup reduces application code without new dependencies or service layers. Changes remain local for review.

## Follow-up fixes

- Dashboard holdings and new daily value snapshots count proxy rows as zero value, matching the deck portfolio's ownership valuation. Historical snapshots remain intact because their original inventory composition cannot be reconstructed reliably. Re-running today's snapshot updates today's value, including an all-proxy collection becoming zero.
- Both import commit and reconciliation preview validate required parallel-array lengths before processing rows. Missing or surplus fields produce a helpful 400 response and no writes. Optional arrays retain their legacy defaults.

## Similarities retained deliberately

- Collection and deck reconciliation already share inventory lookup. Their remaining recommendations differ: collection sync counts all ownership; deck reconciliation distinguishes movable, shared, sibling, and already-in-deck copies.
- Import metadata updates intentionally leave MTGJSON prices alone; they cannot be replaced wholesale with the card-refresh updater.
- Shared/trade card templates use different privacy projections and controls. Similar markup is not sufficient reason to unify those contracts.
- Trade action routes already call one transition service. Explicit route declarations and CSRF dependencies remain easy to inspect.
- Location create/edit and token create/edit share domain helpers but have different lookup, validation, and redirect behavior.
- Active drawer and card routes expose different fields and location labels. Short arithmetic repetition does not justify a configurable renderer; their unused legacy builders were removed.
- Autocomplete controls have different grouped-search, printing-selection, and form behavior. Existing server-side resolution remains shared.
- SQLite/PostgreSQL guards, client/server game handling, and responsive CSS serve different runtimes or contexts.

## Verification

Full SQLite: **2,013 passed, 3 skipped**. Full PostgreSQL 18: **2,015 passed, 1 skipped**. Route smoke, Ruff lint/format, dependency checks, and whitespace checks passed. Logs are saved under `output/code-duplication-review/` locally.

After the follow-up fixes, the full suites passed again: **2,039 passed, 3 skipped on SQLite; 2,041 passed, 1 skipped on PostgreSQL 18**. The new tests cover rendered dashboard value, zero-value all-proxy collections, preserved historical snapshots, mismatched required import fields, optional-field defaults, and no-write rejection. Isolated mutations verified that disabling either fix fails its regression tests. Lint, formatting, dependency, and whitespace checks passed; logs are saved under `output/followup-fixes/`. The temporary test database was removed.

Regression tests exercise both import routes with and without reconciliation, explicit destinations versus auto-sort, audit finish/type separation and ownership, and supplied versus fetched metadata. Three isolated mutations confirmed that the new tests detect broken import redirects, mixed audit types, and skipped fetched metadata updates. Existing rollback, brew, and scoped audit tests remain part of the full suites. The disposable PostgreSQL container was stopped after validation.

## Second-pass fixes

- Drawer detail now reads canonical `StorageLocation` membership, matching the overview. Direct imports appear immediately; stale legacy drawer fields cannot put a binder card in a drawer.
- Inventory valuation uses one `inventory_unit_price` helper across Collection, locations, cards, drawers, pending rows, decks, and inventory sorting. Proxies contribute zero. Catalog/replacement quotes and export price fields retain their existing semantics.
- Pending batch groups recognize the actual `import` event and legacy `imported` events. A window query selects the latest timestamp, with ID as a deterministic tie-breaker; both logs and batch metadata are owner-scoped.
- Import commit and reconciliation preview reuse the existing strict boolean parser. Invalid proxy/brew fields return 400 before writing; omitted optional fields retain their defaults.
- Removed three uncalled helpers: `list_set_completion_summaries`, `fetch_token_by_name`, and `get_previous_location_for_row`.

Added regressions exercise real import requests, rendered inventory surfaces, stale drawer labels, ownership boundaries, batch timestamp ordering/ties, and invalid flags. Six isolated mutations confirm the tests detect broken drawer membership, proxy valuation, event matching, timestamp ordering, tie-breaking, and flag validation. The existing drawer price-render fixture now attaches its row to the canonical location.

Final second-pass validation: **2,054 passed, 3 skipped on SQLite; 2,056 passed, 1 skipped on PostgreSQL 18**. Ruff lint/format, dependency checks, and whitespace checks passed. Results and mutation evidence are saved under `output/second-review-fixes/`. Changes remain local; no push or deployment was performed.

## Iterative review and fixes

Repeated the review → reproduce → fix → targeted-test cycle, followed by a final caller/diff review.

- **Inventory identity:** returning a physical proxy from a normal deck preserves proxy status and language, including when pending rows already exist. New returned rows retain notes/tags. Printing swaps no longer absorb real cards into proxy rows on either leg; the returned printing retains its language. Brew-placeholder discard semantics and existing merge metadata policy remain unchanged.
- **Collection consistency:** `drawer:` search uses canonical location membership. Sidebar counts exclude brew placeholders just like the grid. Search/facet price filters and dashboard/history totals now share `inventory_unit_price_expr`, eliminating the two drifting SQL implementations. Proxies have zero value; empty/zero foil prices fall back consistently with displayed prices. Empty strings are normalized before PostgreSQL casts; unknown prices remain NULL for filters.
- **Retired decks:** public shares and sibling links exclude retired identities; Goldfish uses the existing active-deck lookup. Editing and import auto-creation can reuse retired names. The underlying deck identities and game-history references remain intact.
- **Dead code:** removed eight uncalled helpers across auth, DB sessions, name lookup, watchlist queries, and legacy inventory creation, plus dummy references that only suppressed unused-import warnings. The session-auth helper used by live-game SSE remains in place.

The original defects were reproduced before correction. Final regressions independently distinguish language and proxy merge guards and exercise rendered routes as well as services. The follow-up scan checked application callers (including aliases, route registration, templates, tests, and scripts), exact duplicate function bodies, related valuation queries, retirement readers, and the final diff. It found no further actionable issues in this pass; similar code with distinct contracts was retained.

Final iterative validation: **2,072 passed, 3 skipped on SQLite; 2,074 passed, 1 skipped on PostgreSQL 18**. Nine isolated mutation checks caught removed guards for returned proxy identity, language, copied proxy status, printing-swap identity, SQL proxy value, facet scope, public retirement filtering, retired-name reuse, and retired import lookup. Lint, formatting, dependency checks, and whitespace checks passed. Logs are in `output/iterative-review/`. No migration, dependency addition, push, or deployment was performed.

## Release v4.19.7

Packaged the reviewed changes with the release record. Local fresh-install and v4.19.6-to-head migration rehearsals passed, as did Chromium/Firefox image-fallback regressions and built-container HTTP, authentication, CSRF, deck-menu, quantity-update, and usability checks at desktop/mobile sizes. Local release evidence is in `output/release-v4.19.7/`. Publication uses the repository CI pipeline, which tests and publishes its saved image without rebuilding.
