"""#135 — showcases mirror a StorageLocation live (locked 2026-06-14 Cluster C).

The bug this closes is an ASYMMETRY, and the recon refined it before the fix
existed: a ShowcaseItem keys on ``inventory_row_id`` with no location, so a card
MOVED out of the box never left the showcase (the row survives a move), and a
card added to the box never joined. "Removing it from the box dropped it" was
only ever true when the row was DELETED or merged away.

Membership is now computed at read time, so both directions are live and there
are no sync hooks to forget. **The privacy projection is the first-class concern
here** — mirroring changes where membership comes from, and must not widen by a
single field what a share exposes.
"""

from __future__ import annotations

import pytest

from app import share_service
from app.models import (
    Card,
    InventoryRow,
    Showcase,
    ShowcaseItem,
    ShowcaseLocationSource,
    StorageLocation,
)


@pytest.fixture
def showcase(db, user):
    sc = Showcase(user_id=user.id, name="Trade binder")
    db.add(sc)
    db.commit()
    return sc


@pytest.fixture
def box(db, user):
    loc = StorageLocation(user_id=user.id, name="Trade box", type="box", mode="manual")
    db.add(loc)
    db.commit()
    return loc


def _card(db, name):
    c = Card(
        scryfall_id=f"sid-{name}",
        name=name,
        set_code="abc",
        collector_number="1",
        type_line="Creature",
        color_identity="G",
    )
    db.add(c)
    db.commit()
    return c


def _row(db, user, card, loc, *, qty=1, proxy=False, pending=False, notes=None):
    r = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        quantity=qty,
        finish="normal",
        is_proxy=proxy,
        is_pending=pending,
        storage_location_id=loc.id if loc else None,
        notes=notes,
    )
    db.add(r)
    db.commit()
    return r


# --------------------------------------------------------------------------
# The asymmetry the issue is actually about.
# --------------------------------------------------------------------------


def test_a_card_added_to_a_mirrored_location_appears(db, user, showcase, box):
    """Half the bug: nothing ever ADDED items when cards entered the location."""
    share_service.add_location_source(db, user.id, showcase.id, box.id)
    assert share_service.resolve_showcase_rows(db, showcase.id) == []

    _row(db, user, _card(db, "Llanowar Elves"), box)

    resolved = share_service.resolve_showcase_rows(db, showcase.id)
    assert [e["row"].card.name for e in resolved] == ["Llanowar Elves"]
    assert resolved[0]["mirrored"] is True


def test_a_card_moved_OUT_of_a_mirrored_location_disappears(db, user, showcase, box):
    """The other half, and the one the original report was really about.

    A curated ShowcaseItem keys on ``inventory_row_id``, which SURVIVES a move —
    so moving a card out of the box left it in the showcase. Mirrored membership
    is a location predicate, so the move is the removal.
    """
    row = _row(db, user, _card(db, "Sol Ring"), box)
    share_service.add_location_source(db, user.id, showcase.id, box.id)
    assert len(share_service.resolve_showcase_rows(db, showcase.id)) == 1

    elsewhere = StorageLocation(user_id=user.id, name="Shelf", type="box", mode="manual")
    db.add(elsewhere)
    db.commit()
    row.storage_location_id = elsewhere.id
    db.commit()

    assert share_service.resolve_showcase_rows(db, showcase.id) == []


def test_stopping_the_mirror_removes_its_cards_immediately(db, user, showcase, box):
    """Nothing was copied, so nothing is left behind."""
    _row(db, user, _card(db, "Sol Ring"), box)
    share_service.add_location_source(db, user.id, showcase.id, box.id)
    assert len(share_service.resolve_showcase_rows(db, showcase.id)) == 1

    assert share_service.remove_location_source(db, user.id, showcase.id, box.id) is True
    assert share_service.resolve_showcase_rows(db, showcase.id) == []


