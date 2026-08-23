"""Trade counter-proposals (SaintWacko, 2026-08-21).

"Rather than a trade being fixed when it is created, allow either participant to
modify the trade and issue it as a counter-proposal. Both participants would be
able to see a diff of what was changed, and either accept or decline it, in
which case the trade would return to its original state."

Owner decisions, 2026-08-21:
  * a counter appends a REVISION to the same trade (not a new trade, not an
    in-place mutation with a change log);
  * either party may counter, without limit;
  * a side may be EMPTY on a counter — but NOT on an opening proposal, so the
    wishlist flow's no-one-sided-gift rule (A6) is untouched.
"""

from __future__ import annotations

import itertools
import json
import re

import pytest

from app import trade_service as ts
from app.models import (
    Card,
    InventoryRow,
    PlaygroupMember,
    Share,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    TradeItem,
    User,
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
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line="Creature",
        image_url="https://img.example.invalid/x.jpg",
    )
    db.add(c)
    db.flush()
    return c


def _own(db, user_id, card, loc, qty=1):
    row = InventoryRow(
        card_id=card.id,
        user_id=user_id,
        quantity=qty,
        finish="normal",
        is_pending=False,
        storage_location_id=loc.id,
    )
    db.add(row)
    db.flush()
    return row


def _loc(db, user_id, name):
    loc = StorageLocation(user_id=user_id, name=name, type="binder", mode="managed")
    db.add(loc)
    db.flush()
    return loc


def _share(db, owner, pg, rows):
    sc = Showcase(user_id=owner.id, name=f"SC{next(_seq)}")
    db.add(sc)
    db.flush()
    items = []
    for row in rows:
        si = ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=row.quantity)
        db.add(si)
        items.append(si)
    db.add(Share(user_id=owner.id, showcase_id=sc.id, playgroup_id=pg.id))
    db.flush()
    return sc, items


@pytest.fixture
def world(db):
    """Two co-members, both sharing a showcase, and a live proposed trade.

    The PROPOSER shares one too — otherwise a countering recipient would have
    nothing of theirs to add, which is its own (tested) case.
    """
    proposer = _user(db, "Prop")
    recipient = _user(db, "Recip")
    pg = create_playgroup(db, proposer.id, "Pod")
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=recipient.id, role="member"))

    p_loc, r_loc = _loc(db, proposer.id, "P"), _loc(db, recipient.id, "R")
    p_sol = _own(db, proposer.id, _card(db, "Sol Ring"), p_loc, qty=3)
    p_mana = _own(db, proposer.id, _card(db, "Mana Crypt"), p_loc)
    p_secret = _own(db, proposer.id, _card(db, "Secret Card"), p_loc)  # not shared
    r_rhystic = _own(db, recipient.id, _card(db, "Rhystic Study"), r_loc)
    r_mystic = _own(db, recipient.id, _card(db, "Mystic Remora"), r_loc)

    _sc_p, p_items = _share(db, proposer, pg, [p_sol, p_mana])
    _sc_r, r_items = _share(db, recipient, pg, [r_rhystic, r_mystic])
    db.commit()

    trade = ts.create_trade(
        db,
        proposer_user_id=proposer.id,
        recipient_user_id=recipient.id,
        playgroup_id=pg.id,
        offered=[{"inventory_row_id": p_secret.id, "quantity": 1}],
        requested=[{"showcase_item_id": r_items[0].id, "quantity": 1}],
    )
    return {
        "proposer": proposer,
        "recipient": recipient,
        "pg": pg,
        "trade": trade,
        "p_sol": p_sol,
        "p_secret": p_secret,
        "p_share_items": p_items,
        "r_share_items": r_items,
    }


def _names(items):
    return sorted(i.card.name for i in items)


# --------------------------------------------------------------------------
# The revision model
# --------------------------------------------------------------------------


