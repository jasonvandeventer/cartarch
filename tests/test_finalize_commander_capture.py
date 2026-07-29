"""#175 — recording a result is the last chance to capture what people played.

Game creation offers a deck picker and a commander field (#164), and #165 lets a
player set their own from a phone. All three are skippable, and measured over 23
games that produced **30 of 94 finalized Commander seats with no deck — 29 of them
with no commander name and no deck name either.** Nothing recorded means nothing a
backfill can resolve; #164's already took every seat that recorded *something*.

Finalize is the backstop, because it is the one moment somebody is definitely
typing. The field is deliberately narrow: it appears only where there is nothing,
so it never becomes a form people learn to dismiss.
"""

from __future__ import annotations

import itertools

import pytest

from app.game_service import attach_seat_commanders
from app.models import Card, Deck, DeckCommander, Game, GameSeat, StorageLocation, User

_seq = itertools.count(1)


def _game(db, owner, seats=2, status="in_progress"):
    g = Game(user_id=owner.id, format="Commander", status=status)
    db.add(g)
    db.commit()
    for i in range(1, seats + 1):
        db.add(
            GameSeat(
                game_id=g.id,
                seat_number=i,
                player_name=f"Player {i}",
                starting_life=40,
                user_id=owner.id if i == 1 else None,
            )
        )
    db.commit()
    return g


def _commander(db, user, name, *, owned=True):
    card = Card(
        scryfall_id=f"fc-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line="Legendary Creature — Human",
    )
    db.add(card)
    db.commit()
    if owned:
        loc = StorageLocation(user_id=user.id, name=f"D{next(_seq)}", type="deck", mode="manual")
        db.add(loc)
        db.commit()
        d = Deck(user_id=user.id, name=name, storage_location_id=loc.id)
        db.add(d)
        db.commit()
        db.add(DeckCommander(deck_id=d.id, card_id=card.id))
        db.commit()
        return card, d
    return card, None


# ── The capture ─────────────────────────────────────────────────────────────


def test_a_typed_commander_links_the_existing_deck(db, user):
    """FIND before CREATE — #164's rule, reused rather than reimplemented."""
    _card, deck = _commander(db, user, "Gorma, the Gullet")
    g = _game(db, user)
    seat = g.seats[0]

    assert attach_seat_commanders(db, g.id, user.id, {seat.id: "Gorma, the Gullet"}) == []
    db.refresh(seat)
    assert seat.deck_id == deck.id
    assert seat.commander_name_at_game == "Gorma, the Gullet"
    assert seat.deck_name_at_game == deck.name


def test_an_unknown_commander_creates_a_placeholder(db, user):
    _card, _ = _commander(db, user, "Auntie Ool, Cursewretch", owned=False)
    g = _game(db, user)
    seat = g.seats[0]
    before = db.query(Deck).count()

    assert attach_seat_commanders(db, g.id, user.id, {seat.id: "Auntie Ool, Cursewretch"}) == []
    db.refresh(seat)
    assert db.query(Deck).count() == before + 1
    assert seat.deck_id is not None


def test_a_name_the_catalog_does_not_have_is_reported_and_the_seat_stays_blank(db, user):
    """Never fail a finalize over an attribution problem — the same non-blocking
    posture creation and claiming take. A FLAVOR name lands here."""
    g = _game(db, user)
    seat = g.seats[0]

    missing = attach_seat_commanders(db, g.id, user.id, {seat.id: "Buttercup, Provincial Princess"})
    assert missing == ["Buttercup, Provincial Princess"]
    db.refresh(seat)
    assert seat.deck_id is None


# ── Guards ──────────────────────────────────────────────────────────────────


def test_a_seat_that_already_has_a_deck_is_never_overwritten(db, user):
    """The form does not render the field for such a seat, but a stray or forged
    one must not be able to rewrite a recorded result. Silently changing what
    somebody played is worse than dropping the input."""
    _c1, keep = _commander(db, user, "Original Commander")
    _c2, _other = _commander(db, user, "Different Commander")
    g = _game(db, user)
    seat = g.seats[0]
    seat.deck_id = keep.id
    db.commit()

    assert attach_seat_commanders(db, g.id, user.id, {seat.id: "Different Commander"}) == []
    db.refresh(seat)
    assert seat.deck_id == keep.id


def test_a_guest_seat_is_skipped_and_reported(db, user):
    """`decks.user_id` is NOT NULL — a seat with no user has nobody to own the
    deck. That is #167/#172 and is deliberately not pre-decided here."""
    _card, _ = _commander(db, user, "Guest Commander")
    g = _game(db, user)
    guest = g.seats[1]
    assert guest.user_id is None

    assert attach_seat_commanders(db, g.id, user.id, {guest.id: "Guest Commander"}) == [
        "Guest Commander"
    ]
    db.refresh(guest)
    assert guest.deck_id is None


def test_a_non_owner_changes_nothing(db, user):
    other = User(username=f"fc{next(_seq)}@ex.com", password_hash="x")
    db.add(other)
    db.commit()
    _card, _deck = _commander(db, user, "Owned Commander")
    g = _game(db, user)
    seat = g.seats[0]

    assert attach_seat_commanders(db, g.id, other.id, {seat.id: "Owned Commander"}) == []
    db.refresh(seat)
    assert seat.deck_id is None


