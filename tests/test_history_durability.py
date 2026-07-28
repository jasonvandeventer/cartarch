"""#163 — ordinary deck cleanup must not destroy game history.

`game_seats.deck_id` and `.user_id` were `ON DELETE SET NULL`, so deleting a deck
nulled the reference on EVERY seat that deck ever occupied, across every game — and
deleting a user did the same for a player. No warning, no error, no trace.

**The FK change alone would have been a no-op**, which is the trap these tests exist
to close. Both delete paths nulled the seats at the *application* layer before the
DELETE ran, so by the time the constraint could fire there were no referencing rows
left. `RESTRICT` on its own would have looked like protection while changing nothing.

Deck deletion is now a soft retire; account deletion is refused for a user with game
history and points at deactivation instead.
"""

from __future__ import annotations

from app import deck_service
from app.models import Card, Deck, Game, GameSeat, StorageLocation, User


def _deck(db, owner, name="Test Deck", *, with_location=True):
    loc = None
    if with_location:
        loc = StorageLocation(user_id=owner.id, name=name, type="deck", mode="manual")
        db.add(loc)
        db.commit()
    d = Deck(user_id=owner.id, name=name, storage_location_id=loc.id if loc else None)
    db.add(d)
    db.commit()
    return d


def _played_game(db, owner, deck=None, seat_user=None):
    g = Game(user_id=owner.id, status="finalized")
    db.add(g)
    db.commit()
    seat = GameSeat(
        game_id=g.id,
        seat_number=1,
        player_name="P1",
        deck_id=deck.id if deck else None,
        user_id=seat_user.id if seat_user else None,
        placement=1,
    )
    db.add(seat)
    db.commit()
    return g, seat


# ── The core defect ─────────────────────────────────────────────────────────


def test_deleting_a_deck_no_longer_erases_its_game_history(db, user):
    """THE regression. The seat must still point at the deck afterwards."""
    deck = _deck(db, user)
    _game, seat = _played_game(db, user, deck=deck)
    assert seat.deck_id == deck.id

    deck_service.delete_deck(db, deck_id=deck.id, user_id=user.id)
    db.refresh(seat)

    assert seat.deck_id == deck.id, "the seat's deck reference was erased"
    assert seat.placement == 1


def test_deck_deletion_retires_rather_than_removing_the_row(db, user):
    deck = _deck(db, user)
    _played_game(db, user, deck=deck)

    assert deck_service.delete_deck(db, deck_id=deck.id, user_id=user.id) is True

    still_there = db.query(Deck).filter(Deck.id == deck.id).one()
    assert still_there.retired_at is not None


def test_a_retired_deck_is_invisible_exactly_as_a_deleted_one_was(db, user):
    """The change has to be unobservable from the app."""
    deck = _deck(db, user, name="Gone")
    deck_service.delete_deck(db, deck_id=deck.id, user_id=user.id)

    assert deck_service.get_deck(db, deck_id=deck.id, user_id=user.id) is None
    assert [d.name for d in deck_service.list_decks(db, user_id=user.id)] == []
    assert [d.name for d in deck_service.list_decks_basic(db, user_id=user.id)] == []


def test_retiring_is_idempotent(db, user):
    deck = _deck(db, user)
    assert deck_service.delete_deck(db, deck_id=deck.id, user_id=user.id) is True
    assert deck_service.delete_deck(db, deck_id=deck.id, user_id=user.id) is False


def test_a_retired_deck_frees_its_name_for_reuse(db, user):
    """Otherwise an invisible change becomes a visible regression."""
    deck_service.delete_deck(db, deck_id=_deck(db, user, name="Atraxa").id, user_id=user.id)
    replacement = _deck(db, user, name="Atraxa")
    assert replacement.retired_at is None


def test_deck_disband_still_returns_real_cards_to_the_collection(db, user):
    """Retiring must not change what the user sees happen to their cards."""
    from app.models import InventoryRow

    deck = _deck(db, user)
    card = Card(scryfall_id="x1", name="Sol Ring", set_code="tst", collector_number="1")
    db.add(card)
    db.commit()
    row = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        finish="normal",
        quantity=1,
        storage_location_id=deck.storage_location_id,
        is_pending=False,
    )
    db.add(row)
    db.commit()

    deck_service.delete_deck(db, deck_id=deck.id, user_id=user.id)
    db.refresh(row)

    assert row.storage_location_id is None, "real card did not return to the collection"
    assert row.is_pending is True


