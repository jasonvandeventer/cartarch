"""#133 — the decks table carries ONLY the compound (user_id, name) uniqueness.

The pre-v3.1.0 single-tenant standalone UNIQUE on decks.name died at the v4
cutover; this pins that the model (and thus create_all + the Alembic baseline it
mirrors) never re-introduces a global-unique name. Guards the removed
cross_user_deck_conflict workarounds from creeping back.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from app.db import Base


def test_decks_name_is_not_globally_unique():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)

    uniques = insp.get_unique_constraints("decks")
    indexes = insp.get_indexes("decks")

    # the correct multi-user scope: compound (user_id, name)
    assert any(set(u["column_names"]) == {"user_id", "name"} for u in uniques), uniques

    # NO standalone unique on `name` alone — neither a table constraint...
    assert not any(u["column_names"] == ["name"] for u in uniques), uniques
    # ...nor a unique index (the legacy sqlite_autoindex / ix_decks_name-unique form)
    assert not any(ix.get("unique") and ix["column_names"] == ["name"] for ix in indexes), indexes

    # `name` still has a plain (non-unique) lookup index
    assert any(not ix.get("unique") and ix["column_names"] == ["name"] for ix in indexes), indexes
