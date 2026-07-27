"""Storage modes are labelled by consequence, and default to doing nothing (#159).

The stored values name the sorter's ROLE for a location ("managed", "sink"); the
user needs to know what happens to their CARDS. Combined with `mode` defaulting to
"managed", that produced four accounts holding sortable storage they never chose.

Pinned here:
  - stored values are UNCHANGED (this is a display-layer rename; every comparison
    site still reads "managed"/"sink"/"manual"/"ignored")
  - labels are consistent with the two sorter predicates — a label that says the
    sorter leaves a location alone must correspond to a non-source, non-target mode
  - a NEW location defaults to "manual" (do nothing)
  - **auto-created drawers stay "managed"** — the one site that relied on the old
    implicit default, and the way the default flip could have silently disabled
    the drawer sorter
  - the labels reach the page; the raw stored word does not
"""

from __future__ import annotations

import pytest

from app.location_service import (
    DEFAULT_LOCATION_MODE,
    LOCATION_MODE_CHOICES,
    SORTABLE_SOURCE_MODES,
    SORTABLE_TARGET_MODES,
    VALID_LOCATION_MODES,
    create_location,
    is_sortable_source,
    is_sortable_target,
    location_mode_badge,
    location_mode_label,
)
from app.models import StorageLocation


def test_stored_values_are_unchanged():
    """The rename is display-only. Changing these is a migration, not this issue."""
    assert VALID_LOCATION_MODES == {"managed", "manual", "sink", "ignored"}
    assert SORTABLE_TARGET_MODES == frozenset({"managed"})
    assert SORTABLE_SOURCE_MODES == frozenset({"managed", "sink"})


def test_every_valid_mode_has_exactly_one_label():
    labelled = [value for value, _l, _d, _b in LOCATION_MODE_CHOICES]
    assert sorted(labelled) == sorted(VALID_LOCATION_MODES)
    assert len(labelled) == len(set(labelled))


def test_labels_do_not_contradict_the_sorter_predicates():
    """A label promising the sorter leaves it alone must be a non-source mode.

    This is the check that matters: a wrong label is worse than the vague one it
    replaced, because a user acts on it.
    """
    for value, label, desc, _badge in LOCATION_MODE_CHOICES:
        loc = StorageLocation(user_id=1, name="x", type="box", mode=value)
        promises_hands_off = "leave" in label.lower()
        if promises_hands_off:
            assert not is_sortable_source(loc), f"{value}: label promises hands-off but IS a source"
            assert not is_sortable_target(loc), f"{value}: label promises hands-off but IS a target"
        # Any mode the sorter can empty must say so somewhere the user reads.
        if is_sortable_source(loc):
            blurb = (label + " " + desc).lower()
            assert any(w in blurb for w in ("take", "empty", "pull")), (
                f"{value}: is a sorter SOURCE but neither label nor description says so"
            )


def test_the_safe_option_is_first_and_is_the_default():
    assert LOCATION_MODE_CHOICES[0][0] == "manual"
    assert DEFAULT_LOCATION_MODE == "manual"
    inert = StorageLocation(user_id=1, name="x", type="box", mode=DEFAULT_LOCATION_MODE)
    assert not is_sortable_source(inert)
    assert not is_sortable_target(inert)


def test_unknown_mode_falls_back_to_the_raw_value():
    assert location_mode_label("something-new") == "something-new"
    assert location_mode_badge("something-new") == "something-new"


def test_badges_are_short_enough_to_scan_in_a_table():
    """The badge is the at-a-glance risk indicator; a sentence defeats it."""
    for value, _label, _desc, badge in LOCATION_MODE_CHOICES:
        assert len(badge) <= 24, f"{value}: badge too long for the Mode column ({badge!r})"


def test_a_new_location_does_nothing_by_default(db, user):
    loc = create_location(db, user.id, name="Shoebox", type="box")
    assert loc.mode == "manual"
    assert not is_sortable_source(loc)


def test_an_explicit_mode_still_wins(db, user):
    loc = create_location(db, user.id, name="Sorted Box", type="box", mode="managed")
    assert loc.mode == "managed"
    assert is_sortable_source(loc)


def test_model_level_default_is_also_manual(db, user):
    """Not just the service default — a raw StorageLocation() must be inert too."""
    loc = StorageLocation(user_id=user.id, name="Raw", type="box")
    db.add(loc)
    db.commit()
    assert loc.mode == "manual"


def test_auto_created_drawers_are_still_managed(db, user):
    """THE regression this flip could have caused, silently.

    `_get_or_create_drawer_location` was the only StorageLocation site relying on
    the implicit default. If it inherits "manual", a bootstrapped drawer is no
    longer a sorter target or source and the drawer sorter quietly stops working.
    """
    from app.inventory_service import _get_or_create_drawer_location

    drawer = _get_or_create_drawer_location(db, user.id, "3")
    db.commit()

    assert drawer.mode == "managed"
    assert is_sortable_target(drawer)
    assert is_sortable_source(drawer)


@pytest.mark.parametrize("mode", sorted(VALID_LOCATION_MODES))
def test_the_label_reaches_the_page_and_the_raw_word_does_not(client, db, user, mode):
    loc = StorageLocation(user_id=user.id, name=f"Box {mode}", type="box", mode=mode)
    db.add(loc)
    db.commit()

    body = client.get("/locations").text

    # picker label in the edit popout, short badge in the table
    assert location_mode_label(mode) in body
    assert location_mode_badge(mode) in body
    # The badge used to render the bare stored word; it must not anymore.
    assert f'location-mode-{mode}">{mode}<' not in body
