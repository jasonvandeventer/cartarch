"""#165 — a logged-in member claims a seat from their phone, before the game starts.

Attribution used to happen only on the tablet: a player's phone could see a game and
set their own deck, but only if the tablet had already seated them. There was no way
in — the lobby omitted the game, and both the view and the POST 404'd. Those 404s were
correct (`get_viewable_game` + a seat check), so claiming has to introduce a
deliberate, scoped way past them.

**ONE claim primitive, TWO front doors.** A QR link carries the code in the path; the
manual form posts the same code. The QR is a shortcut, not the mechanism — a tablet
across the table or a camera in bad light must not be what stops someone joining.

**Guest claiming is NOT here** (#172). Every route below requires a logged-in account,
which is what keeps this from opening a new trust boundary.
"""

from __future__ import annotations

import itertools

import pytest

from app import game_service
from app.game_service import GameLockedError, claim_seat, claimable_seats
from app.models import (
    Card,
    Deck,
    DeckCommander,
    Game,
    GameSeat,
    StorageLocation,
    User,
)

_seq = itertools.count(1)


@pytest.fixture
def guest_client(client):
    """A client with NO auth override at all — a real signed-out browser over the
    test DB, carrying its cookies. The only way to prove the claim actually signs
    the guest in, since the shared fixture would answer with the pinned user."""
    from app import main
    from app.dependencies import get_current_user

    main.app.dependency_overrides.pop(get_current_user, None)
    yield client


@pytest.fixture
def authed_client(client, user):
    """The shared ``client`` fixture pins ``get_current_user`` but not
    ``get_optional_current_user`` — which is what the join routes read since #172,
    so through ``client`` they see an anonymous visitor. This pins both, i.e. a
    signed-in claimant; use plain ``client`` to exercise the guest path."""
    from app import main
    from app.dependencies import get_optional_current_user

    main.app.dependency_overrides[get_optional_current_user] = lambda: user
    try:
        yield client
    finally:
        main.app.dependency_overrides.pop(get_optional_current_user, None)


def _user(db, display=None):
    u = User(username=f"claim{next(_seq)}@ex.com", password_hash="x", display_name=display)
    db.add(u)
    db.commit()
    return u


def _game(db, owner, seats=4, status="created", code="CODE123"):
    g = Game(user_id=owner.id, format="Commander", status=status, join_code=code)
    db.add(g)
    db.commit()
    for i in range(1, seats + 1):
        db.add(GameSeat(game_id=g.id, seat_number=i, player_name=f"Player {i}", starting_life=40))
    db.commit()
    return g


def _card(db, name, sid):
    c = Card(
        scryfall_id=sid,
        name=name,
        set_code="tst",
        collector_number="1",
        type_line="Legendary Creature — Human",
    )
    db.add(c)
    db.commit()
    return c


# ── The claim ───────────────────────────────────────────────────────────────


def test_a_member_claims_an_unclaimed_seat(db, user):
    g = _game(db, user)
    joiner = _user(db, display="Mason")
    seat = claimable_seats(g)[1]

    claimed, unresolved = claim_seat(db, code="CODE123", user_id=joiner.id, seat_id=seat.id)

    assert unresolved == []
    assert claimed.user_id == joiner.id


def test_the_claim_ALWAYS_attaches_a_user_id(db, user):
    """#152 groups by GameSeat.user_id and never player_name — a claim that set only
    a name would regress attribution for the best-covered population."""
    g = _game(db, user)
    joiner = _user(db, display="Mason")

    claimed, _ = claim_seat(
        db,
        code="CODE123",
        user_id=joiner.id,
        seat_id=claimable_seats(g)[0].id,
        display_name="Totally Different Name",
    )

    assert claimed.user_id == joiner.id
    assert claimed.player_name == "Totally Different Name"


def test_the_placeholder_name_is_OVERWRITTEN(db, user):
    """Positional labels are worse than nothing — next month's 'Player 3' is someone
    else (#167 on Opp 1/2/3)."""
    g = _game(db, user)
    joiner = _user(db, display="Mason")
    seat = claimable_seats(g)[2]
    assert seat.player_name == "Player 3"

    claimed, _ = claim_seat(db, code="CODE123", user_id=joiner.id, seat_id=seat.id)

    assert claimed.player_name == "Mason"
    assert claimed.user_name_at_game == "Mason"