def test_a_proposal_has_one_revision_and_a_counter_appends_another(db, world):
    trade = world["trade"]
    assert len(trade.revisions) == 1
    assert trade.revisions[0].author_user_id == world["proposer"].id

    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[{"trade_item_id": ts._items_by_side(trade, "offered")[0].id, "quantity": 1}],
        requested=[{"showcase_item_id": world["r_share_items"][1].id, "quantity": 1}],
        note="I'd rather trade the Remora",
    )
    db.refresh(trade)

    assert len(trade.revisions) == 2
    assert ts.current_revision(trade).author_user_id == world["recipient"].id
    # The trade itself is untouched — same id, same status, same inbox.
    assert trade.status == "proposed"
    # The page shows ONLY the current revision.
    assert _names(ts._items_by_side(trade, "requested")) == ["Mystic Remora"]
    # ...and the superseded items are still on disk, which is what the diff and
    # the fall-back are built from.
    assert len(trade.items) == 4


def test_the_detail_view_and_totals_see_only_the_current_revision(db, world):
    trade = world["trade"]
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["proposer"].id,
        offered=[{"inventory_row_id": world["p_sol"].id, "quantity": 2}],
        requested=[{"showcase_item_id": world["r_share_items"][0].id, "quantity": 1}],
    )
    detail = ts.get_trade_detail(db, world["recipient"].id, trade.id)
    assert [i["card"].name for i in detail["offered_items"]] == ["Sol Ring"]
    assert len(detail["requested_items"]) == 1


# --------------------------------------------------------------------------
# Who may counter, and with what
# --------------------------------------------------------------------------


def test_the_recipient_can_keep_a_card_they_cannot_name(db, world):
    """The proposer offered a card that is NOT in their shared showcase. The
    recipient can still hold on to it when countering, by the trade's own line —
    otherwise every counter would silently drop it."""
    trade = world["trade"]
    kept = ts._items_by_side(trade, "offered")[0]
    assert kept.card.name == "Secret Card"

    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[
            {"trade_item_id": kept.id, "quantity": 1},
            {"showcase_item_id": world["p_share_items"][0].id, "quantity": 2},
        ],
        requested=[{"showcase_item_id": world["r_share_items"][0].id, "quantity": 1}],
    )
    db.refresh(trade)
    assert _names(ts._items_by_side(trade, "offered")) == ["Secret Card", "Sol Ring"]


def test_a_counter_cannot_reach_into_inventory_its_author_cannot_see(db, world):
    """The recipient naming the proposer's raw row id is refused — that row is
    not theirs to name, and the showcase path is the one they are allowed."""
    with pytest.raises(ValueError, match="don't own|not in their Showcase"):
        ts.counter_trade(
            db,
            trade_id=world["trade"].id,
            author_user_id=world["recipient"].id,
            offered=[{"showcase_item_id": world["r_share_items"][0].id, "quantity": 1}],
            requested=[{"showcase_item_id": world["r_share_items"][0].id, "quantity": 1}],
        )


def test_a_stranger_cannot_counter(db, world):
    outsider = _user(db, "Nosy")
    db.commit()
    with pytest.raises(ValueError, match="party to the trade"):
        ts.counter_trade(
            db,
            trade_id=world["trade"].id,
            author_user_id=outsider.id,
            offered=[],
            requested=[{"showcase_item_id": world["r_share_items"][0].id, "quantity": 1}],
        )


def test_a_closed_trade_cannot_be_countered(db, world):
    trade = world["trade"]
    ts.transition_trade(
        db, trade_id=trade.id, actor_user_id=world["recipient"].id, new_status="accepted"
    )
    with pytest.raises(ValueError, match="can no longer be countered"):
        ts.counter_trade(
            db,
            trade_id=trade.id,
            author_user_id=world["proposer"].id,
            offered=[{"inventory_row_id": world["p_sol"].id, "quantity": 1}],
            requested=[{"showcase_item_id": world["r_share_items"][0].id, "quantity": 1}],
        )


