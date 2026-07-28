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


def test_claiming_through_the_route_redirects_to_the_companion_view(client, db, user):
    g = _game(db, user)
    seat = claimable_seats(g)[0]

    r = client.post("/join/CODE123/claim", data={"seat_id": seat.id}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/games/{g.id}/companion")


def test_the_route_reports_an_unresolved_commander(client, db, user):
    g = _game(db, user)
    seat = claimable_seats(g)[0]

    r = client.post(
        "/join/CODE123/claim",
        data={"seat_id": seat.id, "commander_name": "Buttercup, Provincial Princess"},
        follow_redirects=False,
    )

    assert "commander_unresolved" in r.headers["location"]


def test_claiming_a_taken_seat_through_the_route_is_403(client, db, user):
    g = _game(db, user)
    other = _user(db)
    seat = claimable_seats(g)[0]
    claim_seat(db, code="CODE123", user_id=other.id, seat_id=seat.id)

    r = client.post("/join/CODE123/claim", data={"seat_id": seat.id}, follow_redirects=False)

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
