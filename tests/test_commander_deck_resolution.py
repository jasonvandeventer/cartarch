"""#164 — a commander name is enough to create a deck.

Deck linkage on finalized Commander seats was 57 of 94. Eight seats named a deck that
was never registered, because there was nothing to link to: the picker only offered
decks that already existed. Typing a commander now resolves to that commander's deck,
creating a **placeholder** (a deck row with a commander and no cards) if there isn't
one.

Three rules carry the weight:

* **Find before create.** `uq_decks_user_name` is UNIQUE per user, so a blind create
  raises on the second attempt. Matching is on the commander NAME SET, so it finds the
  deck however it was renamed — six decks in prod have been renamed with their
  commander constant.
* **Match on NAME, not `card_id`.** #163 anchors `deck_commanders` on `card_id`, which
  is a PRINTING. Two printings of Atraxa are different rows, so card-id set equality
  would fail to recognise the same commander. The catalog has no `oracle_id`, so the
  name is the oracle proxy — the same rule brew-mode matching uses.
* **An unmatched name creates NOTHING.** A flavor name lands there ("Buttercup,
  Provincial Princess" is Sisay, Weatherlight Captain) because Cartarch stores no
  flavor names. Owner decision 2026-07-27: fail visibly.
"""

from __future__ import annotations

from app import deck_service
from app.models import Card, Deck, DeckCommander, InventoryRow, StorageLocation, User


def _card(db, name, sid, set_code="tst", collector="1"):
    c = Card(
        scryfall_id=sid,
        name=name,
        set_code=set_code,
        collector_number=collector,
        type_line="Legendary Creature — Human Wizard",
    )
    db.add(c)
    db.commit()
    return c


def _own(db, user, card):
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=card.id,
            finish="normal",
            quantity=1,
            is_pending=False,
            is_proxy=False,
        )
    )
    db.commit()


def _deck_with_commander(db, user, deck_name, cards):
    loc = StorageLocation(user_id=user.id, name=deck_name, type="deck", mode="manual")
    db.add(loc)
    db.commit()
    d = Deck(user_id=user.id, name=deck_name, storage_location_id=loc.id)
    db.add(d)
    db.commit()
    for c in cards:
        db.add(DeckCommander(deck_id=d.id, card_id=c.id))
    db.commit()
    return d


def _names(db, deck):
    return {
        n.lower()
        for (n,) in db.query(Card.name)
        .join(DeckCommander, DeckCommander.card_id == Card.id)
        .filter(DeckCommander.deck_id == deck.id)
    }


# ── Create ──────────────────────────────────────────────────────────────────


def test_a_commander_name_creates_a_placeholder_deck(db, user):
    _card(db, "Atraxa, Praetors' Voice", "sc-atraxa")

    deck, missing = deck_service.resolve_commander_to_deck(db, user.id, "Atraxa, Praetors' Voice")

    assert missing == []
    assert deck is not None
    assert deck.contents_tracked is False
    assert _names(db, deck) == {"atraxa, praetors' voice"}


def test_a_placeholder_holds_no_cards(db, user):
    _card(db, "Atraxa, Praetors' Voice", "sc-atraxa")
    deck, _ = deck_service.resolve_commander_to_deck(db, user.id, "Atraxa, Praetors' Voice")

    rows = (
        db.query(InventoryRow)
        .filter(InventoryRow.storage_location_id == deck.storage_location_id)
        .count()
    )
    assert rows == 0


def test_name_matching_is_case_insensitive(db, user):
    _card(db, "Atraxa, Praetors' Voice", "sc-atraxa")

    deck, missing = deck_service.resolve_commander_to_deck(db, user.id, "atraxa, PRAETORS' voice")

    assert missing == [] and deck is not None


# ── Find before create ──────────────────────────────────────────────────────


def test_resolving_twice_returns_the_SAME_deck(db, user):
    """uq_decks_user_name is UNIQUE — a blind create would raise the second time."""
    _card(db, "Atraxa, Praetors' Voice", "sc-atraxa")

    first, _ = deck_service.resolve_commander_to_deck(db, user.id, "Atraxa, Praetors' Voice")
    second, _ = deck_service.resolve_commander_to_deck(db, user.id, "Atraxa, Praetors' Voice")

    assert first.id == second.id
    assert db.query(Deck).filter(Deck.user_id == user.id).count() == 1