def test_an_identical_counter_is_refused(db, world):
    """Nothing changed, so there is nothing to answer — and a "they countered"
    badge on an unchanged trade is a lie."""
    trade = world["trade"]
    with pytest.raises(ValueError, match="identical"):
        ts.counter_trade(
            db,
            trade_id=trade.id,
            author_user_id=world["proposer"].id,
            offered=[{"inventory_row_id": world["p_secret"].id, "quantity": 1}],
            requested=[{"showcase_item_id": world["r_share_items"][0].id, "quantity": 1}],
        )


# --------------------------------------------------------------------------
# The empty side — allowed on a counter, never on an opening proposal
# --------------------------------------------------------------------------


def test_a_counter_may_empty_one_side(db, world):
    """ "Actually, just take it" is a real trade."""
    trade = world["trade"]
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[{"trade_item_id": ts._items_by_side(trade, "offered")[0].id, "quantity": 1}],
        requested=[],
    )
    db.refresh(trade)
    assert ts._items_by_side(trade, "requested") == []
    assert len(ts._items_by_side(trade, "offered")) == 1


def test_a_counter_may_not_empty_BOTH_sides(db, world):
    with pytest.raises(ValueError, match="at least one card"):
        ts.counter_trade(
            db,
            trade_id=world["trade"].id,
            author_user_id=world["proposer"].id,
            offered=[],
            requested=[],
        )


def test_an_opening_proposal_still_needs_both_sides(db, world):
    """A6 holds where it was made: the wishlist "propose a trade" flow relies on
    it to prevent a one-sided gift trade."""
    with pytest.raises(ValueError, match="(?i)at least one requested item"):
        ts.create_trade(
            db,
            proposer_user_id=world["proposer"].id,
            recipient_user_id=world["recipient"].id,
            playgroup_id=world["pg"].id,
            offered=[{"inventory_row_id": world["p_sol"].id, "quantity": 1}],
            requested=[],
        )


# --------------------------------------------------------------------------
# Declining a counter returns the trade to its previous state
# --------------------------------------------------------------------------


def test_declining_a_counter_restores_the_previous_version(db, world):
    trade = world["trade"]
    before = _names(ts._items_by_side(trade, "requested"))
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[{"trade_item_id": ts._items_by_side(trade, "offered")[0].id, "quantity": 1}],
        requested=[{"showcase_item_id": world["r_share_items"][1].id, "quantity": 1}],
    )
    db.refresh(trade)
    assert _names(ts._items_by_side(trade, "requested")) != before

    ts.decline_counter(db, trade.id, world["proposer"].id)
    db.refresh(trade)

    assert _names(ts._items_by_side(trade, "requested")) == before
    assert trade.status == "proposed", "declining a COUNTER must not close the trade"
    # The rejected version is kept — it is the record of what was refused.
    assert len(trade.revisions) == 2
    assert trade.revisions[-1].declined_at is not None


def test_you_cannot_decline_your_own_counter(db, world):
    trade = world["trade"]
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[],
        requested=[{"showcase_item_id": world["r_share_items"][1].id, "quantity": 1}],
    )
    with pytest.raises(ValueError, match="your own"):
        ts.decline_counter(db, trade.id, world["recipient"].id)


def test_there_is_nothing_to_decline_on_an_uncountered_trade(db, world):
    """Rejecting the ORIGINAL proposal is `transition_trade(..., "declined")` —
    a different, terminal thing."""
    with pytest.raises(ValueError, match="no counter-proposal"):
        ts.decline_counter(db, world["trade"].id, world["recipient"].id)


# --------------------------------------------------------------------------
# Who answers the version on the table
# --------------------------------------------------------------------------


