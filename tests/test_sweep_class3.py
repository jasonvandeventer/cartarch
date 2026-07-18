"""#136 Class 3 sweep — fabricated pending real rows from the is_proxy-blind
return path (now fixed by #140). The buggy path no longer produces these, so the
tests build the minted rows directly and exercise detect -> select -> apply.
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.legacy_tables  # noqa: F401
from app.db import Base
from app.models import Card, Deck, InventoryRow, ShowcaseItem, StorageLocation, TransactionLog, User
from scripts.sweep_class3_fabricated_returns import (
    CORRECTION_EVENT,
    apply_class3,
    detect_class1,
    detect_class2,
    detect_class3,
    select_for_deletion,
)

_seq = itertools.count(1)


def _fresh():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    return sessionmaker(bind=e, expire_on_commit=False)


def _user(s):
    u = User(username=f"u{next(_seq)}", password_hash="x")
    s.add(u)
    s.flush()
    return u


def _card(s, name):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}", name=name, set_code="tst", collector_number=str(next(_seq))
    )
    s.add(c)
    s.flush()
    return c


def _brew(s, user_id, name):
    return __deck(s, user_id, name, True)


def __deck(s, user_id, name, is_brew):
    loc = StorageLocation(user_id=user_id, name=f"{name} (deck)", type="deck", mode="managed")
    s.add(loc)
    s.flush()
    d = Deck(user_id=user_id, name=name, storage_location_id=loc.id, is_brew=is_brew)
    s.add(d)
    s.flush()
    return d


def _fabricated_row(s, user_id, card, deck, qty=1):
    """A pending, non-proxy row + the return_from_deck tx that minted it — the
    exact shape the is_proxy-blind return path left behind."""
    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=qty,
        finish="normal",
        is_proxy=False,
        storage_location_id=None,
        is_pending=True,
    )
    s.add(row)
    s.flush()
    s.add(
        TransactionLog(
            user_id=user_id,
            event_type="return_from_deck",
            card_id=card.id,
            finish="normal",
            quantity_delta=qty,
            source_location=f"deck:{deck.name}",
            inventory_row_id=row.id,
        )
    )
    s.flush()
    return row


def _placed_real(s, user_id, card):
    loc = StorageLocation(user_id=user_id, name=f"Box{next(_seq)}", type="box", mode="managed")
    s.add(loc)
    s.flush()
    r = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=1,
        finish="normal",
        is_proxy=False,
        storage_location_id=loc.id,
        is_pending=False,
    )
    s.add(r)
    s.flush()
    return r


def test_detect_class3_and_confidence():
    s = _fresh()()
    u = _user(s)
    brew = _brew(s, u.id, "Silverquill Confluence")
    c_hi = _card(s, "Spirit Link")
    c_amb = _card(s, "Eye of Nidhogg")
    hi = _fabricated_row(s, u.id, c_hi, brew)
    amb = _fabricated_row(s, u.id, c_amb, brew)
    _placed_real(s, u.id, c_amb)  # gives amb other_real_rows = 1
    s.commit()

    rows = detect_class3(s)
    by_id = {r.row_id: r for r in rows}
    assert set(by_id) == {hi.id, amb.id}
    assert by_id[hi.id].other_real_rows == 0 and by_id[hi.id].ambiguous is False
    assert by_id[amb.id].other_real_rows == 1 and by_id[amb.id].ambiguous is True
    # Class 1 & 2 empty in this setup
    assert detect_class1(s) == [] and detect_class2(s) == []


def test_select_defaults_ambiguous_to_opt_out():
    s = _fresh()()
    u = _user(s)
    brew = _brew(s, u.id, "Dealer's Choice")
    c_hi, c_amb = _card(s, "Equipoise"), _card(s, "Priority Boarding")
    hi = _fabricated_row(s, u.id, c_hi, brew)
    amb = _fabricated_row(s, u.id, c_amb, brew)
    _placed_real(s, u.id, c_amb)
    s.commit()
    rows = detect_class3(s)

    to_del, skipped = select_for_deletion(rows, exclude_ids=set(), include_ambiguous=False)
    assert [r.row_id for r in to_del] == [hi.id]
    assert amb.id in [r.row_id for r, _ in skipped]

    # --include-ambiguous sweeps it too
    to_del2, _ = select_for_deletion(rows, exclude_ids=set(), include_ambiguous=True)
    assert {r.row_id for r in to_del2} == {hi.id, amb.id}

    # --exclude opts a specific row out
    to_del3, skip3 = select_for_deletion(rows, exclude_ids={hi.id}, include_ambiguous=True)
    assert hi.id not in [r.row_id for r in to_del3]
    assert hi.id in [r.row_id for r, _ in skip3]


def test_apply_deletes_logs_and_is_fk_safe():
    s = _fresh()()
    u = _user(s)
    brew = _brew(s, u.id, "Silverquill Confluence")
    card = _card(s, "Ethereal Armor")
    row = _fabricated_row(s, u.id, card, brew)
    # a dangling ShowcaseItem on the row proves clean_inventory_row_references runs
    from app.share_service import get_or_create_showcase

    sc = get_or_create_showcase(s, u.id)
    s.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1))
    s.commit()
    rid = row.id

    to_del, _ = select_for_deletion(detect_class3(s), exclude_ids=set(), include_ambiguous=False)
    n = apply_class3(s, to_del)
    s.commit()

    assert n == 1
    assert s.get(InventoryRow, rid) is None  # fabricated row deleted
    assert s.query(ShowcaseItem).filter_by(inventory_row_id=rid).count() == 0  # FK-safe cleanup ran
    logs = s.query(TransactionLog).filter_by(event_type=CORRECTION_EVENT).all()
    assert len(logs) == 1
    assert logs[0].inventory_row_id == rid and logs[0].quantity_delta == -1  # audited
    assert detect_class3(s) == []  # nothing left to sweep


def test_non_brew_return_is_not_class3():
    """A return from a NON-brew deck is a legitimate owned card returning; it must
    NOT be swept."""
    s = _fresh()()
    u = _user(s)
    normal = __deck(s, u.id, "Normal", False)
    card = _card(s, "Sol Ring")
    _fabricated_row(s, u.id, card, normal)  # same shape, but source deck isn't a brew
    s.commit()
    assert detect_class3(s) == []
