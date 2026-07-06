# Cartarch Repo Map

This file explains **where changes belong** so you stop guessing and start debugging with intent.

## Core rule

- **`app/main.py`**: HTTP routes only. Request parsing, calling services, redirects, and choosing templates.
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

- `app/main.py`

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

## Current cleanup included in this version

- Added comments/docstrings across the Python app files.
- Added `app/presentation_service.py` so routes are thinner.
- Updated `app/main.py` to use presentation helpers.
- Fixed the card detail page so it fetches **only the target card rows** instead of loading the whole collection.

## What is still overloaded

The biggest remaining hotspot is **`app/inventory_service.py`**.

That file still owns a lot:

- drawer rules
- card upsert logic
- row merge/update/delete
- undo logic
- resort logic

That is acceptable for now, but it is still the most likely place for future bugs to cluster.
