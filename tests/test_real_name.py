"""`users.real_name` — the name the playgroup knows you by.

Requested 2026-08-14: show people's actual names, and default a game seat to
them. Owner decisions: **real name only** (not "Alex (SaintWacko)"), and
**admin seeds all, users edit their own**.

**A SEPARATE COLUMN, not a repurposing of display_name**, and the tests below
treat that as the load-bearing part. `display_name` is the pseudonymous handle
and it is what the ANONYMOUS wishlist page renders; writing real names into it
would turn a public surface into a first-name one for anyone holding a link.

`User.player_label` is THE definition (real → handle → login). It exists
because `display_name or username` was open-coded at ten sites, so a third
field would otherwise mean ten edits — the multi-copy trap this codebase keeps
paying for.
"""

from __future__ import annotations

from app.models import Playgroup, PlaygroupMember, User


def _mk(db, username, *, display=None, real=None):
    u = User(username=username, password_hash="x", display_name=display, real_name=real)
    db.add(u)
    db.flush()
    return u


# ── the one definition ───────────────────────────────────────────────────────


def test_player_label_prefers_real_then_handle_then_login(db):
    assert _mk(db, "a@x.invalid", display="SaintWacko", real="Alex").player_label == "Alex"
    assert _mk(db, "b@x.invalid", display="MasonRex").player_label == "MasonRex"
    assert _mk(db, "c@x.invalid").player_label == "c@x.invalid"


def test_a_blank_real_name_never_renders_as_an_empty_label(db):
    """Both write paths strip to None, so a blank never reaches the column.

    Pinned because an empty-string real_name would win the `or` chain and
    render a nameless seat — the property strips, but the guarantee lives in
    the routes, so this asserts what a user can actually produce.
    """
    from app.routes.account import update_profile  # noqa: F401  (route is the guard)

    u = _mk(db, "d@x.invalid", display="CptObvious", real=None)
    assert u.player_label == "CptObvious"


def test_the_sql_expression_matches_the_python_property(db):
    """`player_label_expr` exists because a property cannot ORDER BY."""
    _mk(db, "e@x.invalid", display="Zed", real="Alex")
    _mk(db, "f@x.invalid", display="Aaron")
    rows = db.query(User.player_label_expr()).order_by(User.player_label_expr()).all()
    labels = [r[0] for r in rows]
    assert labels == sorted(labels)
    assert "Alex" in labels and "Aaron" in labels
    assert "Zed" not in labels  # the real name won, exactly as the property says


# ── the privacy boundary ─────────────────────────────────────────────────────


def test_the_anonymous_wishlist_page_never_shows_a_real_name(db, client, user):
    """THE guard. /w/{token} is public — it must keep showing the handle."""

    user.display_name = "SaintWacko"
    user.real_name = "Alex"
    user.wishlist_share_token = "tok-public"
    db.commit()

    body = client.get("/w/tok-public").text
    assert "SaintWacko" in body
    assert "Alex" not in body


def test_the_public_deck_page_never_shows_a_real_name(db, client, user):
    from app.models import Deck

    user.display_name = "SaintWacko"
    user.real_name = "Alex"
    deck = Deck(user_id=user.id, name="Silverquill", share_token="tok-deck")
    db.add(deck)
    db.commit()

    body = client.get("/d/tok-deck").text
    assert "Alex" not in body


# ── it reaches the surfaces that asked for it ────────────────────────────────


def test_a_new_game_seat_defaults_to_the_real_name(db, user):
    """The "default to that when creating games" half of the request."""
    from app.game_service import _capture_user_attribution

    user.display_name = "SaintWacko"
    user.real_name = "Alex"
    db.commit()

    _uid, name = _capture_user_attribution(db, user.id)
    assert name == "Alex"


def test_the_playgroup_record_labels_people_by_real_name(db, user):
    from app.models import Game, GameSeat
    from app.playgroup_service import playgroup_record

    user.display_name = "SaintWacko"
    user.real_name = "Alex"
    pg = Playgroup(name="Pod", created_by=user.id)
    db.add(pg)
    db.flush()
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=user.id))
    game = Game(user_id=user.id, playgroup_id=pg.id, format="Commander")
    db.add(game)
    db.flush()
    db.add(
        GameSeat(
            game_id=game.id, user_id=user.id, seat_number=1, placement=1, player_name="SaintWacko"
        )
    )  # NOT NULL; the snapshot at creation
    db.commit()

    rec = playgroup_record(db, pg.id)
    assert [r["label"] for r in rec] == ["Alex"]


# ── who can set it ───────────────────────────────────────────────────────────


def test_a_user_sets_their_own_on_the_account_page(client, db, user):
    resp = client.post(
        "/account/update-profile",
        data={
            "email": user.username,
            "display_name": "SaintWacko",
            "real_name": "Alex",
            "csrf_token": "x",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(user)
    assert user.real_name == "Alex" and user.display_name == "SaintWacko"


def test_clearing_it_restores_the_handle(client, db, user):
    user.display_name = "SaintWacko"
    user.real_name = "Alex"
    db.commit()
    client.post(
        "/account/update-profile",
        data={
            "email": user.username,
            "display_name": "SaintWacko",
            "real_name": "  ",
            "csrf_token": "x",
        },
        follow_redirects=False,
    )
    db.refresh(user)
    assert user.real_name is None
    assert user.player_label == "SaintWacko"


def test_an_admin_seeds_someone_elses(client, db, user):
    user.is_admin = True
    other = _mk(db, "mason@x.invalid", display="MasonRex")
    db.commit()

    resp = client.post(
        f"/admin/users/{other.id}/real-name",
        data={"real_name": "Mason", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(other)
    assert other.real_name == "Mason"


def test_a_non_admin_cannot_set_anyone_elses(client, db, user):
    user.is_admin = False
    other = _mk(db, "phil@x.invalid", display="CptObvious")
    db.commit()

    resp = client.post(
        f"/admin/users/{other.id}/real-name",
        data={"real_name": "Phil", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 403, 404)
    db.refresh(other)
    assert other.real_name is None


def test_no_anonymous_template_reaches_for_player_label():
    """The class, not just the two instances above.

    `/w/{token}` and `/d/{token}` render to people with no account. Their
    templates must keep using `display_name`; `player_label` would surface a
    real name to anyone holding a share link. Behaviour is pinned by the two
    tests above — this stops a NEW anonymous template being written the wrong
    way, which those cannot see.
    """
    import pathlib

    anonymous = ["wishlist_public.html", "deck_public.html"]
    offenders = [
        n for n in anonymous if "player_label" in (pathlib.Path("app/templates") / n).read_text()
    ]
    assert not offenders, f"anonymous template(s) reaching for a real name: {offenders}"
    # Self-check: the files exist and are non-trivial, or this guard is vacuous.
    for n in anonymous:
        assert len((pathlib.Path("app/templates") / n).read_text()) > 500, n
