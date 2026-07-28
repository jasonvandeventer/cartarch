"""#133 — the decks table carries ONLY the compound (user_id, name) uniqueness.

The pre-v3.1.0 single-tenant standalone UNIQUE on decks.name died at the v4
cutover; this pins that the model (and thus create_all + the Alembic baseline it
mirrors) never re-introduces a global-unique name. Guards the removed
cross_user_deck_conflict workarounds from creeping back.

**#163 amended the MECHANISM, not the intent.** The compound uniqueness moved from a
table CONSTRAINT to a PARTIAL unique INDEX scoped ``WHERE retired_at IS NULL``,
because deck deletion became a soft retire — without the predicate a retired deck
would squat on its name and a user could not reuse a name they had just "deleted".
The assertions below therefore accept either form, and additionally pin that the
uniqueness is scoped to live decks.
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

    # the correct multi-user scope: compound (user_id, name) — as a table
    # constraint OR a unique index (#163 made it the latter, partial).
    compound_constraint = any(set(u["column_names"]) == {"user_id", "name"} for u in uniques)
    compound_index = any(
        ix.get("unique") and set(ix["column_names"]) == {"user_id", "name"} for ix in indexes
    )
    assert compound_constraint or compound_index, (uniques, indexes)

    # NO standalone unique on `name` alone — neither a table constraint...
    assert not any(u["column_names"] == ["name"] for u in uniques), uniques
    # ...nor a unique index (the legacy sqlite_autoindex / ix_decks_name-unique form)
    assert not any(ix.get("unique") and ix["column_names"] == ["name"] for ix in indexes), indexes

    # `name` still has a plain (non-unique) lookup index
    assert any(not ix.get("unique") and ix["column_names"] == ["name"] for ix in indexes), indexes


def test_the_compound_uniqueness_is_scoped_to_live_decks(db, user):
    """#163 — a RETIRED deck must not squat on its name.

    Deck deletion is now a soft retire, so without a partial predicate the user
    could never reuse a name they had "deleted" — turning an invisible change into
    a visible regression. Driven through real inserts rather than index
    introspection, because the predicate is what matters, not its spelling.
    """
    from app.models import Deck
    from app.timeutil import utc_now

    live = Deck(user_id=user.id, name="Atraxa")
    db.add(live)
    db.commit()

    live.retired_at = utc_now()
    db.commit()

    # The name is free again once retired.
    replacement = Deck(user_id=user.id, name="Atraxa")
    db.add(replacement)
    db.commit()

    assert replacement.id != live.id
    assert live.retired_at is not None
    assert replacement.retired_at is None


def test_two_LIVE_decks_still_cannot_share_a_name(db, user):
    """The other half — the predicate must not have disabled the constraint."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    from app.models import Deck

    db.add(Deck(user_id=user.id, name="Atraxa"))
    db.commit()

    db.add(Deck(user_id=user.id, name="Atraxa"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
