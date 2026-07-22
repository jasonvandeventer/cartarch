"""Wishlist → trade tie-in (#146/#147 follow-up — Alex, Discord 2026-07-22).

Two independent asks over the shared wishlist surfaces:

  Part 1 — "Propose a trade" seeds the OFFERED side from wishlist ∩ own
  inventory. Decision: A6 stands (>= 1 item per side); the button lands the
  viewer on the construction page with offered prefilled, and is HIDDEN when
  the owner has no Showcase Share to a shared playgroup (the C2 precheck).

  Part 2 — [pending] flags a wishlist entry already covered by an in-flight
  trade. Decision: playgroup surface only, aggregate flag only (never the
  proposer's identity or quantity), proposed OR accepted.
"""

from __future__ import annotations

import itertools
import re

from app import trade_service as ts
from app import watchlist_service as ws
from app.models import (
    Card,
    InventoryRow,
    PlaygroupMember,
    Share,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    User,
    WatchlistItem,
)
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


def _loc(db, user_id, name, mode="managed", type_="binder"):
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode=mode)
    db.add(loc)
    db.flush()
    return loc


def _own(db, user_id, card, qty=1, location_id=None, is_proxy=False, is_pending=False):
    row = InventoryRow(
        card_id=card.id,
        user_id=user_id,
        quantity=qty,
        finish="normal",
        is_proxy=is_proxy,
        is_pending=is_pending,
        storage_location_id=location_id,
    )
    db.add(row)
    db.flush()
    return row


def _showcase_share(db, owner_id, playgroup_id, row):
    """Give the owner an active Share to the playgroup with one offered row."""
    sc = Showcase(user_id=owner_id, name="Trades")
    db.add(sc)
    db.flush()
    db.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1))
    db.add(Share(user_id=owner_id, showcase_id=sc.id, playgroup_id=playgroup_id))
    db.flush()
    return sc


def _setup(db, *, with_showcase=True):
    """owner wants Rhystic Study; viewer owns one tradeable copy. Co-members."""
    owner = _user(db, "Owner")
    viewer = _user(db, "Viewer")
    pg = create_playgroup(db, owner.id, "Pod")
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=viewer.id, role="member"))
    rhystic = _card(db, "Rhystic Study")
    db.add(WatchlistItem(user_id=owner.id, card_name="Rhystic Study"))
    viewer_row = _own(db, viewer.id, rhystic, location_id=_loc(db, viewer.id, "Binder").id)
    if with_showcase:
        owner_row = _own(db, owner.id, _card(db, "Sol Ring"), location_id=None)
        _showcase_share(db, owner.id, pg.id, owner_row)
    db.commit()
    return owner, viewer, pg, rhystic, viewer_row


# --------------------------------------------------------------------------- #
# Part 1 — propose-from-wishlist
# --------------------------------------------------------------------------- #


def test_resolve_prefills_offered_from_wishlist_intersection(db):
    owner, viewer, pg, _rhystic, viewer_row = _setup(db)

    resolved = ts.resolve_propose_from_wishlist(db, viewer.id, owner.id)
    assert resolved is not None
    assert resolved["recipient"].id == owner.id
    assert resolved["playgroup"].id == pg.id
    # The card the owner wants and the viewer owns — nothing else.
    assert resolved["offered_row_ids"] == [viewer_row.id]


def test_no_target_without_a_showcase_share(db):
    """C2 precheck: no Share to a shared playgroup → no target, so the button
    is hidden rather than landing the user on a page create_trade would reject."""
    owner, viewer, _pg, _rhystic, _row = _setup(db, with_showcase=False)

    assert ts.wishlist_propose_targets(db, viewer.id) == {}
    assert ts.resolve_propose_from_wishlist(db, viewer.id, owner.id) is None
    # Self-trade is never a target either.
    assert ts.resolve_propose_from_wishlist(db, viewer.id, viewer.id) is None


def test_prefill_skips_proxies_pending_and_untradeable_copies(db):
    """Only placed, real, tradeable (managed/sink) copies are auto-offered — a
    deck/display copy would mean breaking something, so it stays hand-added."""
    owner, viewer, _pg, rhystic, viewer_row = _setup(db)
    deck_loc = _loc(db, viewer.id, "Atraxa", mode="manual", type_="deck")
    _own(db, viewer.id, rhystic, location_id=deck_loc.id)
    _own(db, viewer.id, rhystic, is_proxy=True, location_id=_loc(db, viewer.id, "Proxies").id)
    _own(db, viewer.id, rhystic, is_pending=True)
    db.commit()

    resolved = ts.resolve_propose_from_wishlist(db, viewer.id, owner.id)
    assert resolved["offered_row_ids"] == [viewer_row.id]


def test_prefilled_offered_reaches_the_construction_page(db, client, user):
    """The route hands the template the row ids; A6 is untouched — the
    requested side is still empty and must be picked by hand."""
    owner = _user(db, "Owner")
    pg = create_playgroup(db, owner.id, "Pod")
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=user.id, role="member"))
    rhystic = _card(db, "Rhystic Study")
    db.add(WatchlistItem(user_id=owner.id, card_name="Rhystic Study"))
    row = _own(db, user.id, rhystic, location_id=_loc(db, user.id, "Binder").id)
    _showcase_share(db, owner.id, pg.id, _own(db, owner.id, _card(db, "Sol Ring")))
    db.commit()

    resp = client.get(f"/trades/new?from_wishlist_user={owner.id}")
    assert resp.status_code == 200
    body = resp.text
    offered = body[body.index("offered-grid") :]
    assert re.search(
        rf'trade-pick-item trade-pick-prefilled"\s+data-inventory-row-id="{row.id}"', offered
    )
    # A6 untouched — nothing is prefilled on the requested side.
    requested = body[body.index("requested-grid") : body.index("offered-grid")]
    assert "trade-pick-prefilled" not in requested