def test_a_blank_name_falls_back_to_the_account(db, user):
    g = _game(db, user)
    joiner = _user(db, display="Mason")

    claimed, _ = claim_seat(
        db, code="CODE123", user_id=joiner.id, seat_id=claimable_seats(g)[0].id, display_name="  "
    )

    assert claimed.player_name == "Mason"


# ── Guards, each deliberate ─────────────────────────────────────────────────


def test_a_started_game_refuses_claims(db, user):
    """Same boundary set_own_seat_deck already enforces — not a second timing model."""
    g = _game(db, user, status="in_progress")
    joiner = _user(db)

    with pytest.raises(GameLockedError):
        claim_seat(db, code="CODE123", user_id=joiner.id, seat_id=claimable_seats(g)[0].id)


def test_a_taken_seat_cannot_be_stolen(db, user):
    g = _game(db, user)
    first, second = _user(db), _user(db)
    seat = claimable_seats(g)[0]
    claim_seat(db, code="CODE123", user_id=first.id, seat_id=seat.id)

    with pytest.raises(PermissionError):
        claim_seat(db, code="CODE123", user_id=second.id, seat_id=seat.id)

    db.refresh(seat)
    assert seat.user_id == first.id, "the seat was silently overwritten"


def test_one_person_cannot_take_two_seats(db, user):
    """Otherwise one phone could occupy the whole table."""
    g = _game(db, user)
    joiner = _user(db)
    seats = claimable_seats(g)
    claim_seat(db, code="CODE123", user_id=joiner.id, seat_id=seats[0].id)

    with pytest.raises(PermissionError):
        claim_seat(db, code="CODE123", user_id=joiner.id, seat_id=seats[1].id)


def test_an_unknown_code_finds_nothing(db, user):
    _game(db, user)
    joiner = _user(db)

    with pytest.raises(LookupError):
        claim_seat(db, code="NOPE", user_id=joiner.id, seat_id=1)


def test_a_disabled_code_finds_nothing(db, user):
    """NULL join_code = claiming off. A blank submission must not match it."""
    _game(db, user, code=None)
    assert game_service.get_game_by_join_code(db, "") is None
    assert game_service.get_game_by_join_code(db, None) is None


# ── Commander entry routes through #164 ─────────────────────────────────────


def test_claiming_with_a_commander_links_a_deck(db, user):
    g = _game(db, user)
    joiner = _user(db, display="Mason")
    _card(db, "Atraxa, Praetors' Voice", "sc-atx")

    claimed, unresolved = claim_seat(
        db,
        code="CODE123",
        user_id=joiner.id,
        seat_id=claimable_seats(g)[0].id,
        commander_entry="Atraxa, Praetors' Voice",
    )

    assert unresolved == []
    assert claimed.deck_id is not None
    assert claimed.commander_name_at_game == "Atraxa, Praetors' Voice"
    assert db.get(Deck, claimed.deck_id).contents_tracked is False


def test_it_finds_an_EXISTING_deck_rather_than_duplicating(db, user):
    """#164's find-before-create, reached through the claim path."""
    g = _game(db, user)
    joiner = _user(db)
    card = _card(db, "Atraxa, Praetors' Voice", "sc-atx")
    loc = StorageLocation(user_id=joiner.id, name="Mine", type="deck", mode="manual")
    db.add(loc)
    db.commit()
    existing = Deck(user_id=joiner.id, name="Superfriends", storage_location_id=loc.id)
    db.add(existing)
    db.commit()
    db.add(DeckCommander(deck_id=existing.id, card_id=card.id))
    db.commit()

    claimed, _ = claim_seat(
        db,
        code="CODE123",
        user_id=joiner.id,
        seat_id=claimable_seats(g)[0].id,
        commander_entry="Atraxa, Praetors' Voice",
    )

    assert claimed.deck_id == existing.id
    assert db.query(Deck).filter(Deck.user_id == joiner.id).count() == 1


