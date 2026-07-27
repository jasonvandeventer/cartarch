"""The first sorter rule warns before it makes every managed location a source.

`StorageLocation.mode` defaults to "managed", and `resort_collection` treats any
non-deck managed/sink location as a sortable SOURCE across the user's WHOLE
collection. So a user who quietly built boxes through the normal UI turns all of
them into sorter input the moment they add their first rule — a bulk relocation
of cards they had filed by hand, with nothing said beforehand.

Pinned here:
  - `first_sweep_preview` reports exactly the locations `resort_collection` would
    pull from: managed/sink, non-deck, non-empty
  - the create route REFUSES an unacknowledged first rule (the checkbox is an
    affordance; this is the guard)
  - acknowledging lets it through
  - LATER rules are never gated — the sweep was already consented to
  - a user with nothing sweepable is never nagged
  - the warning and its card counts actually render on the page
"""

from __future__ import annotations

from app.models import Card, InventoryRow, SorterRule, StorageLocation
from app.sorter_rule_service import first_sweep_preview


def _loc(db, user, name, *, mode="managed", type_="box"):
    loc = StorageLocation(user_id=user.id, name=name, type=type_, mode=mode)
    db.add(loc)
    db.commit()
    return loc


def _fill(db, user, loc, *, qty, tag="c"):
    card = Card(
        scryfall_id=f"{tag}-{loc.id}",
        name=f"Card {tag}{loc.id}",
        set_code="tst",
        collector_number="1",
    )
    db.add(card)
    db.commit()
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=card.id,
            finish="normal",
            quantity=qty,
            storage_location_id=loc.id,
            is_pending=False,
        )
    )
    db.commit()


def test_preview_lists_managed_locations_biggest_first(db, user):
    small = _loc(db, user, "Binder")
    big = _loc(db, user, "Bulk")
    _fill(db, user, small, qty=3, tag="s")
    _fill(db, user, big, qty=40, tag="b")

    preview = first_sweep_preview(db, user.id)

    assert [(p["location"].name, p["cards"]) for p in preview] == [("Bulk", 40), ("Binder", 3)]


def test_preview_skips_manual_deck_and_empty_locations(db, user):
    _loc(db, user, "Empty Box")  # managed but holds nothing
    manual = _loc(db, user, "Hand-filed", mode="manual")
    deck_loc = _loc(db, user, "Deck", type_="deck")
    _fill(db, user, manual, qty=5, tag="m")
    _fill(db, user, deck_loc, qty=5, tag="d")

    assert first_sweep_preview(db, user.id) == []


def test_preview_ignores_other_users(db, user):
    from app.models import User

    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.commit()
    theirs = _loc(db, other, "Their Bulk")
    _fill(db, other, theirs, qty=99, tag="o")

    assert first_sweep_preview(db, user.id) == []


def test_first_rule_is_refused_without_acknowledgement(client, db, user):
    box = _loc(db, user, "Bulk")
    _fill(db, user, box, qty=25, tag="b")
    target = _loc(db, user, "Target", mode="manual")

    resp = client.post(
        "/sorter-rules",
        data={"query": "t:land", "target_location_id": target.id},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "sweep_unack=1" in resp.headers["location"]
    assert db.query(SorterRule).count() == 0


def test_acknowledging_lets_the_first_rule_through(client, db, user):
    box = _loc(db, user, "Bulk")
    _fill(db, user, box, qty=25, tag="b")
    target = _loc(db, user, "Target", mode="manual")

    resp = client.post(
        "/sorter-rules",
        data={
            "query": "t:land",
            "target_location_id": target.id,
            "acknowledge_sweep": "1",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "sweep_unack" not in resp.headers["location"]
    assert db.query(SorterRule).count() == 1


def test_later_rules_are_not_gated(client, db, user):
    """Once the sorter is on, the sweep has been consented to — stop nagging."""
    box = _loc(db, user, "Bulk")
    _fill(db, user, box, qty=25, tag="b")
    target = _loc(db, user, "Target", mode="manual")
    db.add(SorterRule(user_id=user.id, query="t:goblin", target_location_id=target.id, position=0))
    db.commit()

    resp = client.post(
        "/sorter-rules",
        data={"query": "t:land", "target_location_id": target.id},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "sweep_unack" not in resp.headers["location"]
    assert db.query(SorterRule).count() == 2


def test_user_with_nothing_sweepable_is_not_gated(client, db, user):
    target = _loc(db, user, "Target", mode="manual")

    resp = client.post(
        "/sorter-rules",
        data={"query": "t:land", "target_location_id": target.id},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "sweep_unack" not in resp.headers["location"]
    assert db.query(SorterRule).count() == 1


def test_the_warning_renders_with_real_numbers(client, db, user):
    """Route-level: the counts have to reach the page, not just the service."""
    box = _loc(db, user, "Bulk")
    _fill(db, user, box, qty=2897, tag="b")

    body = client.get("/locations").text

    assert "first sorter rule" in body.lower()
    assert "2897" in body
    assert 'name="acknowledge_sweep"' in body


def test_no_warning_once_the_sorter_is_already_on(client, db, user):
    box = _loc(db, user, "Bulk")
    _fill(db, user, box, qty=2897, tag="b")
    target = _loc(db, user, "Target", mode="manual")
    db.add(SorterRule(user_id=user.id, query="", target_location_id=target.id, position=0))
    db.commit()

    body = client.get("/locations").text

    assert 'name="acknowledge_sweep"' not in body