def test_hand_picked_items_survive_the_mirror_being_removed(db, user, showcase, box):
    """Curated and mirrored are structurally different — that is the design."""
    row = _row(db, user, _card(db, "Sol Ring"), box)
    db.add(ShowcaseItem(showcase_id=showcase.id, inventory_row_id=row.id, quantity_offered=1))
    db.commit()
    share_service.add_location_source(db, user.id, showcase.id, box.id)

    share_service.remove_location_source(db, user.id, showcase.id, box.id)
    resolved = share_service.resolve_showcase_rows(db, showcase.id)
    assert len(resolved) == 1
    assert resolved[0]["mirrored"] is False


# --------------------------------------------------------------------------
# Dedup and the mirror-wins rule.
# --------------------------------------------------------------------------


def test_a_row_both_curated_and_mirrored_appears_ONCE_and_the_mirror_wins(db, user, showcase, box):
    """Locked decision: dedup by ``inventory_row_id``, mirror wins on quantity.

    The curated item offers 1 of 3; the mirror shows the live 3. Adding the
    location is a statement that the box's contents are the offer, so a stale
    hand-set number must not override it.
    """
    row = _row(db, user, _card(db, "Sol Ring"), box, qty=3)
    db.add(ShowcaseItem(showcase_id=showcase.id, inventory_row_id=row.id, quantity_offered=1))
    db.commit()
    share_service.add_location_source(db, user.id, showcase.id, box.id)

    resolved = share_service.resolve_showcase_rows(db, showcase.id)
    assert len(resolved) == 1, "a row in both representations must appear once"
    assert resolved[0]["mirrored"] is True
    assert resolved[0]["offered"] == 3


# --------------------------------------------------------------------------
# What a mirror must NOT pull in.
# --------------------------------------------------------------------------


def test_pending_rows_are_not_mirrored(db, user, showcase, box):
    """A pending row is not filed anywhere yet; offering it would be a lie."""
    _row(db, user, _card(db, "Unplaced"), box, pending=True)
    share_service.add_location_source(db, user.id, showcase.id, box.id)
    assert share_service.resolve_showcase_rows(db, showcase.id) == []


def test_brew_placeholders_are_not_mirrored(db, user, showcase):
    """A brew placeholder is a card the sharer does NOT own.

    Mirroring must not offer one merely because a brew deck's location happens
    to be mirrored — the same guard ``add_rows_to_showcase`` already applies.
    """
    from app.models import Deck

    deck_loc = StorageLocation(user_id=user.id, name="Brew", type="deck", mode="manual")
    db.add(deck_loc)
    db.commit()
    deck = Deck(user_id=user.id, storage_location_id=deck_loc.id, name="Brew", is_brew=True)
    db.add(deck)
    db.commit()
    _row(db, user, _card(db, "Fake Mox"), deck_loc, proxy=True)

    # A deck location cannot even be mirrored, which is the first guard...
    assert share_service.add_location_source(db, user.id, showcase.id, deck_loc.id) is None
    # ...and the resolver's exclusion is the second.
    src = ShowcaseLocationSource(showcase_id=showcase.id, storage_location_id=deck_loc.id)
    db.add(src)
    db.commit()
    assert share_service.resolve_showcase_rows(db, showcase.id) == []


def test_another_users_location_cannot_be_mirrored(db, user, showcase):
    """Ownership is checked on BOTH sides.

    A forged location id would otherwise mirror somebody else's box into a
    showcase the caller shares to a playgroup — a disclosure with extra steps.
    """
    from app.models import User

    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.commit()
    theirs = StorageLocation(user_id=other.id, name="Theirs", type="box", mode="manual")
    db.add(theirs)
    db.commit()

    assert share_service.add_location_source(db, user.id, showcase.id, theirs.id) is None


def test_another_users_showcase_cannot_be_targeted(db, user, box):
    from app.models import User

    other = User(username="other2@example.com", password_hash="x")
    db.add(other)
    db.commit()
    theirs = Showcase(user_id=other.id, name="Theirs")
    db.add(theirs)
    db.commit()

    assert share_service.add_location_source(db, user.id, theirs.id, box.id) is None


# --------------------------------------------------------------------------
# THE privacy boundary. Mirroring changes where membership comes from and must
# not widen by one field what a share exposes.
# --------------------------------------------------------------------------