# --------------------------------------------------------------------------- #
# Part 2 — [pending] coverage flag
# --------------------------------------------------------------------------- #


def _propose(db, proposer, owner, pg, offered_row, showcase_item_id):
    return ts.create_trade(
        db,
        proposer_user_id=proposer.id,
        recipient_user_id=owner.id,
        playgroup_id=pg.id,
        offered=[{"inventory_row_id": offered_row.id, "quantity": 1}],
        requested=[{"showcase_item_id": showcase_item_id, "quantity": 1}],
    )


def test_pending_names_cover_proposed_and_accepted(db):
    owner, viewer, pg, _rhystic, viewer_row = _setup(db)
    si = db.query(ShowcaseItem).first()

    assert ts.pending_offer_names_for_playgroup(db, pg.id, [owner.id]) == {}

    trade = _propose(db, viewer, owner, pg, viewer_row, si.id)
    db.commit()
    assert ts.pending_offer_names_for_playgroup(db, pg.id, [owner.id]) == {
        owner.id: {"rhystic study"}
    }

    # Recording-only (B1): accepting does not clear the owner's wishlist row,
    # so the "already being provided" signal must survive acceptance.
    ts.transition_trade(db, trade.id, owner.id, "accepted")
    db.commit()
    assert ts.pending_offer_names_for_playgroup(db, pg.id, [owner.id]) == {
        owner.id: {"rhystic study"}
    }

    # A terminal non-agreement clears it.
    trade2 = _propose(db, viewer, owner, pg, viewer_row, si.id)
    db.commit()
    ts.transition_trade(db, trade2.id, owner.id, "declined")
    db.query(ts.Trade).filter(ts.Trade.id == trade.id).update({"status": "declined"})
    db.commit()
    assert ts.pending_offer_names_for_playgroup(db, pg.id, [owner.id]) == {}


def test_playgroup_view_flags_pending_and_never_leaks_who(db):
    owner, viewer, pg, _rhystic, viewer_row = _setup(db)
    ws.share_wishlist_to_playgroup(db, owner.id, pg.id)
    si = db.query(ShowcaseItem).first()
    _propose(db, viewer, owner, pg, viewer_row, si.id)
    db.commit()

    rows = ws.list_wishlist_shares_for_playgroup(db, viewer.id, pg.id)
    (entry,) = [r for r in rows if r["sharer"].id == owner.id]
    (card,) = entry["view"]["cards"]
    assert card["trade_pending"] is True
    assert entry["view"]["can_propose"] is True
    # Aggregate only — no proposer identity, no quantity anywhere in the card.
    assert "proposer" not in card and "quantity" not in card

    # The public projection carries no pending signal at all (anonymous surface).
    public = ws.build_public_wishlist_view(db, owner.id)
    assert "trade_pending" not in public["cards"][0]


def test_offered_inventory_carries_its_location(db):
    """Each offered copy reports WHERE it lives, so a deck card is visible as
    one before it's offered (SaintWacko, 2026-07-22).

    Labels come from the decklist checker's shared builder — not a second
    format — and the recipient's side gets none of this: their storage is
    private, and the sanitized card projection is all that crosses.
    """
    owner, viewer, pg, rhystic, _row = _setup(db)
    deck = _loc(db, viewer.id, "Atraxa", mode="manual", type_="deck")
    _own(db, viewer.id, _card(db, "Cultivate"), location_id=deck.id)
    _own(db, viewer.id, _card(db, "Llanowar Elves"), location_id=None)
    db.commit()

    opts = ts.get_construction_options(db, viewer.id, owner.id, pg.id)
    by_label = {
        it["card"].name: (it["location_label"], it["location_type"])
        for it in opts["proposer_inventory"]
    }
    assert by_label["Cultivate"] == ("Deck · Atraxa", "deck")
    assert by_label["Rhystic Study"] == ("Binder · Binder", "binder")
    # Placed but unlocated reads as Unassigned, never a crash or empty string.
    assert by_label["Llanowar Elves"] == ("Unassigned", "")

    # The requested side exposes no location field at all.
    for item in opts["recipient_share_items"]:
        assert "location_label" not in item


def test_completed_trade_items_keep_their_scryfall_id(db):
    """A terminal (completed) trade must still render card ART.

    Regression, reported by SaintWacko 2026-07-22: the post-terminal
    ``_SnapshotCardProjection`` exposed ``image_url`` but NOT ``scryfall_id``,
    and the templates guard on the former while building the mirror src from
    the latter — so every card on a completed trade rendered as a blank frame.
    Jinja converts the missing attribute to Undefined instead of raising, which
    is why the projection's "fails loudly" contract did not catch it. Same class
    of bug as the ``_SHARE_CARD_FIELDS`` omission fixed in v4.11.36.
    """
    owner, viewer, pg, _rhystic, viewer_row = _setup(db)
    si = db.query(ShowcaseItem).first()
    trade = _propose(db, viewer, owner, pg, viewer_row, si.id)
    db.commit()

    ts.transition_trade(db, trade.id, owner.id, "accepted")
    db.commit()

    view = ts.get_trade_detail(db, trade.id, owner.id)
    items = view["offered_items"] + view["requested_items"]
    assert items, "a completed trade should still project its items"
    for item in items:
        card = item["card"]
        # The pair is load-bearing together: an image_url with no scryfall_id
        # builds ".../normal.jpg" and 404s.
        assert card.scryfall_id, f"{card.name} lost its scryfall_id"
        assert card.image_url is None or card.scryfall_id
