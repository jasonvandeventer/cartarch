"""#170 — the commander picker renders one row per NAME, not per printing.

Deduping on `card.id` showed a legend once per printing, and the picker renders
only name and type line, so the extra rows were textually IDENTICAL. **Alt-art and
showcase treatments within ONE set** are what make this indistinguishable rather
than merely redundant — three rows of "Merry, Esquire of Rohan" from `ltr, ltr,
ltr` with nothing to tell them apart, each linking to a different preview.

Safe to collapse because the brew is printing-invariant: `generate_recommendation`
reads the commander for colour identity, themes and legality only. The chosen
`card_id` decides the hover image and which physical copy a brew references —
hence the representative-printing rule, reusing #119's rule 2 rather than
inventing one.
"""

from __future__ import annotations

import itertools

from app.models import Card, Deck, InventoryRow, StorageLocation
from app.recommendation_service import list_commander_candidates

_seq = itertools.count(1)

_LEGEND = "Legendary Creature — Merfolk Druid"


def _printing(db, name, set_code, *, type_line=_LEGEND):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code=set_code,
        collector_number=str(next(_seq)),
        type_line=type_line,
        legalities='{"commander": "legal"}',
    )
    db.add(c)
    db.commit()
    return c


def _own(db, user, card, *, location=None, proxy=False):
    r = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        quantity=1,
        finish="normal",
        is_proxy=proxy,
        is_pending=False,
        storage_location_id=location.id if location else None,
    )
    db.add(r)
    db.commit()
    return r


def _location(db, user, name, type_):
    loc = StorageLocation(user_id=user.id, name=name, type=type_, mode="manual")
    db.add(loc)
    db.commit()
    return loc


def test_two_printings_of_one_commander_collapse_to_one_row(db, user):
    a = _printing(db, "Tatyova, Benthic Druid", "cmm")
    b = _printing(db, "Tatyova, Benthic Druid", "m3c")
    _own(db, user, a)
    _own(db, user, b)

    rows = list_commander_candidates(db, user.id)
    assert [c.name for c in rows] == ["Tatyova, Benthic Druid"]
    assert rows[0].printing_count == 2


def test_two_printings_INSIDE_ONE_SET_also_collapse(db, user):
    """The case that makes this a defect rather than untidiness.

    Alt-art and showcase treatments share a set code, so the rows are identical
    on every field the picker renders — set code would not have distinguished
    them even if it were shown.
    """
    a = _printing(db, "Merry, Esquire of Rohan", "ltr", type_line="Legendary Creature — Halfling")
    b = _printing(db, "Merry, Esquire of Rohan", "ltr", type_line="Legendary Creature — Halfling")
    c = _printing(db, "Merry, Esquire of Rohan", "ltr", type_line="Legendary Creature — Halfling")
    for card in (a, b, c):
        _own(db, user, card)

    rows = list_commander_candidates(db, user.id)
    assert len(rows) == 1
    assert rows[0].printing_count == 3


def test_a_single_printing_reports_one_and_the_template_stays_quiet(db, user):
    _own(db, user, _printing(db, "Solitary Legend", "abc"))
    (row,) = list_commander_candidates(db, user.id)
    assert row.printing_count == 1


def test_distinct_commanders_are_not_merged(db, user):
    _own(db, user, _printing(db, "Alpha Legend", "abc"))
    _own(db, user, _printing(db, "Beta Legend", "abc"))
    assert [c.name for c in list_commander_candidates(db, user.id)] == [
        "Alpha Legend",
        "Beta Legend",
    ]


# ── Representative printing: #119 rule 2, prefer an owned LOOSE copy ─────────


def test_the_representative_printing_prefers_a_loose_copy_over_a_deck_resident_one(db, user):
    """#119 rule 2's spirit: the copy you can actually pick up wins.

    Order of creation is deliberately deck-first, so a naive "first row wins"
    would return the deck copy and this test would fail.
    """
    deck_loc = _location(db, user, "Some Deck", "deck")
    db.add(Deck(user_id=user.id, name="Some Deck", storage_location_id=deck_loc.id))
    db.commit()
    binder = _location(db, user, "Binder", "binder")

    in_deck = _printing(db, "Etali, Primal Storm", "rix")
    loose = _printing(db, "Etali, Primal Storm", "blc")
    _own(db, user, in_deck, location=deck_loc)
    _own(db, user, loose, location=binder)

    (row,) = list_commander_candidates(db, user.id)
    assert row.id == loose.id, "a loose copy must beat a deck-resident one"
    assert row.printing_count == 2