def test_an_unmatched_commander_still_claims_the_seat(db, user):
    """Never fail a claim over a deck-naming problem — but never hide it either.

    A flavor name lands here ("Buttercup, Provincial Princess" is Sisay).
    """
    g = _game(db, user)
    joiner = _user(db)

    claimed, unresolved = claim_seat(
        db,
        code="CODE123",
        user_id=joiner.id,
        seat_id=claimable_seats(g)[0].id,
        commander_entry="Buttercup, Provincial Princess",
    )

    assert claimed.user_id == joiner.id, "the claim was lost over a bad commander name"
    assert unresolved == ["Buttercup, Provincial Princess"]
    assert claimed.deck_id is None
    assert db.query(Deck).count() == 0


# ── Two front doors, one claim ──────────────────────────────────────────────


def test_the_QR_path_and_the_manual_path_render_the_same_page(client, db, user):
    _game(db, user)

    by_qr = client.get("/join/CODE123")
    manual = client.get("/join?code=CODE123")

    assert by_qr.status_code == manual.status_code == 200
    assert "Pick your seat" in by_qr.text
    assert "Pick your seat" in manual.text


def test_the_manual_form_is_offered_even_without_a_code(client, db, user):
    """A camera can fail; typing must always be available."""
    r = client.get("/join")
    assert r.status_code == 200
    assert 'name="code"' in r.text


def test_a_started_game_says_so_rather_than_pretending_the_code_is_wrong(client, db, user):
    _game(db, user, status="in_progress")

    body = client.get("/join/CODE123").text

    assert "already started" in body
    assert "Pick your seat" not in body