# ── The user side ───────────────────────────────────────────────────────────


def test_admin_user_deletion_is_refused_when_the_user_has_game_history(client, db, user):
    """The AC's 'refused, not silently succeeding'.

    Driven through the real admin route so the guard, not just the FK, is proven.
    """
    from app.routes import admin as admin_routes

    target = User(username="target@example.com", password_hash="x")
    db.add(target)
    db.commit()
    _game, seat = _played_game(db, user, seat_user=target)

    resp = admin_routes.delete_user(user_id=target.id, session=db, current_user=user, _=None)

    assert "user_has_game_history" in resp.headers["location"]
    db.refresh(seat)
    assert seat.user_id == target.id, "the player's seat reference was erased"
    assert db.query(User).filter(User.id == target.id).count() == 1


def test_user_deletion_is_refused_when_ANOTHER_seat_references_their_deck(client, db, user):
    """The borrowed-deck case: someone else's seat pointing at this user's deck."""
    from app.routes import admin as admin_routes

    owner = User(username="deckowner@example.com", password_hash="x")
    db.add(owner)
    db.commit()
    borrowed = _deck(db, owner, name="Lent Out")
    # Seat belongs to `user`, deck belongs to `owner`.
    _game, seat = _played_game(db, user, deck=borrowed, seat_user=user)

    resp = admin_routes.delete_user(user_id=owner.id, session=db, current_user=user, _=None)

    assert "user_has_game_history" in resp.headers["location"]
    db.refresh(seat)
    assert seat.deck_id == borrowed.id


def test_a_user_with_no_game_history_can_still_be_deleted(client, db, user):
    """The guard must not have bricked account deletion outright."""
    from app.routes import admin as admin_routes

    target = User(username="clean@example.com", password_hash="x")
    db.add(target)
    db.commit()

    resp = admin_routes.delete_user(user_id=target.id, session=db, current_user=user, _=None)

    assert "user_has_game_history" not in resp.headers["location"]
    assert db.query(User).filter(User.id == target.id).count() == 0


# ── The commander SET ───────────────────────────────────────────────────────


def test_a_multi_commander_deck_round_trips_as_an_order_independent_set(db, user):
    """Partner order must not split a lineage — the deck-4 failure mode."""
    from app.models import DeckCommander

    deck = _deck(db, user, name="Partners")
    frodo = Card(
        scryfall_id="f", name="Frodo, Adventurous Hobbit", set_code="ltr", collector_number="1"
    )
    sam = Card(scryfall_id="s", name="Sam, Loyal Attendant", set_code="ltr", collector_number="2")
    db.add_all([frodo, sam])
    db.commit()

    # Insert in one order...
    db.add_all(
        [
            DeckCommander(deck_id=deck.id, card_id=sam.id),
            DeckCommander(deck_id=deck.id, card_id=frodo.id),
        ]
    )
    db.commit()

    stored = {r.card_id for r in db.query(DeckCommander).filter(DeckCommander.deck_id == deck.id)}
    # ...and it compares equal to the other order. Set equality, not string.
    assert stored == {frodo.id, sam.id}
    assert stored == {sam.id, frodo.id}


def test_a_deck_with_no_commander_is_legal(db, user):
    """4 decks are in this state today, and #164's placeholders start here."""
    from app.models import DeckCommander

    deck = _deck(db, user, name="Commanderless")
    assert db.query(DeckCommander).filter(DeckCommander.deck_id == deck.id).count() == 0


def test_nothing_forbids_two_decks_sharing_a_commander_set(db, user):
    """#163 amendment 2 is an OPEN owner decision — a constraint would pre-decide it."""
    from app.models import DeckCommander

    card = Card(
        scryfall_id="c", name="Atraxa, Praetors' Voice", set_code="cmd", collector_number="1"
    )
    db.add(card)
    db.commit()
    for name in ("Atraxa Budget", "Atraxa Upgraded"):
        d = _deck(db, user, name=name)
        db.add(DeckCommander(deck_id=d.id, card_id=card.id))
    db.commit()

    assert db.query(DeckCommander).filter(DeckCommander.card_id == card.id).count() == 2


# ── Variants compose ────────────────────────────────────────────────────────


