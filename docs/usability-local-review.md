# Local usability candidate

This worktree is a local-only candidate based on v4.19.5. No release tag, image push, or production change is included.

## Changes

- Mobile collection search uses a normal input height. Filters start collapsed on narrow screens and remain expanded on desktop; the summary identifies active filters.
- A search with no results shows a no-match state, not new-account onboarding. Filtered totals identify their scope and link back to the whole collection.
- Login and import controls have accessible names.
- Landing/auth copy states free access and self-service registration.
- Detailed deck analysis and piloting notes use native disclosures; routine card work remains visible. Management actions are grouped separately. Saving a profile reopens its section after reload.
- Import methods use exclusive native disclosures. Switching methods preserves text already entered. Syntax examples and exact-printing entry remain available without filling the initial screen.
- New-account steps link to their destinations, and remaining setup work stays visible after an import. Dashboard search now accurately describes collection search.
- Startup seed imports require a matching owner and deck name, not only a numeric deck ID. See the deployment consideration below.

## Browser measurements

Same synthetic fixture, 390px viewport; before values are from the fresh usability audit. The fixture contains 100 distinct card rows; menu tests adjust the Forest quantity.

| Measurement | Before | Candidate (Chromium) |
| --- | ---: | ---: |
| Collection search field height | 320px | 38px |
| First collection card, distance from document top | 2298px | 1300px |
| Deck Add/Collection/This deck controls | 3313px | 728px |
| Paste field after choosing Paste Card List | 999px | 594px |

Firefox measured a 42px search field, first collection card at 1316px and deck controls at 735px. Deck reduction includes hiding detailed panels and preventing the fixture from receiving an unrelated seeded profile. Values vary with actual deck content and text length.

Both engines passed at 390px and 1400px: lazy menu failure/retry/reuse; quantity changes and refreshed totals; full-page card forms; filter disclosure; no-match recovery; import switching with input retention; accessible labels; piloting save/reload; management modal access; and horizontal-overflow checks.

## Validation results

- Full SQLite suite: 2002 passed, 3 skipped.
- Full PostgreSQL suite: 2004 passed, 1 skipped.
- Built-image startup, authenticated HTTP/CSRF checks, and both browser suites passed.
- Ruff lint/format and staged whitespace checks passed.
- Empty-search and seed identity regressions were mutation-verified.

## Local reproduction

From this worktree, with Docker and installed Python/Node dependencies:

```sh
docker build --build-arg APP_VERSION=local-usability -t cartarch-usability:local .
PYTHON=.venv/bin/python EXPECTED_VERSION=local-usability SMOKE_PORT=5582 SMOKE_HOLD_SECONDS=1800 scripts/check_image.sh cartarch-usability:local
```

The script runs migrations, seeds disposable data, starts the actual app, and runs both browser suites. After they pass it leaves the app at http://127.0.0.1:5582 for 30 minutes, then removes its containers/network. Synthetic login: `smoke@example.invalid`, password `local-smoke-password`. This account exists only in the disposable test database.

The focused regressions are `tests/test_usability_review.py`; the browser additions are `tests/browser/usability.cjs`, invoked by the existing image gate. Mutation checks confirmed that restoring filtered-count onboarding or bypassing seed identity makes the corresponding tests fail.

## Seed identity: review before production

Existing saved profiles, custom edits, and simulation results are preserved. Old seed exports lack identity and are therefore skipped; they no longer attach to arbitrary same-numbered decks in a fresh database. This deliberately pauses automatic seed refresh for those exports until their identity is verified.

To re-enable an entry, add `_identity` alongside its profile fields (or alongside the simulation entry fields):

```json
"_identity": {"owner_username": "collector@example.invalid", "deck_name": "Example deck"}
```

Both values must match the resolved deck exactly. Identity metadata is excluded from persisted profile/result data. Unknown, renamed, or differently owned decks are skipped, preserving existing records.

Automatic approval review rejected a production identity read because the authorized task was local-only. No production identities were retrieved. Before rollout, review whether to supply a verified mapping or keep automatic seed refresh disabled. The UI changes themselves do not require production data or schema changes.
