"""Regression: scryfall_cards.full_art is Boolean, not Integer.

The ingest writes a Python ``bool`` into ``full_art``; on Postgres an INTEGER
column rejected it (``DatatypeMismatch``) and failed the whole batch upsert every
cycle. The column is now native Boolean. This drives the REAL upsert SQL
(``_BULK_UPSERT_SQL``) against the REAL table definition
(``legacy_tables.scryfall_cards``) for each truth state and asserts a clean bool
round-trip back through ``_cached_row_to_payload``.

This rides the shared ``db_engine`` fixture, which is the project's dual-backend
seam: temp SQLite by default, but a real Postgres engine when ``TEST_DATABASE_URL``
is set. **Postgres is where this bug actually lives** — SQLite silently coerced
bool->0/1, so only the Postgres run proves the ``DatatypeMismatch`` is gone:

    TEST_DATABASE_URL=postgresql+psycopg://… pytest tests/test_full_art_boolean.py
"""

from __future__ import annotations

import pytest
from sqlalchemy import Boolean, text

from app.legacy_tables import scryfall_cards
from app.scryfall import (
    _BULK_UPSERT_SQL,
    _CACHE_COLUMNS,
    _cached_row_to_payload,
    _normalize_card_payload,
)


def test_column_is_boolean():
    assert isinstance(scryfall_cards.c.full_art.type, Boolean)


@pytest.mark.parametrize(
    ("raw_full_art", "expected"),
    [(True, True), (False, False), (None, False)],  # None (missing) -> False
)
def test_upsert_roundtrips_full_art(db_engine, raw_full_art, expected):
    raw = {
        "id": "x-1",
        "name": "Test Card",
        "set": "tst",
        "set_name": "Test",
        "collector_number": "1",
        "rarity": "common",
        "type_line": "Land",
        "oracle_text": "",
        "prices": {"usd": "1.00"},
        "colors": [],
        "color_identity": [],
        "cmc": 0.0,
        "legalities": {},
        "full_art": raw_full_art,
        "layout": "normal",
    }
    payload = _normalize_card_payload(raw)
    assert payload["full_art"] is expected  # write path produces a Python bool

    # db_engine already has the schema (create_all from legacy_tables, now Boolean).
    # On Postgres this upsert is exactly the path that raised DatatypeMismatch.
    with db_engine.begin() as conn:
        conn.execute(_BULK_UPSERT_SQL, payload)
        row = conn.execute(text(f"SELECT {_CACHE_COLUMNS} FROM scryfall_cards")).mappings().one()

    got = _cached_row_to_payload(row)
    assert got["full_art"] is expected
    assert isinstance(got["full_art"], bool)