def test_a_game_can_carry_more_than_one_variant(db, user):
    """Planechase + Momir is legitimate, so an enum column would be the wrong shape."""
    from app.models import GameVariant

    g = Game(user_id=user.id, status="finalized")
    db.add(g)
    db.commit()
    db.add_all(
        [
            GameVariant(game_id=g.id, variant="planechase"),
            GameVariant(game_id=g.id, variant="momir"),
        ]
    )
    db.commit()

    variants = {v.variant for v in db.query(GameVariant).filter(GameVariant.game_id == g.id)}
    assert variants == {"planechase", "momir"}


def test_variant_tokens_are_service_layer_constrained(db, user):
    from app.game_service import VALID_GAME_VARIANTS, normalize_game_variant

    assert normalize_game_variant("Planechase") == "planechase"
    assert normalize_game_variant("random-deck") == "random_deck"
    assert normalize_game_variant("nonsense") is None
    assert normalize_game_variant(None) is None
    assert "planechase" in VALID_GAME_VARIANTS


# ── contents_tracked is a column only ───────────────────────────────────────


def test_contents_tracked_marks_a_placeholder_and_gates_only_the_game_pickers(db, user):
    """#164 wrote this column and nothing read it; v4.12.29 is the FIRST reader.

    **The no-reader AST guard that used to live here was deleted deliberately, and
    this is the "say why".** Its reasoning was sound and is worth preserving: #164
    planned to teach every content-dependent surface to skip untracked decks, and
    measurement showed they already degrade correctly because they gate on *"does
    this deck have rows"* (`routes/decks.py`: `if all_deck_rows:`) — the real
    condition, which cannot drift from reality the way a flag can. That argument
    still holds for every one of those surfaces, and none of them reads the flag.

    It does NOT hold for a game deck PICKER, which is why the reader is here and
    nowhere else. A picker is not asking "can I render this deck" — it is asking
    "can this deck be brought to a table", and `if rows:` answers that WRONG in
    both directions: a deck created five minutes ago and not yet filled is empty
    and must stay pickable, while a placeholder is unplayable no matter what rows
    it might one day acquire. `contents_tracked` is exactly "nobody is tracking
    what is in this", which is the distinction a picker needs.

    The scope of the reader is therefore load-bearing: `list_pickable_decks` and
    nothing else. A placeholder stays live everywhere it matters — its seats point
    at it, `resolve_commander_to_deck` still matches it so a replay of that
    commander joins the existing lineage instead of minting a second placeholder,
    and it is still listed and manageable on the Decks page.
    """
    from app.deck_service import list_pickable_decks

    tracked = _deck(db, user)
    assert tracked.contents_tracked is True

    placeholder = _deck(db, user, name="Placeholder Commander")
    placeholder.contents_tracked = False
    db.commit()

    offered = {d.id for d in list_pickable_decks(db, [user.id])}
    assert tracked.id in offered
    assert placeholder.id not in offered, "an untracked placeholder is not playable"


def test_an_empty_but_tracked_deck_is_still_pickable(db, user):
    """The reason the flag is the discriminator and `if rows:` is not.

    A deck you made and have not filled yet is empty and MUST stay offered — the
    whole point of logging a game is often that you just built the thing.
    """
    from app.deck_service import list_pickable_decks

    fresh = _deck(db, user, name="Built This Morning")
    assert fresh.contents_tracked is True
    assert fresh.id in {d.id for d in list_pickable_decks(db, [user.id])}


def test_retired_at_is_not_set_on_a_live_deck(db, user):
    assert _deck(db, user).retired_at is None


def test_a_placeholder_that_gains_cards_becomes_pickable_again(db, user):
    """Nothing ever flips `contents_tracked` back to true.

    So the flag ALONE would be a trap: import a decklist into a placeholder and
    you have a real 100-card deck that is invisible in every game picker forever,
    with no way to fix it from the UI. The `or has rows` half self-heals — a
    placeholder graduates by being used, not by a write nobody would think to make.
    """
    from app.deck_service import list_pickable_decks
    from app.models import Card, InventoryRow

    placeholder = _deck(db, user, name="Grew Into A Deck")
    placeholder.contents_tracked = False
    db.commit()
    assert placeholder.id not in {d.id for d in list_pickable_decks(db, [user.id])}

    card = Card(scryfall_id="grew-1", name="Sol Ring", set_code="tst", collector_number="1")
    db.add(card)
    db.commit()
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=card.id,
            quantity=1,
            finish="normal",
            storage_location_id=placeholder.storage_location_id,
            is_pending=False,
        )
    )
    db.commit()

    assert placeholder.id in {d.id for d in list_pickable_decks(db, [user.id])}
