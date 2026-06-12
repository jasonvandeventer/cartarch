"""Proxy valuation tests — ADR proxy-valuation-2026-06-12.

Proxies remain ADDABLE to Showcases/trades, but they carry **$0 market
value** in every Showcase total, share-view total, and trade side total /
per-card price, and bulk-add SKIPS them unless explicitly opted in. The
proxy stays flagged (is_proxy True) for the warning badge.

Isolated in-memory SQLite engine (services take the Session as a param).
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import share_service, trade_service
from app.db import Base
from app.models import (
    Card,
    InventoryRow,
    Playgroup,
    PlaygroupMember,
    ShowcaseItem,
    Trade,
    TradeItem,
)

_seq = itertools.count(1)


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _row(s, user_id, price="10.00", qty=1, is_proxy=False):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name="Test Card",
        set_code="TST",
        collector_number=str(next(_seq)),
        price_usd=price,
    )
    s.add(c)
    s.flush()
    r = InventoryRow(
        card_id=c.id,
        user_id=user_id,
        quantity=qty,
        finish="normal",
        is_pending=False,
        is_proxy=is_proxy,
    )
    s.add(r)
    s.commit()
    return r


def test_proxy_contributes_zero_to_showcase_total():
    s = _session()
    sc = share_service.create_showcase(s, user_id=1, name="Trade", description=None)
    real = _row(s, 1, price="10.00")
    proxy = _row(s, 1, price="10.00", is_proxy=True)
    share_service.add_showcase_item(
        s, user_id=1, inventory_row_id=real.id, showcase_id=sc.id, quantity_offered=1
    )
    share_service.add_showcase_item(
        s, user_id=1, inventory_row_id=proxy.id, showcase_id=sc.id, quantity_offered=1
    )
    data = share_service.get_showcase_with_items(s, user_id=1, showcase_id=sc.id)
    # real $10 + proxy $0 = $10 (NOT $20)
    assert abs(data["total_value"] - 10.00) < 1e-6
    by_proxy = {it["is_proxy"]: it for it in data["items"]}
    assert by_proxy[True]["effective_price"] == 0.0
    assert by_proxy[True]["value"] == 0.0
    assert by_proxy[False]["effective_price"] == 10.0


def test_proxy_contributes_zero_to_share_view_total():
    s = _session()
    sc = share_service.create_showcase(s, user_id=1, name="Trade", description=None)
    real = _row(s, 1, price="10.00")
    proxy = _row(s, 1, price="10.00", is_proxy=True)
    share_service.add_showcase_item(
        s, user_id=1, inventory_row_id=real.id, showcase_id=sc.id, quantity_offered=1
    )
    share_service.add_showcase_item(
        s, user_id=1, inventory_row_id=proxy.id, showcase_id=sc.id, quantity_offered=1
    )
    pg = Playgroup(name="PG", created_by=1, join_code="ABC123")
    s.add(pg)
    s.flush()
    s.add(PlaygroupMember(playgroup_id=pg.id, user_id=1, role="owner"))
    s.commit()
    share = share_service.create_share(s, user_id=1, showcase_id=sc.id, playgroup_id=pg.id)
    view = share_service.get_share_view(s, viewer_user_id=1, share_id=share.id)
    assert abs(view["total_value"] - 10.00) < 1e-6
    items = {it["is_proxy"]: it for it in view["items"]}
    assert items[True]["effective_price"] == 0.0
    assert items[True]["total_value"] == 0.0
    assert items[False]["effective_price"] == 10.0


def test_proxy_contributes_zero_to_both_trade_side_totals():
    s = _session()
    # proposer (1) offers a proxy + a real; recipient (2) has a proxy requested.
    off_proxy = _row(s, 1, price="20.00", is_proxy=True)
    off_real = _row(s, 1, price="5.00")
    req_proxy = _row(s, 2, price="34.45", is_proxy=True)
    sc2 = share_service.create_showcase(s, user_id=2, name="theirs", description=None)
    si = share_service.add_showcase_item(
        s, user_id=2, inventory_row_id=req_proxy.id, showcase_id=sc2.id, quantity_offered=1
    )
    t = Trade(proposer_user_id=1, recipient_user_id=2, status="proposed")
    s.add(t)
    s.flush()
    s.add(
        TradeItem(
            trade_id=t.id,
            side="offered",
            inventory_row_id=off_proxy.id,
            card_id=off_proxy.card_id,
            finish="normal",
            quantity=1,
        )
    )
    s.add(
        TradeItem(
            trade_id=t.id,
            side="offered",
            inventory_row_id=off_real.id,
            card_id=off_real.card_id,
            finish="normal",
            quantity=1,
        )
    )
    s.add(
        TradeItem(
            trade_id=t.id,
            side="requested",
            inventory_row_id=req_proxy.id,
            card_id=req_proxy.card_id,
            showcase_item_id=si.id,
            finish="normal",
            quantity=1,
        )
    )
    s.commit()

    detail = trade_service.get_trade_detail(s, viewer_user_id=1, trade_id=t.id)
    # offered total = proxy($0) + real($5) = $5; requested total = proxy($0) = $0
    assert abs(detail["offered_total"] - 5.00) < 1e-6
    assert abs(detail["requested_total"] - 0.00) < 1e-6
    assert detail["has_proxy"] is True
    off = {it["is_proxy"]: it for it in detail["offered_items"]}
    assert off[True]["effective_price"] == 0.0
    assert off[True]["total_value"] == 0.0
    assert off[False]["effective_price"] == 5.0
    req = detail["requested_items"][0]
    assert req["is_proxy"] is True
    assert req["effective_price"] == 0.0
    assert req["total_value"] == 0.0


def test_bulk_add_skips_proxies_unless_opted_in():
    s = _session()
    sc = share_service.create_showcase(s, user_id=1, name="One", description=None)
    real = _row(s, 1, price="1.00")
    proxy = _row(s, 1, price="1.00", is_proxy=True)

    # Default: proxies are skipped — only the real row is added.
    res = share_service.add_rows_to_showcase(s, user_id=1, showcase_id=sc.id)
    assert res["added"] == 1
    assert s.query(ShowcaseItem).filter(ShowcaseItem.inventory_row_id == proxy.id).first() is None
    assert (
        s.query(ShowcaseItem).filter(ShowcaseItem.inventory_row_id == real.id).first() is not None
    )

    # Opt in: include_proxies=True now adds the proxy (top-up; real already in).
    res2 = share_service.add_rows_to_showcase(s, user_id=1, showcase_id=sc.id, include_proxies=True)
    assert res2["added"] == 1
    assert (
        s.query(ShowcaseItem).filter(ShowcaseItem.inventory_row_id == proxy.id).first() is not None
    )


def test_bulk_add_by_row_ids_skips_proxies_unless_opted_in():
    """The Collection filter-scoped bulk-add (``row_ids`` path) gets the SAME
    proxy-skip default as the Showcase-page sweeps — two bulk paths must not
    disagree on the proxy default (ADR proxy-valuation-2026-06-12)."""
    s = _session()
    sc = share_service.create_showcase(s, user_id=1, name="One", description=None)
    real = _row(s, 1, price="1.00")
    proxy = _row(s, 1, price="1.00", is_proxy=True)

    # Default on the row_ids path: proxy in the id set is skipped.
    res = share_service.add_rows_to_showcase(
        s, user_id=1, showcase_id=sc.id, row_ids=[real.id, proxy.id]
    )
    assert res["added"] == 1
    assert s.query(ShowcaseItem).filter(ShowcaseItem.inventory_row_id == proxy.id).first() is None

    # Opt in on the row_ids path includes it.
    res2 = share_service.add_rows_to_showcase(
        s, user_id=1, showcase_id=sc.id, row_ids=[real.id, proxy.id], include_proxies=True
    )
    assert res2["added"] == 1
    assert (
        s.query(ShowcaseItem).filter(ShowcaseItem.inventory_row_id == proxy.id).first() is not None
    )
