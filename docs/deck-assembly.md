# Deck assembly checklist

Local implementation for the next feature release; not deployed yet.

1. Create a **Brew** deck and import its card list into that deck.
2. In the import preview's Reconciliation section, choose **Plan all for assembly**.
   This creates placeholders while leaving owned cards in their current locations.
   Individual rows can still use the existing move/import choices.
3. Open **Assembly checklist** from the import result or deck page.
4. Follow the location and slot groups, choose the physical printing/finish/language,
   set the quantity, and press **Confirm pull** after placing those copies in the deck.
5. Leave and return anytime. Confirmed moves are saved in inventory; unfilled
   placeholders remain on the checklist. The final pull automatically clears Brew.

The page separates available pulls, unavailable placeholders, copies already recorded
in the deck, and shared references to other decks. Real copies in other decks or
considering areas are informational only. Physical proxies in ordinary decks remain
physical proxies; this workflow does not silently replace them.

Progress has no separate table: `InventoryRow` remains the source of truth. Each
confirmation rechecks ownership, lifecycle, name, quantity, and the displayed row
versions under database locks. A stale or repeated submission refreshes the checklist
without making another move. One physical source is suggested only once even when
multiple placeholders need the same card. Source choices are suggestions, not
reservations against other decks or users' concurrent actions.

Assembly and the existing bulk Materialize action share the same movement primitive.
It preserves the selected printing, finish, language and commander role, cleans up
references when a source is depleted, and logs the actual source location. Existing
rows already in a deck are treated as recorded placement, not as newly confirmed
physical checks.

Checks: `tests/test_deck_assembly.py` covers the import-to-pull workflow, read-only
rendering, partial/resumed/replayed requests, unavailable sources, owner boundaries,
language identity, commander roles, and a PostgreSQL race for the last copy.
`tests/browser/assembly.cjs`, invoked by `scripts/check_image.sh`, exercises actual
confirmations in Chromium and Firefox at 390px and 1400px.

Companion features are now implemented locally: saved Collection views under
**Filters & saved views**, and shared-card controls with **Open source deck** and
explicit removal feedback. Saved views include filters, sorting, and layout, and
can be renamed, replaced with the current view, or deleted. They require migration
`b82ad74e19c3`; no production deployment has been performed.

## v4.20.0 candidate validation

Prepared locally on 2026-09-29. README and Chronicle now carry v4.20.0;
deployment is still pending approval. The combined review found no further fixes.

- Full SQLite suite with the final release record: 2,093 passed, 4 skipped.
- Fresh feature, brew, account-deletion, and route regressions: 69 passed and
  1 PostgreSQL-only skip on SQLite; all 70 passed on PostgreSQL, including the
  competing-deck pull race.
- The earlier full PostgreSQL run passed 2,096 tests with 1 skip. Application
  behavior has not changed since that run; this preparation changes release metadata.
- Fresh-install and v4.19.7 upgrade rehearsals passed.
- The built v4.20.0 image passed startup, authentication, CSRF, existing usability,
  and all three feature flows in Chromium and Firefox at 390px and 1400px.
  Artwork fallback regressions passed in both browsers. Mobile screenshots were
  visually inspected. Ruff and whitespace checks passed.

Fresh evidence: `output/release-v4.20.0/`; earlier full PostgreSQL evidence:
`output/saved-shared/postgres-final.log`. The tested local image is
`cartarch-v4.20.0:validated`, ID
`sha256:f4cd60448f12cd176b14d20e4bafdd0690f85daad10c25118bba0210eedc3956`.

The only schema change adds saved Collection views. The previous application
can run with that table left in place if an application rollback is needed;
downgrading the migration would delete saved views. Before deployment, recheck
production backup health and the previous immutable image. Publish through the
existing verification workflow, which publishes its saved tested image without
rebuilding. No tag, push, or production change was made during this preparation.

Publication approved on 2026-09-30. Preflight found main aligned with origin,
production v4.19.7 ready, and PostgreSQL healthy with three ready instances,
successful backup and continuous archiving. The daily 2026-09-30 backup completed.
Previous production image for rollback:
`ghcr.io/jasonvandeventer/cartarch@sha256:be966b13cba2283ec72677ce621784fbf49338ed2ba8c2e65c453fdb1df88176`.