def test_it_finds_a_deck_that_was_RENAMED(db, user):
    """The point of #163's anchor: six prod decks were renamed, commander constant."""
    atraxa = _card(db, "Atraxa, Praetors' Voice", "sc-atraxa")
    existing = _deck_with_commander(db, user, "Superfriends Deluxe", [atraxa])

    found, missing = deck_service.resolve_commander_to_deck(db, user.id, "Atraxa, Praetors' Voice")

    assert missing == []
    assert found.id == existing.id, "a renamed deck was not recognised"
    assert db.query(Deck).count() == 1


def test_it_matches_across_PRINTINGS_not_card_ids(db, user):
    """THE card_id trap: #163's anchor is per-printing, so id equality would miss."""
    printing_a = _card(db, "Atraxa, Praetors' Voice", "sc-a", set_code="cmd", collector="1")
    printing_b = _card(db, "Atraxa, Praetors' Voice", "sc-b", set_code="cmm", collector="2")
    existing = _deck_with_commander(db, user, "My Atraxa", [printing_a])
    _own(db, user, printing_b)  # they own the OTHER printing

    found, _ = deck_service.resolve_commander_to_deck(db, user.id, "Atraxa, Praetors' Voice")

    assert found.id == existing.id, "a different printing of the same commander split the deck"
    assert printing_a.id != printing_b.id


def test_a_retired_deck_is_not_matched(db, user):
    """#163 retires rather than deletes; a retired deck must not absorb the resolve."""
    from app.timeutil import utc_now

    atraxa = _card(db, "Atraxa, Praetors' Voice", "sc-atraxa")
    old = _deck_with_commander(db, user, "Old Atraxa", [atraxa])
    old.retired_at = utc_now()
    db.commit()

    found, _ = deck_service.resolve_commander_to_deck(db, user.id, "Atraxa, Praetors' Voice")

    assert found.id != old.id
    assert found.retired_at is None


def test_another_users_deck_is_never_matched(db, user):
    atraxa = _card(db, "Atraxa, Praetors' Voice", "sc-atraxa")
    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.commit()
    theirs = _deck_with_commander(db, other, "Their Atraxa", [atraxa])

    found, _ = deck_service.resolve_commander_to_deck(db, user.id, "Atraxa, Praetors' Voice")

    assert found.id != theirs.id
    assert found.user_id == user.id


# ── Partners: an order-independent SET ──────────────────────────────────────


def test_partners_resolve_regardless_of_the_order_typed(db, user):
    frodo = _card(db, "Frodo, Adventurous Hobbit", "sc-f")
    sam = _card(db, "Sam, Loyal Attendant", "sc-s")
    existing = _deck_with_commander(db, user, "Second Breakfast", [frodo, sam])

    a, _ = deck_service.resolve_commander_to_deck(
        db, user.id, "Frodo, Adventurous Hobbit + Sam, Loyal Attendant"
    )
    b, _ = deck_service.resolve_commander_to_deck(
        db, user.id, "Sam, Loyal Attendant + Frodo, Adventurous Hobbit"
    )

    assert a.id == b.id == existing.id, "partner order split one lineage in two"


def test_the_and_separator_also_splits(db, user):
    """`deck_name_at_game` uses 'and' in prod ('Frodo, ... and Sam, ...')."""
    frodo = _card(db, "Frodo, Adventurous Hobbit", "sc-f")
    sam = _card(db, "Sam, Loyal Attendant", "sc-s")
    existing = _deck_with_commander(db, user, "Second Breakfast", [frodo, sam])

    found, _ = deck_service.resolve_commander_to_deck(
        db, user.id, "Frodo, Adventurous Hobbit and Sam, Loyal Attendant"
    )
    assert found.id == existing.id


def test_a_partner_pair_does_not_match_a_single_commander_deck(db, user):
    """Set equality, not subset — one of the pair is a different deck."""
    frodo = _card(db, "Frodo, Adventurous Hobbit", "sc-f")
    _card(db, "Sam, Loyal Attendant", "sc-s")
    solo = _deck_with_commander(db, user, "Frodo Solo", [frodo])

    found, _ = deck_service.resolve_commander_to_deck(
        db, user.id, "Frodo, Adventurous Hobbit + Sam, Loyal Attendant"
    )
    assert found.id != solo.id


# ── Unmatched names create NOTHING ──────────────────────────────────────────