def test_a_commander_owned_only_inside_a_deck_is_still_offered(db, user):
    """Falls back rather than dropping off the list — you may well want to brew
    around that commander again."""
    deck_loc = _location(db, user, "Only Deck", "deck")
    db.add(Deck(user_id=user.id, name="Only Deck", storage_location_id=deck_loc.id))
    db.commit()
    card = _printing(db, "Deck Bound Legend", "abc")
    _own(db, user, card, location=deck_loc)

    (row,) = list_commander_candidates(db, user.id)
    assert row.id == card.id


def test_the_choice_is_stable_across_calls(db, user):
    """The picker must not shuffle which printing it shows between page loads —
    the hover image and the referenced physical copy both hang off it."""
    for set_code in ("aaa", "bbb", "ccc"):
        _own(db, user, _printing(db, "Stable Legend", set_code))
    first = list_commander_candidates(db, user.id)[0].id
    for _ in range(3):
        assert list_commander_candidates(db, user.id)[0].id == first


# ── Unchanged behaviour ─────────────────────────────────────────────────────


def test_proxies_are_still_excluded(db, user):
    _own(db, user, _printing(db, "Proxy Only Legend", "abc"), proxy=True)
    assert list_commander_candidates(db, user.id) == []


def test_a_non_commander_is_still_excluded(db, user):
    _own(db, user, _printing(db, "Just A Bear", "abc", type_line="Creature — Bear"))
    assert list_commander_candidates(db, user.id) == []


def test_another_users_copies_are_not_counted_as_printings(db, user):
    from app.models import User

    other = User(username=f"cp{next(_seq)}@ex.com", password_hash="x")
    db.add(other)
    db.commit()

    mine = _printing(db, "Shared Name Legend", "aaa")
    theirs = _printing(db, "Shared Name Legend", "bbb")
    _own(db, user, mine)
    _own(db, other, theirs)

    (row,) = list_commander_candidates(db, user.id)
    assert row.id == mine.id
    assert row.printing_count == 1, "another user's printing must not inflate the count"


# ── The page, and the safety argument ───────────────────────────────────────


def test_the_collapse_is_STATED_on_the_page_not_silent(client, db, user):
    """Route-level, because `printing_count` has to reach the template.

    Silently dropping printings a user knows they own reads as a bug in the other
    direction — "where did my other two Tatyovas go".
    """
    for set_code in ("cmm", "m3c", "fdn"):
        _own(db, user, _printing(db, "Tatyova, Benthic Druid", set_code))
    _own(db, user, _printing(db, "Solitary Legend", "abc"))

    body = client.get("/recommendations/commander").text
    assert "3 printings" in body
    # Count ROWS, not name occurrences: each row emits the name twice (the
    # `data-filter-text` attribute and the link text), so a bare name count reads
    # 2 for one row and would have passed at 3 rows too if written as `> 1`.
    assert body.count('data-filter-text="Tatyova, Benthic Druid') == 1
    # A one-printing row must stay quiet — "· 1 printings" would be noise on
    # almost every row.
    assert "1 printings" not in body


def test_the_brew_is_printing_invariant(db, user):
    """The safety argument for collapsing at all, pinned rather than asserted.

    `generate_recommendation` reads the commander only for oracle-level
    properties, so which printing the picker happened to choose cannot change the
    deck. If this ever stops holding, the collapse becomes a real information loss
    and this test is where that surfaces.
    """
    from app.recommendation_service import DeckBuildIntent, generate_recommendation

    a = _printing(db, "Tatyova, Benthic Druid", "cmm")
    b = _printing(db, "Tatyova, Benthic Druid", "m3c")
    _own(db, user, a)
    _own(db, user, b)
    for i in range(40):
        c = _printing(db, f"Filler Forest {i}", "abc", type_line="Basic Land — Forest")
        _own(db, user, c)

    def _names(card_id):
        rec = generate_recommendation(db, user.id, DeckBuildIntent(commander_card_id=card_id))
        return [c.name for c in rec.mainboard], [c.name for c in rec.lands]

    assert _names(a.id) == _names(b.id)
