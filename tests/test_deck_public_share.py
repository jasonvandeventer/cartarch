"""#143 — public read-only deck share links.

Token IS the toggle (generate = publish, revoke = NULL → link 404s). The public
route bypasses auth and renders a SANITIZED projection — no owner data leaks.
"""

from __future__ import annotations

import itertools

from app import deck_service
from app.models import Card, InventoryRow

_seq = itertools.count(1)


def _card(db, name, type_line="Creature", ci="G", mana="{1}{G}"):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line=type_line,
        mana_cost=mana,
        cmc=2,
        colors=ci,
        color_identity=ci,
        oracle_text="x",
        rarity="rare",
    )
    db.add(c)
    db.flush()
    return c


def _place(db, user_id, card, loc_id, qty=1, role=None):
    r = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=qty,
        finish="normal",
        is_proxy=False,
        storage_location_id=loc_id,
        is_pending=False,
        role=role,
    )
    db.add(r)
    db.flush()
    return r


def _deck_with_cards(db, user):
    deck = deck_service.create_deck(db, user.id, "Gruul Smash", format_name="commander")
    loc = deck.storage_location_id
    _place(db, user.id, _card(db, "Rampaging Baloths", "Creature — Beast"), loc, qty=1)
    _place(db, user.id, _card(db, "Cultivate", "Sorcery"), loc, qty=1)
    _place(db, user.id, _card(db, "Forest", "Basic Land — Forest", ci="", mana=""), loc, qty=10)
    _place(
        db,
        user.id,
        _card(db, "Ruric Thar", "Legendary Creature — Ogre", ci="RG"),
        loc,
        role="commander",
    )
    db.commit()
    return deck


# --------------------------------------------------------------------------- #
# Service — token lifecycle
# --------------------------------------------------------------------------- #


def test_generate_revoke_lifecycle(db, user):
    deck = _deck_with_cards(db, user)
    assert deck.share_token is None

    tok = deck_service.generate_deck_share_token(db, deck_id=deck.id, user_id=user.id)
    assert tok and deck.share_token == tok
    assert deck_service.get_deck_by_share_token(db, tok).id == deck.id

    # regenerate → new token, old one dead
    tok2 = deck_service.generate_deck_share_token(db, deck_id=deck.id, user_id=user.id)
    assert tok2 != tok
    assert deck_service.get_deck_by_share_token(db, tok) is None
    assert deck_service.get_deck_by_share_token(db, tok2).id == deck.id

    # revoke → NULL, link dead
    assert deck_service.revoke_deck_share_token(db, deck_id=deck.id, user_id=user.id) is True
    assert deck.share_token is None
    assert deck_service.get_deck_by_share_token(db, tok2) is None


def test_owner_scoping(db, user):
    deck = _deck_with_cards(db, user)
    # a different user_id can neither generate nor revoke
    assert (
        deck_service.generate_deck_share_token(db, deck_id=deck.id, user_id=user.id + 999) is None
    )
    assert deck.share_token is None
    deck_service.generate_deck_share_token(db, deck_id=deck.id, user_id=user.id)
    assert deck_service.revoke_deck_share_token(db, deck_id=deck.id, user_id=user.id + 999) is False
    assert deck.share_token is not None  # untouched by the non-owner


def test_get_by_token_rejects_empty(db):
    assert deck_service.get_deck_by_share_token(db, "") is None
    assert deck_service.get_deck_by_share_token(db, None) is None


# --------------------------------------------------------------------------- #
# Service — sanitized view model
# --------------------------------------------------------------------------- #


def test_public_view_is_sanitized(db, user):
    deck = _deck_with_cards(db, user)
    deck.blurb = "A stompy Gruul brew"
    db.commit()
    view = deck_service.build_public_deck_view(db, deck)

    assert view["name"] == "Gruul Smash"
    assert view["format"] == "commander"
    assert view["blurb"] == "A stompy Gruul brew"
    assert set(view["color_identity"]) == set("RG")
    assert [c["name"] for c in view["commanders"]] == ["Ruric Thar"]
    assert view["total_cards"] == 13  # 1 + 1 + 10 + 1 commander

    # grouped, and NO owner fields anywhere in the projected items
    all_items = view["commanders"] + [it for g in view["groups"] for it in g["rows"]]
    forbidden = {"price", "price_usd", "is_proxy", "tags", "effective_price", "storage_location_id"}
    for it in all_items:
        assert forbidden.isdisjoint(it.keys()), it
    labels = {g["label"] for g in view["groups"]}
    assert {"Creature", "Sorcery", "Land"} <= labels


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def test_public_route_200_and_404(client, db, user):
    deck = _deck_with_cards(db, user)

    # create the link via the owner route
    r = client.post(f"/decks/{deck.id}/share", follow_redirects=False)
    assert r.status_code == 303
    db.refresh(deck)
    tok = deck.share_token
    assert tok

    # anonymous GET works and shows the deck, not owner data
    page = client.get(f"/d/{tok}")
    assert page.status_code == 200
    body = page.text
    assert "Gruul Smash" in body and "Ruric Thar" in body
    assert "$" not in body  # no prices on the public page

    # bad token 404s
    assert client.get("/d/does-not-exist").status_code == 404

    # revoke → the link 404s immediately
    assert client.post(f"/decks/{deck.id}/unshare", follow_redirects=False).status_code == 303
    assert client.get(f"/d/{tok}").status_code == 404