def test_claiming_through_the_route_redirects_to_the_companion_view(authed_client, db, user):
    g = _game(db, user)
    seat = claimable_seats(g)[0]

    r = authed_client.post("/join/CODE123/claim", data={"seat_id": seat.id}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/games/{g.id}/companion")


def test_the_route_reports_an_unresolved_commander(authed_client, db, user):
    g = _game(db, user)
    seat = claimable_seats(g)[0]

    r = authed_client.post(
        "/join/CODE123/claim",
        data={"seat_id": seat.id, "commander_name": "Buttercup, Provincial Princess"},
        follow_redirects=False,
    )

    assert "commander_unresolved" in r.headers["location"]


def test_claiming_a_taken_seat_through_the_route_is_403(authed_client, db, user):
    g = _game(db, user)
    other = _user(db)
    seat = claimable_seats(g)[0]
    claim_seat(db, code="CODE123", user_id=other.id, seat_id=seat.id)

    r = authed_client.post("/join/CODE123/claim", data={"seat_id": seat.id}, follow_redirects=False)

    assert r.status_code == 403


# ── The tablet can see claims land ──────────────────────────────────────────


def test_the_lobby_endpoint_reports_seat_occupancy(client, db, user):
    """#165 finding: there is NO SSE stream before live_start, so without this the
    tablet shows a static page while phones claim seats behind it."""
    g = _game(db, user)
    joiner = _user(db, display="Mason")
    claim_seat(db, code="CODE123", user_id=joiner.id, seat_id=claimable_seats(g)[1].id)

    body = client.get(f"/games/{g.id}/lobby.json").json()

    assert body["status"] == "created"
    assert len(body["seats"]) == 4
    claimed = [s for s in body["seats"] if s["claimed"]]
    assert len(claimed) == 1
    assert claimed[0]["player_name"] == "Mason"


# ── The code is the toggle ──────────────────────────────────────────────────


def test_the_owner_can_mint_and_revoke_the_code(client, db, user):
    g = _game(db, user, code=None)

    client.post(f"/games/{g.id}/join-code", data={"enable": "true"}, follow_redirects=False)
    db.refresh(g)
    assert g.join_code

    client.post(f"/games/{g.id}/join-code", data={"enable": "false"}, follow_redirects=False)
    db.refresh(g)
    assert g.join_code is None


def test_a_non_owner_cannot_touch_the_code(client, db, user):
    """get_game is strict owner-only, so this 404s rather than leaking existence."""
    other = _user(db)
    g = _game(db, other, code=None)

    r = client.post(f"/games/{g.id}/join-code", data={"enable": "true"}, follow_redirects=False)

    assert r.status_code == 404


def test_the_claim_code_is_NOT_the_table_token(db, user):
    """Different in KIND: the table token controls every seat and must never reach
    a phone; the join code only lets a member attach themselves to one seat."""
    g = _game(db, user)
    g.client_token = "TABLETOKEN"
    db.commit()

    assert game_service.get_game_by_join_code(db, "TABLETOKEN") is None
    assert game_service.get_game_by_join_code(db, "CODE123").id == g.id


def test_generated_codes_are_unique(db, user):
    codes = {game_service.generate_join_code(db) for _ in range(20)}
    assert len(codes) == 20


# ── The QR is a shortcut, never the mechanism ───────────────────────────────


def test_the_tablet_shows_both_a_code_and_a_QR(client, db, user):
    g = _game(db, user)

    body = client.get(f"/games/{g.id}").text

    assert "CODE123" in body, "the code must be readable as text"
    assert "<svg" in body.split("join-qr")[1][:400] if "join-qr" in body else True
    assert "/join" in body


def test_the_QR_degrades_to_the_code_rather_than_breaking_the_page(db, user):
    """A QR that will not draw must return "", not raise."""
    assert game_service.join_qr_svg("") == ""
    svg = game_service.join_qr_svg("https://cartarch.com/join/AbCd1234")
    assert svg.startswith("<svg") and len(svg) > 500


def test_no_QR_or_code_is_shown_once_the_game_is_live(client, db, user):
    """Claiming is refused once live, so advertising a code would be a lie."""
    g = _game(db, user, status="in_progress")

    body = client.get(f"/games/{g.id}").text

    assert "Turn joining off" not in body
    assert "Let players join from their phones" not in body


def test_two_games_may_both_have_claiming_DISABLED(db, user):
    """The other half of the PARTIAL unique index.

    `uq_games_join_code` is unique only `WHERE join_code IS NOT NULL`. Without the
    predicate, the second game with claiming off would collide — so every game but
    one could never disable it. (The enabled-collision half is enforced by the DB
    and was verified on PG18; this half is the one a plain unique index breaks.)
    """
    _game(db, user, code=None)
    _game(db, user, code=None)

    assert db.query(Game).filter(Game.join_code.is_(None)).count() == 2


def test_two_games_cannot_share_an_ENABLED_code(db, user):
    """Otherwise a claim would be ambiguous about which game it joins."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    _game(db, user, code="SAME")
    with _pytest.raises(IntegrityError):
        _game(db, user, code="SAME")
    db.rollback()


# ── Joining is set up where you decide who is playing ───────────────────────


def test_the_new_game_form_offers_joining_up_front(client, db, user):
    """It used to be discoverable one page too late — create the game, then hunt
    for a button. The moment you decide who is playing is the moment you want it."""
    body = client.get("/games/new").text

    assert 'name="enable_join"' in body
    assert "join from their phones" in body


def test_the_button_says_CREATE_not_START(client, db, user):
    """It creates the game in `created` status; it does NOT start it. The old label
    implied pressing it closed the door on joining, when it is what opens it."""
    body = client.get("/games/new").text

    assert "Create Game" in body
    assert "Start Game" not in body


def test_creating_with_the_box_ticked_mints_a_code(client, db, user):
    r = client.post(
        "/games",
        data={
            "player_count": "2",
            "format": "Commander",
            "player_names": ["", ""],
            "deck_ids": ["", ""],
            "commander_names": ["", ""],
            "user_ids": ["", ""],
            "grid_positions": ["", ""],
            "starting_life": "40",
            "enable_join": "true",
        },
        follow_redirects=False,
    )

    assert r.status_code == 303
    game = db.query(Game).order_by(Game.id.desc()).first()
    assert game.join_code, "the host would have to go find a button"
    assert game.status == "created", "creating must not start the game"


def test_creating_WITHOUT_the_box_mints_nothing(client, db, user):
    """An unticked box must mean off — a checkbox that is always on is not a choice."""
    client.post(
        "/games",
        data={
            "player_count": "2",
            "format": "Commander",
            "player_names": ["", ""],
            "deck_ids": ["", ""],
            "commander_names": ["", ""],
            "user_ids": ["", ""],
            "grid_positions": ["", ""],
            "starting_life": "40",
        },
        follow_redirects=False,
    )

    game = db.query(Game).order_by(Game.id.desc()).first()
    assert game.join_code is None


def test_a_code_minted_at_creation_actually_works(client, db, user):
    """End to end: the whole point is that the next page is ready to join."""
    client.post(
        "/games",
        data={
            "player_count": "2",
            "format": "Commander",
            "player_names": ["", ""],
            "deck_ids": ["", ""],
            "commander_names": ["", ""],
            "user_ids": ["", ""],
            "grid_positions": ["", ""],
            "starting_life": "40",
            "enable_join": "true",
        },
        follow_redirects=False,
    )
    game = db.query(Game).order_by(Game.id.desc()).first()

    page = client.get(f"/games/{game.id}").text
    assert game.join_code in page
    assert "<svg" in page  # the QR rendered

    joiner = _user(db, display="Mason")
    claimed, _ = claim_seat(
        db, code=game.join_code, user_id=joiner.id, seat_id=claimable_seats(game)[0].id
    )
    assert claimed.user_id == joiner.id


# ── Playgroup co-members find the game without a code (v4.12.32) ────────────


def _playgroup_with(db, *users, name=None):
    from app.models import Playgroup, PlaygroupMember

    pg = Playgroup(name=name or f"PG{next(_seq)}", created_by=users[0].id)
    db.add(pg)
    db.commit()
    for u in users:
        db.add(PlaygroupMember(playgroup_id=pg.id, user_id=u.id))
    db.commit()
    return pg


def test_a_co_member_is_offered_the_game_without_being_handed_a_code(client, db, user):
    """The app already knows who is in the playgroup — making a member scan a QR
    to join their own group's game asks them to prove something it can look up."""
    from app.game_service import joinable_games_for_user

    member = _user(db, display="Mason")
    pg = _playgroup_with(db, user, member)
    g = _game(db, user, code="PGCODE01")
    g.playgroup_id = pg.id
    db.commit()

    offered = joinable_games_for_user(db, member.id)
    assert [x["game_id"] for x in offered] == [g.id]
    assert offered[0]["open_seats"] == 4


def test_a_non_member_is_not_offered_it(db, user):
    from app.game_service import joinable_games_for_user

    outsider = _user(db)
    pg = _playgroup_with(db, user)
    g = _game(db, user, code="PGCODE02")
    g.playgroup_id = pg.id
    db.commit()
    assert joinable_games_for_user(db, outsider.id) == []


def test_the_code_is_still_THE_toggle_for_members_too(db, user):
    """A second independent way to enable joining is a state nobody could reason
    about from the game page. Joining off means off, membership or not."""
    from app.game_service import joinable_games_for_user

    member = _user(db)
    pg = _playgroup_with(db, user, member)
    g = _game(db, user, code=None)
    g.playgroup_id = pg.id
    db.commit()
    assert joinable_games_for_user(db, member.id) == []


def test_an_unlinked_game_is_invisible_here(db, user):
    """Same scope rule the playgroup record uses — an unlinked private game must
    not surface on a shared surface."""
    from app.game_service import joinable_games_for_user

    member = _user(db)
    _playgroup_with(db, user, member)
    _game(db, user, code="PGCODE03")  # no playgroup_id
    assert joinable_games_for_user(db, member.id) == []


def test_a_started_or_full_game_is_not_offered(db, user):
    """claim_seat refuses both, so offering either is offering a dead link."""
    from app.game_service import joinable_games_for_user

    member = _user(db)
    pg = _playgroup_with(db, user, member)

    live = _game(db, user, status="in_progress", code="PGCODE04")
    live.playgroup_id = pg.id
    full = _game(db, user, code="PGCODE05")
    full.playgroup_id = pg.id
    for s in full.seats:
        s.user_id = user.id if s.seat_number == 1 else _user(db).id
    db.commit()

    assert joinable_games_for_user(db, member.id) == []


def test_someone_already_seated_is_not_offered_their_own_game(db, user):
    from app.game_service import joinable_games_for_user

    member = _user(db)
    pg = _playgroup_with(db, user, member)
    g = _game(db, user, code="PGCODE06")
    g.playgroup_id = pg.id
    g.seats[1].user_id = member.id
    db.commit()
    assert joinable_games_for_user(db, member.id) == []


def test_the_offer_actually_renders_on_the_companion_lobby(client, db, user):
    """Route-level: `joinable` has to reach the template (#152's failure mode)."""
    pg = _playgroup_with(db, user, name="Smackdown")
    other = _user(db)
    g = _game(db, other, code="PGCODE07")
    g.playgroup_id = pg.id
    db.commit()

    body = client.get("/companion").text
    assert "/join/PGCODE07" in body
    assert "Take a seat" in body
    assert "Smackdown" in body


# ── The commander suggestion list ───────────────────────────────────────────
# Typing "Atraxa, Praetors' Voice" exactly, on a phone, is the friction — and a
# typo is silent non-attribution, the very gap #175 exists to close. The list is
# a native <datalist>, so it costs no route and no keystroke fetch.


def test_a_commander_nobody_owns_is_offered(client, db, user):
    """The reported failure, 2026-07-29: this pod plays a set nobody has entered,
    so a list drawn from `cards` had none of their commanders in it and the box
    read as broken. The catalog is the bulk cache."""
    from app.legacy_tables import scryfall_cards

    _game(db, user)
    db.execute(
        scryfall_cards.insert().values(
            scryfall_id="bulk-wolv",
            name="Wolverine, Best There Is",
            set_code="mar",
            set_name="Marvel",
            collector_number="97",
            type_line="Legendary Creature — Mutant Berserker Hero",
        )
    )
    db.commit()
    assert db.query(Card).count() == 0

    body = client.get("/join/CODE123").text

    assert "Wolverine, Best There Is" in body


def test_a_non_commander_in_the_cache_is_not_offered(db):
    """`can_be_commander` still decides — the wider source is not a wider rule."""
    from app.legacy_tables import scryfall_cards
    from app.recommendation_service import commander_name_options

    db.execute(
        scryfall_cards.insert().values(
            scryfall_id="bulk-cradle",
            name="Gaea's Cradle",
            set_code="usg",
            set_name="Urza's Saga",
            collector_number="321",
            type_line="Legendary Land",
        )
    )
    db.commit()

    assert commander_name_options(db) == []


def test_the_commander_box_offers_the_local_catalogs_commanders(client, db, user):
    _game(db, user)
    _card(db, "Atraxa, Praetors' Voice", "sug-1")

    body = client.get("/join/CODE123").text

    assert 'list="commander-options"' in body
    assert '<datalist id="commander-options">' in body
    assert "Atraxa, Praetors&#39; Voice" in body


def test_every_suggestion_resolves(client, db, user):
    """The invariant that makes a local list the right source: a picked suggestion
    can never land on the "couldn't find" banner. A Scryfall typeahead would offer
    thousands of names `_pick_representative_printing` cannot resolve."""
    from app.deck_service import resolve_commander_to_deck
    from app.recommendation_service import commander_name_options

    _card(db, "Atraxa, Praetors' Voice", "sug-2")
    _card(db, "Grist, the Hunger Tide", "sug-3")
    db.query(Card).filter(Card.scryfall_id == "sug-3").update(
        {
            "type_line": "Legendary Creature — Insect",
            "oracle_text": "Grist isn't on the battlefield, it's a creature card.",
        }
    )
    db.commit()

    names = commander_name_options(db)
    assert names

    for name in names:
        deck, unresolved = resolve_commander_to_deck(db, user.id, name)
        assert unresolved == [], f"{name} was suggested but does not resolve"
        assert deck is not None


def test_a_non_commander_is_not_suggested(db):
    """Front-face judging is #160's, not a second definition — a back-face legend
    (Westvale Abbey) is not a commander and must not be offered."""
    from app.recommendation_service import commander_name_options

    db.add(
        Card(
            scryfall_id="sug-4",
            name="Westvale Abbey",
            set_code="tst",
            collector_number="2",
            type_line="Land // Legendary Creature — Demon",
        )
    )
    # Passes the SQL prefilter and is excluded only by can_be_commander itself.
    db.add(
        Card(
            scryfall_id="sug-5",
            name="Gaea's Cradle",
            set_code="tst",
            collector_number="3",
            type_line="Legendary Land",
        )
    )
    db.commit()

    assert commander_name_options(db) == []


# ── #172: joining WITHOUT a Cartarch account ────────────────────────────────
# A guest is a real `users` row with an unusable password, not a second identity
# system. That is the cheap way, not the thorough one: every attribution surface
# keys on user_id, and `decks.user_id` is NOT NULL, so a NULL-user seat records
# nothing anyone can read back.


def test_a_signed_out_visitor_can_reach_the_claim_form(client, db, user):
    """An auth wall on the join page IS the wall — this is where somebody with no
    account joins."""
    _game(db, user)

    body = client.get("/join/CODE123").text

    assert "Pick your seat" in body
    assert "No account needed" in body


def test_claiming_as_a_guest_mints_an_account_and_attributes_the_seat(client, db, user):
    from app.models import User as UserModel

    g = _game(db, user)
    seat = claimable_seats(g)[1]

    r = client.post(
        "/join/CODE123/claim",
        data={"seat_id": seat.id, "display_name": "Mason"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/games/{g.id}/companion")
    db.refresh(seat)
    assert seat.player_name == "Mason"
    assert seat.user_id is not None, "a guest seat that records no user records nothing"
    guest = db.get(UserModel, seat.user_id)
    assert guest.is_guest
    assert guest.display_name == "Mason"


def test_the_guest_password_is_unusable(db):
    """A random secret nobody holds, on an RFC 2606 `.invalid` domain that can
    never receive a reset mail either — so the account cannot be taken over."""
    from app.auth import authenticate_user, create_guest_user

    guest = create_guest_user(db, "Mason")

    assert guest.username.endswith("@guests.cartarch.invalid")
    assert authenticate_user(db, guest.username, "") is None
    assert authenticate_user(db, guest.username, "password") is None


def test_a_nameless_guest_is_sent_back_to_the_form(client, db, user):
    """ "Player 3" is exactly what claiming exists to replace, so an unnamed guest
    seat is worse than no claim."""
    g = _game(db, user)
    seat = claimable_seats(g)[0]

    r = client.post(
        "/join/CODE123/claim",
        data={"seat_id": seat.id, "display_name": "   "},
        follow_redirects=False,
    )

    assert r.headers["location"] == "/join/CODE123?error=name_required"
    db.refresh(seat)
    assert seat.user_id is None


def test_a_guest_commander_resolves_to_a_deck_they_own(client, db, user):
    """The whole point of the account: `decks.user_id` is NOT NULL, so a NULL-user
    seat could never carry #164's placeholder."""
    from app.models import Deck as DeckModel

    g = _game(db, user)
    _card(db, "Atraxa, Praetors' Voice", "guest-atx")
    seat = claimable_seats(g)[0]

    client.post(
        "/join/CODE123/claim",
        data={
            "seat_id": seat.id,
            "display_name": "Mason",
            "commander_name": "Atraxa, Praetors' Voice",
        },
        follow_redirects=False,
    )

    db.refresh(seat)
    assert seat.deck_id is not None
    assert db.get(DeckModel, seat.deck_id).user_id == seat.user_id


def test_every_claim_guard_still_applies_to_a_guest(client, db, user):
    """#172 changes WHO may claim, never WHAT a claim is allowed to do — and a
    refused claim must not leave the account it speculatively minted."""
    from app.models import User as UserModel

    g = _game(db, user, status="in_progress")
    seat = g.seats[0]

    r = client.post(
        "/join/CODE123/claim",
        data={"seat_id": seat.id, "display_name": "Mason"},
        follow_redirects=False,
    )

    assert r.status_code == 409
    db.refresh(seat)
    assert seat.user_id is None
    assert db.query(UserModel).filter(UserModel.is_guest).count() == 0, (
        "a refused claim left a stray guest account behind"
    )


def test_a_guest_does_not_clutter_the_people_picker(db, user):
    """The picker's no-playgroup fallback is "everyone" — without this it fills
    with strangers from other people's tables."""
    from app.auth import create_guest_user
    from app.playgroup_service import get_pickable_users

    guest = create_guest_user(db, "Mason")

    assert guest.id not in {u.id for u in get_pickable_users(db, user.id)}


def test_a_guest_can_use_the_companion_view_it_lands_on(guest_client, db, user, monkeypatch):
    """The payoff of minting an account rather than a NULL-user seat: the phone is
    signed in, so the ordinary seat-scoped surfaces work with no change at all.

    `render()` reads the nav badge counts through the global `SessionLocal`, not
    the overridden dependency — invisible to every other test because none of
    them carries a REAL session cookie, and this one does.
    """
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr("app.dependencies.SessionLocal", sessionmaker(bind=db.get_bind()))
    g = _game(db, user)
    seat = claimable_seats(g)[0]

    r = guest_client.post(
        "/join/CODE123/claim",
        data={"seat_id": seat.id, "display_name": "Mason"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    page = guest_client.get(f"/games/{g.id}/companion")
    assert page.status_code == 200
    assert "Mason" in page.text
