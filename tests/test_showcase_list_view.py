"""Showcase list view — the compact counterpart to the art-tile grid.

Same shape as the deck list view (2026-08-22 request), with one deliberate
difference recorded in the partial: ONE column, because a showcase row carries
an editable offered quantity, a price and two buttons.

The view-mode preference is STORED, never a URL axis — the deck version of this
shipped a dead-looking toggle by letting a `?view=` param and the stored pref
disagree, and there is exactly one writer here so they cannot.
"""

from __future__ import annotations

import re

from app.models import (
    Card,
    InventoryRow,
    Showcase,
    ShowcaseItem,
    ShowcaseLocationSource,
    StorageLocation,
)


def _seed(db, user, *, mirrored=False):
    """A showcase holding one CURATED card, plus optionally a mirrored location
    contributing a second card that has no ShowcaseItem of its own.

    The curated card lives in a DIFFERENT location from the mirrored box on
    purpose: a card that is both curated and mirrored is a third case with its
    own rules, and it has its own test below.
    """
    binder = StorageLocation(user_id=user.id, name="Binder", type="binder", mode="managed")
    db.add(binder)
    box = StorageLocation(user_id=user.id, name="Trade box", type="box", mode="manual")
    sc = Showcase(user_id=user.id, name="Trade binder")
    db.add_all([box, sc])
    db.flush()
    curated_card = Card(
        scryfall_id="sid-curated",
        name="Rhystic Study",
        set_code="csp",
        collector_number="33",
        type_line="Enchantment",
        image_url="https://img.example.invalid/x.jpg",
        price_usd="42.50",
    )
    db.add(curated_card)
    db.flush()
    curated_row = InventoryRow(
        user_id=user.id,
        card_id=curated_card.id,
        quantity=2,
        finish="foil",
        is_pending=False,
        storage_location_id=binder.id,
    )
    db.add(curated_row)
    db.flush()
    item = ShowcaseItem(showcase_id=sc.id, inventory_row_id=curated_row.id, quantity_offered=1)
    db.add(item)

    if mirrored:
        mirrored_card = Card(
            scryfall_id="sid-mirrored",
            name="Mystic Remora",
            set_code="ice",
            collector_number="64",
            type_line="Enchantment",
            image_url="https://img.example.invalid/y.jpg",
        )
        db.add(mirrored_card)
        db.flush()
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=mirrored_card.id,
                quantity=3,
                finish="normal",
                is_pending=False,
                storage_location_id=box.id,
            )
        )
        db.add(ShowcaseLocationSource(showcase_id=sc.id, storage_location_id=box.id))
    db.commit()
    return sc


def test_the_toggle_switches_the_render_and_the_choice_sticks(client, db, user):
    sc = _seed(db, user)

    grid = client.get(f"/showcase/{sc.id}")
    assert grid.status_code == 200
    assert "showcase-items-grid" in grid.text
    assert "showcase-list-row" not in grid.text

    switch = client.post(
        "/account/showcase-view-pref",
        data={"view": "list", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert switch.status_code == 303
    # Asserted on the current user rather than by re-reading the row: the test
    # harness pins get_current_user to a fixture object bound to a DIFFERENT
    # session than the request's, so the route's commit has nothing to flush
    # here. In production get_current_user loads the user from the request's own
    # session, so the same line persists. The behavioural assertion below is the
    # one that would catch a real break either way.
    assert user.showcase_view_mode == "list"

    listed = client.get(f"/showcase/{sc.id}")
    assert "showcase-list-row" in listed.text
    assert "showcase-items-grid" not in listed.text
    # The card is still fully identified without its art.
    assert "Rhystic Study" in listed.text
    assert "CSP #33" in listed.text
    # ...and the row carries no image of its own; the shared hover preview owns
    # that, which is the point of the view on a big showcase.
    rows = re.findall(r'<li class="showcase-list-row.*?</li>', listed.text, re.S)
    assert rows and not any("<img" in r for r in rows)


def test_the_view_is_a_preference_not_a_url_axis(client, db, user):
    """A `?view=` param must not steer the page. The deck toggle looked dead for
    a release because a param and the stored pref disagreed about who wins."""
    sc = _seed(db, user)
    page = client.get(f"/showcase/{sc.id}?view=list")
    assert "showcase-list-row" not in page.text, "a URL param must not change the view"
    assert "showcase-items-grid" in page.text


def test_an_unknown_view_value_is_ignored(client, db, user):
    _seed(db, user)
    client.post("/account/showcase-view-pref", data={"view": "spreadsheet", "csrf_token": "x"})
    assert user.showcase_view_mode == "grid"


def test_a_mirrored_card_offers_no_per_item_controls_in_EITHER_view(client, db, user):
    """A mirrored row has no ShowcaseItem, so `item.id` is None — there is
    nothing per-card to update or remove, and it leaves by dropping the location
    source instead.

    Both views rendered the controls anyway before this change, pointing at
    ``/showcase/items/None/quantity``: buttons that could only fail. Found while
    building the list view, fixed in the grid too, and pinned in both because
    the grid is where it shipped.
    """
    sc = _seed(db, user, mirrored=True)

    for mode in ("grid", "list"):
        client.post("/account/showcase-view-pref", data={"view": mode, "csrf_token": "x"})
        page = client.get(f"/showcase/{sc.id}")
        assert page.status_code == 200
        assert "Mystic Remora" in page.text, f"{mode}: the mirrored card must still be listed"
        assert "/showcase/items/None/" not in page.text, f"{mode}: dead control rendered"
        # The curated card keeps its real controls in the same render.
        item_id = db.query(ShowcaseItem).first().id
        assert f"/showcase/items/{item_id}/quantity" in page.text, (
            f"{mode}: the curated card lost its controls"
        )


def test_a_card_that_is_BOTH_curated_and_mirrored_also_hides_its_controls(client, db, user):
    """The third case, and the reason the gate is `mirrored` rather than `id`.

    Such a row keeps its ShowcaseItem — so it HAS an id and the controls would
    render — but the mirror wins on quantity (share_service: "the point of
    adding a location is that it stops being a hand-managed number"). Update
    would therefore write an offered quantity the page never shows again, and
    Remove would drop the curated item while the mirror kept the card exactly
    where it was: two controls that appear to work and do nothing visible.
    """
    box = StorageLocation(user_id=user.id, name="Trade box", type="box", mode="manual")
    sc = Showcase(user_id=user.id, name="Trade binder")
    db.add_all([box, sc])
    db.flush()
    card = Card(
        scryfall_id="sid-both",
        name="Smothering Tithe",
        set_code="rna",
        collector_number="22",
        type_line="Enchantment",
        image_url="https://img.example.invalid/z.jpg",
    )
    db.add(card)
    db.flush()
    row = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        quantity=4,
        finish="normal",
        is_pending=False,
        storage_location_id=box.id,
    )
    db.add(row)
    db.flush()
    item = ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1)
    db.add(item)
    db.add(ShowcaseLocationSource(showcase_id=sc.id, storage_location_id=box.id))
    db.commit()
    item_id = item.id

    for mode in ("grid", "list"):
        client.post("/account/showcase-view-pref", data={"view": mode, "csrf_token": "x"})
        page = client.get(f"/showcase/{sc.id}")
        assert page.status_code == 200
        assert "Smothering Tithe" in page.text, f"{mode}: the card must still be listed"
        assert f"/showcase/items/{item_id}/quantity" not in page.text, (
            f"{mode}: an Update the mirror would override"
        )
        assert f"/showcase/items/{item_id}/remove" not in page.text, (
            f"{mode}: a Remove that leaves the card in place"
        )
