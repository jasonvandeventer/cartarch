# Cartarch Repo Map

This file explains **where changes belong** so you stop guessing and start debugging with intent.

## Core rule

- **`app/main.py`**: Application startup/shutdown, middleware, background loops, and remaining top-level pages.
- **`app/routes/`**: Feature HTTP routes: parsing, authorization, calling existing services, redirects, and template context.
- **`app/dependencies.py`**: Shared route dependencies, rendering, CSRF, and redirect helpers.
- **`app/presentation_service.py`**: Shapes ORM rows into the dictionaries/totals the templates expect.
- **`app/inventory_service.py`**: Collection business rules. Drawer assignment, sorting, merge/update/delete, undo, resort.
- **`app/import_service.py`**: CSV parsing, row normalization, and import persistence.
- **`app/deck_service.py`**: Deck creation and moving cards into/out of decks.
- **`app/drawer_service.py`**: Read-only drawer queries.
- **`app/audit_service.py`**: Import batches and transaction log creation/listing.
- **`app/pricing.py`**: Finish-aware pricing helpers.
- **`app/scryfall.py`**: API lookups, normalization, retries, throttling, refreshes.
- **`app/models.py`**: Database schema.
- **`app/db.py`**: Engine, session factory, declarative base.
- **`app/templates/`**: HTML structure and rendering.
- **`app/static/`**: CSS and static assets.

## How to debug by symptom

### 1. Wrong data is stored or moved

Look in:

- `app/inventory_service.py`
- `app/import_service.py`
- `app/deck_service.py`

### 2. A page renders but totals/groups are wrong

Look in:

- `app/presentation_service.py`

### 3. Form submits to the wrong place or redirects wrong

Look in:

- `app/routes/` (the feature module)
- `app/main.py` for remaining top-level pages

### 4. A page looks bad but data is right

Look in:

- `app/templates/`
- `app/static/style.css`

### 5. Price or finish behavior is wrong

Look in:

- `app/pricing.py`
- anywhere that passes `finish`

### 6. Scryfall fetch/import behavior is wrong or slow

Look in:

- `app/scryfall.py`
- `app/import_service.py`

## Validation and deployment

- `tests/`: route, service, schema and template regression tests.
- `tests/browser/`: Chromium/Firefox behavior checks and image-fallback regressions.
- `alembic/`: production PostgreSQL migrations; SQLite tests use ORM metadata.
- `scripts/check_migrations.py`: fresh and previous-release upgrade rehearsal.
- `scripts/check_image.sh`: real container startup, HTTP and browser checks.
- `.github/workflows/verify.yml`: shared CI/release gate; publishing reuses its tested image.

See [local validation](local-validation.md) and [backup and recovery](backup-strategy.md).
