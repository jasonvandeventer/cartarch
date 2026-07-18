"""#146 — share your wishlist (public link + playgroup visibility).

Names-only projection (no note / target price / ownership). Public link is a
token-as-toggle (#143 shape); playgroup share mirrors the Showcase Share.
"""

from __future__ import annotations

import itertools

from app import watchlist_service as ws
from app.models import Card, PlaygroupMember, User, WatchlistItem, WishlistShare
from app.playgroup_service import create_playgroup

_seq = itertools.count(1)


def _user(db, name):
    u = User(username=f"{name}-{next(_seq)}@x.com", password_hash="x", display_name=name)
    db.add(u)
    db.flush()
    return u


def _card(db, name):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}", name=name, set_code="tst", collector_number=str(next(_seq))
    )
    db.add(c)
    db.flush()
    return c


def _watch_name(db, user_id, name):
    db.add(WatchlistItem(user_id=user_id, card_name=name))
    db.flush()


def _member(db, playgroup_id, user_id, role="member"):
    db.add(PlaygroupMember(playgroup_id=playgroup_id, user_id=user_id, role=role))
    db.flush()


# --------------------------------------------------------------------------- #
# Public token + names-only projection
# --------------------------------------------------------------------------- #


def test_token_lifecycle_and_lookup(db, user):
    assert user.wishlist_share_token is None
    tok = ws.generate_wishlist_share_token(db, user.id)
    assert tok and ws.get_user_by_wishlist_token(db, tok).id == user.id

    tok2 = ws.generate_wishlist_share_token(db, user.id)  # regenerate → old dies
    assert tok2 != tok
    assert ws.get_user_by_wishlist_token(db, tok) is None

    assert ws.revoke_wishlist_share_token(db, user.id) is True
    assert ws.get_user_by_wishlist_token(db, tok2) is None
    assert ws.get_user_by_wishlist_token(db, "") is None


def test_public_view_is_names_only(db, user):
    c = _card(db, "Rhystic Study")
    db.add(WatchlistItem(user_id=user.id, card_id=c.id, note="private!", target_price=5.0))
    _watch_name(db, user.id, "Sol Ring")
    db.commit()

    view = ws.build_public_wishlist_view(db, user.id)
    assert view["count"] == 2
    names = {i["name"] for i in view["cards"]}
    assert names == {"Rhystic Study", "Sol Ring"}
    # the card-id watch is flagged specific-printing; the name watch is not
    by_name = {i["name"]: i for i in view["cards"]}
    assert by_name["Rhystic Study"]["specific_printing"] is True
    assert by_name["Sol Ring"]["specific_printing"] is False
    # NO private fields anywhere in the projection
    forbidden = {"note", "target_price", "current_min_price", "placed_count", "pending_count"}
    for it in view["cards"]:
        assert forbidden.isdisjoint(it.keys()), it


# --------------------------------------------------------------------------- #
# Playgroup sharing — membership-gated
# --------------------------------------------------------------------------- #


def test_playgroup_share_is_membership_gated(db, user):
    owner = user
    member = _user(db, "Bob")
    outsider = _user(db, "Eve")
    pg = create_playgroup(db, owner.id, "Pod")  # creator auto-added as owner-member
    _member(db, pg.id, member.id)
    _watch_name(db, owner.id, "Cyclonic Rift")
    db.commit()

    # a member (owner) can share; an outsider cannot
    assert ws.share_wishlist_to_playgroup(db, owner.id, pg.id) is not None
    assert ws.share_wishlist_to_playgroup(db, outsider.id, pg.id) is None
    # idempotent
    ws.share_wishlist_to_playgroup(db, owner.id, pg.id)
    assert db.query(WishlistShare).filter_by(user_id=owner.id, playgroup_id=pg.id).count() == 1

    # a fellow member sees the owner's shared wishlist; an outsider sees nothing
    seen = ws.list_wishlist_shares_for_playgroup(db, member.id, pg.id)
    assert [r["sharer"].id for r in seen] == [owner.id]
    assert seen[0]["view"]["cards"][0]["name"] == "Cyclonic Rift"
    assert ws.list_wishlist_shares_for_playgroup(db, outsider.id, pg.id) == []

    # unshare removes it
    assert ws.unshare_wishlist_from_playgroup(db, owner.id, pg.id) is True
    assert ws.list_wishlist_shares_for_playgroup(db, member.id, pg.id) == []


def test_share_playgroup_ids_for_owner_picker(db, user):
    pg = create_playgroup(db, user.id, "Pod")  # creator auto-added as owner-member
    db.commit()
    assert ws.list_wishlist_share_playgroup_ids(db, user.id) == set()
    ws.share_wishlist_to_playgroup(db, user.id, pg.id)
    assert ws.list_wishlist_share_playgroup_ids(db, user.id) == {pg.id}


# --------------------------------------------------------------------------- #
# Public route
# --------------------------------------------------------------------------- #


def test_public_route_200_404_and_no_pii(client, db, user):
    user.display_name = "Tester"
    _watch_name(db, user.id, "Demonic Tutor")
    db.commit()
    r = client.post("/watchlist/share", follow_redirects=False)
    assert r.status_code == 303
    db.refresh(user)
    tok = user.wishlist_share_token

    page = client.get(f"/w/{tok}")
    assert page.status_code == 200
    body = page.text
    assert "Demonic Tutor" in body
    assert user.username not in body  # username is the email — must not leak
    assert user.display_name in body  # display_name IS shown ("<name>'s wishlist")

    assert client.get("/w/nope").status_code == 404
    assert client.post("/watchlist/unshare", follow_redirects=False).status_code == 303
    assert client.get(f"/w/{tok}").status_code == 404