def test_an_unknown_name_creates_no_deck_and_reports_back(db, user):
    deck, missing = deck_service.resolve_commander_to_deck(
        db, user.id, "Buttercup, Provincial Princess"
    )

    assert deck is None
    assert missing == ["Buttercup, Provincial Princess"]
    assert db.query(Deck).count() == 0, "a placeholder was minted from an unmatched name"


def test_one_bad_name_in_a_pair_aborts_the_whole_resolve(db, user):
    """Half a partner pair is not a deck — better a blank than a wrong commander."""
    _card(db, "Frodo, Adventurous Hobbit", "sc-f")

    deck, missing = deck_service.resolve_commander_to_deck(
        db, user.id, "Frodo, Adventurous Hobbit + Not A Real Card"
    )

    assert deck is None
    assert missing == ["Not A Real Card"]
    assert db.query(Deck).count() == 0


def test_an_empty_entry_is_a_no_op(db, user):
    deck, missing = deck_service.resolve_commander_to_deck(db, user.id, "   ")
    assert deck is None and missing == []
    assert db.query(Deck).count() == 0


# ── Representative printing ─────────────────────────────────────────────────


def test_an_owned_printing_is_preferred(db, user):
    """resolve_add_printing's rule 2: if they hold a copy, that is the one they mean."""
    _card(db, "Atraxa, Praetors' Voice", "sc-a", set_code="cmd", collector="1")
    owned = _card(db, "Atraxa, Praetors' Voice", "sc-b", set_code="cmm", collector="2")
    _own(db, user, owned)

    deck, _ = deck_service.resolve_commander_to_deck(db, user.id, "Atraxa, Praetors' Voice")
    anchored = db.query(DeckCommander).filter(DeckCommander.deck_id == deck.id).one()

    assert anchored.card_id == owned.id


def test_an_unowned_commander_still_resolves(db, user):
    """They may own it only on paper — recording what was played is the point."""
    _card(db, "Atraxa, Praetors' Voice", "sc-a")

    deck, missing = deck_service.resolve_commander_to_deck(db, user.id, "Atraxa, Praetors' Voice")

    assert missing == [] and deck is not None


# ── The route ───────────────────────────────────────────────────────────────


def test_the_seat_form_offers_a_commander_field(client, db, user):
    body = client.get("/games/new").text
    assert 'name="commander_names"' in body