def test_blank_and_whitespace_entries_are_ignored(db, user):
    g = _game(db, user)
    seat = g.seats[0]
    assert attach_seat_commanders(db, g.id, user.id, {seat.id: "   "}) == []
    db.refresh(seat)
    assert seat.deck_id is None


def test_resubmitting_does_not_mint_a_second_placeholder(db, user):
    """`/end` is deliberately re-runnable (#114 post-finalize editing), so this has
    to be idempotent. It is, because #164 finds before it creates — but that is a
    property of a different function, so pin it here."""
    g = _game(db, user)
    seat = g.seats[0]
    _card, _ = _commander(db, user, "Repeat Commander", owned=False)

    attach_seat_commanders(db, g.id, user.id, {seat.id: "Repeat Commander"})
    db.refresh(seat)
    first = seat.deck_id
    count = db.query(Deck).count()

    seat.deck_id = None  # simulate the second submit arriving at a still-blank seat
    db.commit()
    attach_seat_commanders(db, g.id, user.id, {seat.id: "Repeat Commander"})
    db.refresh(seat)

    assert db.query(Deck).count() == count
    assert seat.deck_id == first


# ── Both forms carry it, and the banner reaches the page it lands on ────────


@pytest.mark.parametrize("status", ["in_progress", "finalized"])
def test_the_field_renders_for_a_seat_with_nothing_recorded(client, db, user, status):
    """Two forms POST to /end — the tracker and the post-finalize edit form. A field
    on only one is the four-path deck-card-list seam again."""
    g = _game(db, user, status=status)
    body = client.get(f"/games/{g.id}").text
    assert f'name="commander_{g.seats[0].id}"' in body


@pytest.mark.parametrize("status", ["in_progress", "finalized"])
def test_the_field_is_absent_once_the_seat_has_a_deck(client, db, user, status):
    """A field on every seat forever is a form people learn to dismiss."""
    _card, deck = _commander(db, user, "Already Known")
    g = _game(db, user, status=status)
    g.seats[0].deck_id = deck.id
    g.seats[0].commander_name_at_game = "Already Known"
    db.commit()
    assert f'name="commander_{g.seats[0].id}"' not in client.get(f"/games/{g.id}").text


def test_the_unresolved_banner_renders_on_the_finalized_page(client, db, user):
    """The redirect after a finalize lands on game_summary.html, NOT game_detail —
    the game is finalized by then. A banner on only game_detail fires exactly where
    nobody can see it."""
    g = _game(db, user, status="finalized")
    body = client.get(f"/games/{g.id}?commander_unresolved=Buttercup").text
    assert "Couldn't find:" in body
    assert "Buttercup" in body


def test_finalizing_through_the_route_attributes_the_seat(client, db, user):
    """Route-level: the form field has to reach the service (#152's failure mode)."""
    _card, deck = _commander(db, user, "Route Commander")
    g = _game(db, user)
    seat = g.seats[0]
    db.commit()

    r = client.post(
        f"/games/{g.id}/end",
        data={f"placement_{seat.id}": "1", f"commander_{seat.id}": "Route Commander"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.refresh(seat)
    assert seat.deck_id == deck.id


def test_an_unresolved_name_still_records_the_result(client, db, user):
    """The result is the thing that must never be lost."""
    g = _game(db, user)
    seat = g.seats[0]
    db.commit()

    r = client.post(
        f"/games/{g.id}/end",
        data={f"placement_{seat.id}": "1", f"commander_{seat.id}": "Nonexistent Legend"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "commander_unresolved" in r.headers["location"]
    db.refresh(seat)
    assert seat.placement == 1
    assert seat.deck_id is None


# ── The suggestion list reaches the form that needs it most ─────────────────
# Reported 2026-07-29: the Wolverine failure happened HERE, on the summary form,
# and the box offered nothing to pick.


def _bulk_commander(db, name):
    from app.legacy_tables import scryfall_cards

    db.execute(
        scryfall_cards.insert().values(
            scryfall_id=f"bulk-{name[:8]}",
            name=name,
            set_code="mar",
            set_name="Marvel",
            collector_number="97",
            type_line="Legendary Creature — Mutant Berserker Hero",
        )
    )
    db.commit()


def test_the_played_box_suggests_commanders(client, db, user):
    game = _game(db, user, status="finalized")
    _bulk_commander(db, "Wolverine, Best There Is")

    body = client.get(f"/games/{game.id}").text

    assert 'list="commander-options"' in body
    assert "Wolverine, Best There Is" in body


def test_a_game_with_nothing_to_attribute_ships_no_option_list(client, db, user):
    """~196 KB of options nobody can use is not a free default."""
    game = _game(db, user, status="finalized")
    _bulk_commander(db, "Wolverine, Best There Is")
    for seat in game.seats:
        seat.commander_name_at_game = "Atraxa, Praetors' Voice"
    db.commit()

    body = client.get(f"/games/{game.id}").text

    assert "Wolverine, Best There Is" not in body