def test_after_a_recipients_counter_the_PROPOSER_accepts(db, world):
    trade = world["trade"]
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[{"trade_item_id": ts._items_by_side(trade, "offered")[0].id, "quantity": 1}],
        requested=[{"showcase_item_id": world["r_share_items"][1].id, "quantity": 1}],
    )
    # The author cannot accept their own counter...
    with pytest.raises(ValueError, match="did not propose this version"):
        ts.transition_trade(
            db, trade_id=trade.id, actor_user_id=world["recipient"].id, new_status="accepted"
        )
    # ...the other party does.
    ts.transition_trade(
        db, trade_id=trade.id, actor_user_id=world["proposer"].id, new_status="accepted"
    )
    db.refresh(trade)
    assert trade.status == "accepted"


def test_an_uncountered_trade_keeps_the_original_actor_rules(db, world):
    """The generalisation must reduce EXACTLY to the old rule on revision 1,
    where the author is the proposer."""
    trade = world["trade"]
    with pytest.raises(ValueError, match="did not propose this version"):
        ts.transition_trade(
            db, trade_id=trade.id, actor_user_id=world["proposer"].id, new_status="accepted"
        )
    with pytest.raises(ValueError, match="proposed this version"):
        ts.transition_trade(
            db, trade_id=trade.id, actor_user_id=world["recipient"].id, new_status="cancelled"
        )


# --------------------------------------------------------------------------
# The diff
# --------------------------------------------------------------------------


def test_the_diff_reports_added_removed_and_quantity(db, world):
    trade = world["trade"]
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["proposer"].id,
        # Sol Ring x2 added, Secret Card dropped, Rhystic kept at the same qty.
        offered=[{"inventory_row_id": world["p_sol"].id, "quantity": 2}],
        requested=[{"showcase_item_id": world["r_share_items"][0].id, "quantity": 1}],
    )
    db.refresh(trade)
    diff = ts.trade_revision_diff(trade)

    assert diff["has_diff"] is True
    changes = {(r["item"].card.name, r["change"]) for r in diff["offered"]}
    assert ("Sol Ring", "added") in changes
    assert ("Secret Card", "removed") in changes
    assert [r["change"] for r in diff["requested"]] == ["unchanged"]


def test_a_quantity_only_change_reads_as_a_quantity_change(db, world):
    """Matching is on the ROW, not the trade-item id — every revision writes
    fresh item rows, so an id-based diff would call this both an add and a
    remove."""
    trade = world["trade"]
    # Counter once to put Sol Ring x1 on the table...
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["proposer"].id,
        offered=[{"inventory_row_id": world["p_sol"].id, "quantity": 1}],
        requested=[{"showcase_item_id": world["r_share_items"][0].id, "quantity": 1}],
    )
    db.refresh(trade)
    # ...then counter again with the SAME card at a different quantity.
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[{"trade_item_id": ts._items_by_side(trade, "offered")[0].id, "quantity": 3}],
        requested=[{"showcase_item_id": world["r_share_items"][0].id, "quantity": 1}],
    )
    db.refresh(trade)
    diff = ts.trade_revision_diff(trade)
    (row,) = diff["offered"]
    assert (row["change"], row["was_quantity"], row["item"].quantity) == ("quantity", 1, 3)


def test_an_uncountered_trade_has_no_diff(db, world):
    assert ts.trade_revision_diff(world["trade"])["has_diff"] is False


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def _as(client, user):
    """Point the client's pinned current_user at `user` (the fixture pins one)."""
    from app import main
    from app.dependencies import get_current_user

    main.app.dependency_overrides[get_current_user] = lambda: user
    return client


def test_the_counter_editor_renders_for_both_parties(client, db, world, user):
    trade = world["trade"]
    for party in ("proposer", "recipient"):
        page = _as(client, world[party]).get(f"/trades/{trade.id}/counter")
        assert page.status_code == 200, party
        # #184 — the shared picker's restore blob, hydrated server-side.
        assert "pick-restore" in page.text
        # It opens ON the trade, not on an empty form.
        assert '"quantity": 1' in page.text