def test_creating_a_game_by_commander_links_the_seat(client, db, user):
    _card(db, "Atraxa, Praetors' Voice", "sc-atraxa")

    resp = client.post(
        "/games",
        data={
            "player_count": "1",
            "format": "Commander",
            "player_names": ["Solo"],
            "deck_ids": [""],
            "commander_names": ["Atraxa, Praetors' Voice"],
            "user_ids": [str(user.id)],
            "grid_positions": [""],
            "starting_life": "40",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "commander_unresolved" not in resp.headers["location"]
    from app.models import GameSeat

    seat = db.query(GameSeat).order_by(GameSeat.id.desc()).first()
    assert seat.deck_id is not None
    assert db.get(Deck, seat.deck_id).contents_tracked is False


def test_an_explicit_deck_selection_beats_a_typed_commander(db, user, client):
    """The field is a fallback, never an override."""
    atraxa = _card(db, "Atraxa, Praetors' Voice", "sc-atraxa")
    chosen = _deck_with_commander(db, user, "Chosen Deck", [atraxa])

    client.post(
        "/games",
        data={
            "player_count": "1",
            "format": "Commander",
            "player_names": ["Solo"],
            "deck_ids": [str(chosen.id)],
            "commander_names": ["Atraxa, Praetors' Voice"],
            "user_ids": [str(user.id)],
            "grid_positions": [""],
            "starting_life": "40",
        },
        follow_redirects=False,
    )

    from app.models import GameSeat

    seat = db.query(GameSeat).order_by(GameSeat.id.desc()).first()
    assert seat.deck_id == chosen.id


def test_an_unresolved_commander_still_creates_the_game_and_says_so(client, db, user):
    """Never fail a game over an attribution problem — but never hide it either."""
    resp = client.post(
        "/games",
        data={
            "player_count": "1",
            "format": "Commander",
            "player_names": ["Solo"],
            "deck_ids": [""],
            "commander_names": ["Buttercup, Provincial Princess"],
            "user_ids": [str(user.id)],
            "grid_positions": [""],
            "starting_life": "40",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "commander_unresolved" in resp.headers["location"]
    from app.models import Game, GameSeat

    assert db.query(Game).count() == 1
    assert db.query(GameSeat).order_by(GameSeat.id.desc()).first().deck_id is None
    assert db.query(Deck).count() == 0


# ── Backfill: the borrowed-deck ownership rule ──────────────────────────────


def test_the_backfill_credits_a_borrowed_deck_to_the_CARD_OWNER(db, user):
    """Game 27's shape: MasonRex piloted a precon whose commander is MURPGM's.

    Crediting the placeholder to the pilot would record a borrow as ownership. The
    rule fires only when exactly one user owns the commander and it is not the seat's
    user — see `_deck_owner_for`.
    """
    from app.models import Game, GameSeat
    from scripts.backfill_placeholder_decks import _deck_owner_for

    owner = User(username="deckowner@example.com", password_hash="x")
    db.add(owner)
    db.commit()
    zimone = _card(db, "Zimone, Infinite Analyst", "sc-z")
    _own(db, owner, zimone)  # only `owner` holds it

    g = Game(user_id=user.id, status="finalized")
    db.add(g)
    db.commit()
    seat = GameSeat(
        game_id=g.id,
        seat_number=1,
        player_name="Pilot",
        user_id=user.id,  # a DIFFERENT person piloted it
        deck_name_at_game="Quandrix Precon",
        commander_name_at_game="Zimone, Infinite Analyst",
    )
    db.add(seat)
    db.commit()

    owner_id, note = _deck_owner_for(db, seat)

    assert owner_id == owner.id, "the borrowed deck was credited to the pilot"
    assert "BORROWED" in note, "the inference must announce itself"


def test_the_borrowed_rule_does_NOT_fire_when_ownership_is_ambiguous(db, user):
    """Two owners is a tie, and a guess this thin must not be made on a tie."""
    from app.models import Game, GameSeat
    from scripts.backfill_placeholder_decks import _deck_owner_for

    other = User(username="second@example.com", password_hash="x")
    db.add(other)
    db.commit()
    card = _card(db, "Zimone, Infinite Analyst", "sc-z")
    _own(db, user, card)
    _own(db, other, card)

    g = Game(user_id=user.id, status="finalized")
    db.add(g)
    db.commit()
    seat = GameSeat(
        game_id=g.id,
        seat_number=1,
        player_name="Pilot",
        user_id=user.id,
        commander_name_at_game="Zimone, Infinite Analyst",
    )
    db.add(seat)
    db.commit()

    owner_id, note = _deck_owner_for(db, seat)

    assert owner_id == user.id
    assert note == ""


def test_the_borrowed_rule_does_NOT_fire_when_nobody_owns_the_commander(db, user):
    """Krang and Sisay are in this state in prod — the pilot keeps the deck."""
    from app.models import Game, GameSeat
    from scripts.backfill_placeholder_decks import _deck_owner_for

    _card(db, "Krang, Utrom Warlord", "sc-k")
    g = Game(user_id=user.id, status="finalized")
    db.add(g)
    db.commit()
    seat = GameSeat(
        game_id=g.id,
        seat_number=1,
        player_name="Pilot",
        user_id=user.id,
        commander_name_at_game="Krang, Utrom Warlord",
    )
    db.add(seat)
    db.commit()

    assert _deck_owner_for(db, seat) == (user.id, "")


# ── commit=False must actually mean no write ────────────────────────────────


def test_commit_false_writes_NOTHING_after_a_rollback(db, user):
    """THE bug this file exists to prevent recurring.

    `create_deck` committed unconditionally while `resolve_commander_to_deck`
    advertised `commit=False`, so the #164 backfill's DRY RUN wrote five decks to
    production. A caller cannot honour a no-write contract that its callee silently
    breaks — so the contract is now tested end to end, through a rollback, rather
    than trusted.
    """
    _card(db, "Atraxa, Praetors' Voice", "sc-atraxa")
    before = db.query(Deck).count()

    deck, missing = deck_service.resolve_commander_to_deck(
        db, user.id, "Atraxa, Praetors' Voice", commit=False
    )
    assert missing == [] and deck is not None
    assert deck.id is not None, "commit=False must still flush, or dependent rows have no FK"

    db.rollback()

    assert db.query(Deck).count() == before, "a dry run wrote a deck"
    assert db.query(DeckCommander).count() == 0
    assert db.query(StorageLocation).filter(StorageLocation.type == "deck").count() == 0, (
        "a dry run left an orphan storage location"
    )


def test_create_deck_commit_false_leaves_nothing_behind(db, user):
    """The same contract at the layer that actually broke it."""
    before = db.query(Deck).count()

    d = deck_service.create_deck(db, user.id, "Scratch", commit=False)
    assert d.id is not None

    db.rollback()

    assert db.query(Deck).count() == before
    assert db.query(StorageLocation).filter(StorageLocation.name == "Scratch").count() == 0


def test_create_deck_still_commits_by_default(db, user):
    """The default must be unchanged — every existing caller relies on it."""
    d = deck_service.create_deck(db, user.id, "Committed")
    db.rollback()
    assert db.get(Deck, d.id) is not None


# ── The catalog is the BULK CACHE, not the cards we happen to own ────────────
# Reported 2026-07-29: attributing Phil's seat to "Wolverine, Best There Is"
# failed with the flavor-name banner. It is not a flavor name — `cards` only
# holds cards somebody TOUCHED, and nobody here owns it, while `scryfall_cards`
# held two printings the whole time.


def _bulk(db, name, sid="bulk-1", set_code="mar", collector="97"):
    from app.legacy_tables import scryfall_cards

    db.execute(
        scryfall_cards.insert().values(
            scryfall_id=sid,
            name=name,
            set_code=set_code,
            set_name="Test Set",
            collector_number=collector,
            type_line="Legendary Creature — Mutant Berserker Hero",
            color_identity="G",
        )
    )
    db.commit()
    return sid


def test_a_commander_nobody_owns_still_resolves(db, user):
    """The reported case, end to end."""
    _bulk(db, "Wolverine, Best There Is")
    assert db.query(Card).filter(Card.name == "Wolverine, Best There Is").count() == 0

    deck, unresolved = deck_service.resolve_commander_to_deck(
        db, user.id, "Wolverine, Best There Is"
    )

    assert unresolved == []
    assert deck is not None and deck.name == "Wolverine, Best There Is"
    card = db.query(Card).filter(Card.name == "Wolverine, Best There Is").one()
    assert card.set_code == "mar"
    assert db.query(DeckCommander).filter(DeckCommander.deck_id == deck.id).count() == 1


def test_a_typed_name_matches_the_cache_case_insensitively(db, user):
    """`cards` has always matched on `lower(name)`; the cache must not be stricter,
    or an owned commander resolves from a typo-cased entry and an unowned one does
    not."""
    _bulk(db, "Wolverine, Best There Is")

    deck, unresolved = deck_service.resolve_commander_to_deck(
        db, user.id, "wolverine, BEST there is"
    )

    assert unresolved == []
    assert deck is not None


def test_an_owned_printing_still_wins_over_the_cache(db, user):
    """Rule 2 is unchanged — the cache is a FALLBACK, not a new first choice."""
    owned = _card(db, "Wolverine, Best There Is", "owned-wolv", set_code="sld")
    _own(db, user, owned)
    _bulk(db, "Wolverine, Best There Is", sid="bulk-wolv")

    deck, _ = deck_service.resolve_commander_to_deck(db, user.id, "Wolverine, Best There Is")

    anchor = db.query(DeckCommander).filter(DeckCommander.deck_id == deck.id).one()
    assert anchor.card_id == owned.id


def test_a_materialised_card_rolls_back_with_a_dry_run(db, user):
    """#164's lesson: a no-write contract is only as honest as the deepest call it
    makes. The Card is FLUSHED, so `commit=False` must discard it too."""
    _bulk(db, "Wolverine, Best There Is")

    deck_service.resolve_commander_to_deck(db, user.id, "Wolverine, Best There Is", commit=False)
    db.rollback()

    assert db.query(Card).filter(Card.name == "Wolverine, Best There Is").count() == 0
    assert db.query(Deck).count() == 0


def test_a_name_in_neither_table_still_fails_visibly(db, user):
    """The flavor-name path is unchanged — it just no longer swallows real cards."""
    _bulk(db, "Sisay, Weatherlight Captain")

    deck, unresolved = deck_service.resolve_commander_to_deck(
        db, user.id, "Buttercup, Provincial Princess"
    )

    assert deck is None
    assert unresolved == ["Buttercup, Provincial Princess"]
    assert db.query(Card).count() == 0