_FORBIDDEN = (
    "notes",
    "tags",
    "role",
    "is_pending",
    "storage_location_id",
    "drawer",
    "slot",
    "from_drawer",
    "from_slot",
    "user_id",
)


def test_a_mirrored_row_goes_through_the_SAME_sanitized_projection(db, user, showcase, box):
    """§8's hard-flag list, asserted against a MIRRORED row.

    A mirrored row reaches the projection as an ordinary InventoryRow, and it
    carries no ShowcaseItem — so there is no ``ShowcaseItem.notes`` to leak in
    the first place. This pins that the row's OWN private columns stay out too.
    """
    _row(db, user, _card(db, "Sol Ring"), box, notes="bought at LGS, paid too much")
    share_service.add_location_source(db, user.id, showcase.id, box.id)

    display = share_service.build_share_display_items(db, showcase)
    assert len(display) == 1
    for field in _FORBIDDEN:
        assert field not in display[0], f"{field} leaked into the share projection"
    assert "bought at LGS" not in repr(display[0])


def test_the_owner_view_and_the_share_view_agree_on_membership(db, user, showcase, box):
    """ONE resolver, two surfaces — the #156 lesson.

    Two surfaces answering the same question with separate queries is how they
    come to disagree silently; membership is asked once.
    """
    for name in ("Alpha", "Beta", "Gamma"):
        _row(db, user, _card(db, name), box)
    share_service.add_location_source(db, user.id, showcase.id, box.id)

    owner = share_service.get_showcase_with_items(db, user.id, showcase.id)
    shared = share_service.build_share_display_items(db, showcase)

    assert sorted(i["card"].name for i in owner["items"]) == ["Alpha", "Beta", "Gamma"]
    assert sorted(i["card"].name for i in shared) == ["Alpha", "Beta", "Gamma"]


def test_a_mirrored_proxy_is_valued_at_zero(db, user, showcase, box):
    """ADR proxy-valuation-2026-06-12 applies to mirrored rows as much as picked ones."""
    _row(db, user, _card(db, "Fake Bolt"), box, proxy=True)
    share_service.add_location_source(db, user.id, showcase.id, box.id)

    display = share_service.build_share_display_items(db, showcase)
    assert display[0]["effective_price"] == 0.0
    assert display[0]["is_proxy"] is True


# --------------------------------------------------------------------------
# Lifecycle.
# --------------------------------------------------------------------------


def test_mirroring_is_idempotent(db, user, showcase, box):
    a = share_service.add_location_source(db, user.id, showcase.id, box.id)
    b = share_service.add_location_source(db, user.id, showcase.id, box.id)
    assert a.id == b.id
    assert db.query(ShowcaseLocationSource).count() == 1


def test_deleting_the_location_drops_the_source(db, user, showcase, box):
    """SQLite runs FKs OFF, so this is explicit in ``delete_location`` — a
    dangling source would silently mirror nothing."""
    from app.location_service import delete_location

    share_service.add_location_source(db, user.id, showcase.id, box.id)
    delete_location(db, location_id=box.id, user_id=user.id)

    assert db.query(ShowcaseLocationSource).count() == 0


def test_deleting_the_showcase_drops_its_sources(db, user, showcase, box):
    """ORM-level cascade, which is the one that actually fires with FKs OFF."""
    share_service.add_location_source(db, user.id, showcase.id, box.id)
    share_service.delete_showcase(db, user.id, showcase.id)

    assert db.query(ShowcaseLocationSource).count() == 0


def test_the_row_count_matches_what_the_mirror_contributes(db, user, showcase, box):
    """The page's count and the showcase's contents must be the same predicate."""
    _row(db, user, _card(db, "Real"), box)
    _row(db, user, _card(db, "Pending"), box, pending=True)
    src = share_service.add_location_source(db, user.id, showcase.id, box.id)

    assert share_service.location_source_row_count(db, src) == 1
    assert len(share_service.resolve_showcase_rows(db, showcase.id)) == 1