def test_a_non_party_is_redirected_not_403(client, db, world):
    outsider = _user(db, "Nosy")
    db.commit()
    resp = _as(client, outsider).get(f"/trades/{world['trade'].id}/counter", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/trades?error=trade_unavailable"


def test_posting_a_counter_moves_the_trade_to_the_new_version(client, db, world):
    trade = world["trade"]
    resp = _as(client, world["recipient"]).post(
        f"/trades/{trade.id}/counter",
        data={
            "offered_json": json.dumps(
                [{"trade_item_id": ts._items_by_side(trade, "offered")[0].id, "quantity": 1}]
            ),
            "requested_json": json.dumps(
                [{"showcase_item_id": world["r_share_items"][1].id, "quantity": 1}]
            ),
            "note": "swap please",
            "csrf_token": "x",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/trades/{trade.id}?success=countered"
    db.refresh(trade)
    assert len(trade.revisions) == 2
    assert ts.current_revision(trade).note == "swap please"


def test_a_rejected_counter_keeps_its_picks_too(client, db, world):
    """Same rule as a rejected proposal — the error lands on the page holding
    the work."""
    trade = world["trade"]
    resp = _as(client, world["proposer"]).post(
        f"/trades/{trade.id}/counter",
        data={
            "offered_json": json.dumps([]),
            "requested_json": json.dumps([]),
            "note": "",
            "csrf_token": "x",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "at least one card" in resp.text.lower()


def test_the_detail_page_shows_the_diff_and_the_decline_control(client, db, world):
    trade = world["trade"]
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[{"trade_item_id": ts._items_by_side(trade, "offered")[0].id, "quantity": 1}],
        requested=[{"showcase_item_id": world["r_share_items"][1].id, "quantity": 1}],
    )
    page = _as(client, world["proposer"]).get(f"/trades/{trade.id}")
    assert page.status_code == 200
    assert "Counter-proposal from" in page.text
    assert f"/trades/{trade.id}/decline-counter" in page.text
    assert "Mystic Remora" in page.text

    # The author of the counter is not offered the control to decline it.
    own = _as(client, world["recipient"]).get(f"/trades/{trade.id}")
    assert "decline-counter" not in own.text


def test_declining_through_the_route_restores_and_stays_open(client, db, world):
    trade = world["trade"]
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[],
        requested=[{"showcase_item_id": world["r_share_items"][1].id, "quantity": 1}],
    )
    resp = _as(client, world["proposer"]).post(
        f"/trades/{trade.id}/decline-counter",
        data={"csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(trade)
    assert trade.status == "proposed"
    assert len(ts._items_by_side(trade, "offered")) == 1


def test_a_countered_trade_still_carries_one_item_set_into_its_snapshot(db, world):
    """The terminal snapshot is written from the CURRENT revision — a countered
    trade must not freeze both versions into one oversized record."""
    trade = world["trade"]
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[{"trade_item_id": ts._items_by_side(trade, "offered")[0].id, "quantity": 1}],
        requested=[{"showcase_item_id": world["r_share_items"][1].id, "quantity": 1}],
    )
    ts.transition_trade(
        db, trade_id=trade.id, actor_user_id=world["proposer"].id, new_status="accepted"
    )
    db.refresh(trade)
    snapshotted = [
        i
        for i in db.query(TradeItem).filter(TradeItem.trade_id == trade.id).all()
        if i.card_name_at_trade
    ]
    assert sorted(i.card_name_at_trade for i in snapshotted) == ["Mystic Remora", "Secret Card"]


def test_a_card_already_on_the_trade_is_not_offered_twice(client, db, world):
    """The recipient's offered pane lists the trade's OWN lines (the only handle
    for a card the proposer does not share) alongside the proposer's shared
    showcase. A card in both reached the list twice under two identities, and
    picking both would have put one physical card on the trade twice.
    """
    trade = world["trade"]
    # Share the very card that is already offered, so it qualifies for both.
    from app.models import Share, Showcase, ShowcaseItem

    offered_row_id = ts._items_by_side(trade, "offered")[0].inventory_row_id
    sc = db.query(Showcase).filter(Showcase.user_id == world["proposer"].id).first()
    db.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=offered_row_id, quantity_offered=1))
    if not db.query(Share).filter(Share.showcase_id == sc.id).first():
        db.add(Share(user_id=world["proposer"].id, showcase_id=sc.id, playgroup_id=world["pg"].id))
    db.commit()

    page = _as(client, world["recipient"]).get(f"/trades/{trade.id}/counter")
    assert page.status_code == 200
    rows = [
        m for m in re.findall(r'data-pick-kind="([^"]+)" *\n? *data-pick-id="(\d+)"', page.text)
    ]
    # The card appears once, as the trade's own line.
    line_id = ts._items_by_side(trade, "offered")[0].id
    assert ("trade_item_id", str(line_id)) in rows
    names = re.findall(r'data-name="([^"]+)"', page.text)
    assert names.count("Secret Card") == 1, f"listed twice: {names}"


def test_the_page_offers_ACCEPT_to_whoever_must_answer_the_current_version(client, db, world):
    """Reported 2026-08-23: "the accept trade button is missing now".

    It was missing for the person entitled to press it and present for the
    person the server refuses. `transition_trade` has gated on "did NOT write
    the current revision" since counters landed; the PAGE still asked "am I the
    recipient", so after a recipient's counter the proposer — the one who must
    now answer — saw no Accept at all, while the recipient saw one that errored.

    The uncountered case must be unchanged: recipient accepts, proposer cancels.
    """
    trade = world["trade"]

    # 1. Uncountered: exactly as before.
    recip = _as(client, world["recipient"]).get(f"/trades/{trade.id}").text
    assert f"/trades/{trade.id}/accept" in recip
    prop = _as(client, world["proposer"]).get(f"/trades/{trade.id}").text
    assert f"/trades/{trade.id}/accept" not in prop
    assert f"/trades/{trade.id}/cancel" in prop

    # 2. The RECIPIENT counters — now the PROPOSER answers.
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[{"trade_item_id": ts._items_by_side(trade, "offered")[0].id, "quantity": 1}],
        requested=[{"showcase_item_id": world["r_share_items"][1].id, "quantity": 1}],
    )
    db.refresh(trade)

    prop = _as(client, world["proposer"]).get(f"/trades/{trade.id}").text
    assert f"/trades/{trade.id}/accept" in prop, (
        "the button is missing for the party who must answer"
    )
    assert "counter-proposal" in prop.lower()

    author = _as(client, world["recipient"]).get(f"/trades/{trade.id}").text
    assert f"/trades/{trade.id}/accept" not in author, "you cannot accept your own counter"
    assert f"/trades/{trade.id}/cancel" in author, "the author withdraws instead"


def test_the_page_and_the_service_agree_about_who_answers(client, db, world):
    """The page must not offer an action the service will refuse — that is the
    shape of the reported bug, and a template gate that drifts from the service
    gate will always produce it."""
    trade = world["trade"]
    ts.counter_trade(
        db,
        trade_id=trade.id,
        author_user_id=world["recipient"].id,
        offered=[],
        requested=[{"showcase_item_id": world["r_share_items"][1].id, "quantity": 1}],
    )
    db.refresh(trade)

    for party, name in ((world["proposer"], "proposer"), (world["recipient"], "recipient")):
        page = _as(client, party).get(f"/trades/{trade.id}").text
        offered = f"/trades/{trade.id}/accept" in page
        try:
            ts.transition_trade(
                db, trade_id=trade.id, actor_user_id=party.id, new_status="accepted"
            )
            allowed = True
            db.rollback()
        except ValueError:
            allowed = False
        assert offered == allowed, f"{name}: page offers accept={offered}, service allows={allowed}"
