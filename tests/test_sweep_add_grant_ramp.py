"""#139 add-Ramp sweep — aligns persisted tag chips with the fixed tagger for the
cards it now recognizes as ramp (grant-to-you-control + "searches their library").
Narrow: only rows attributable to the #139 change, never overwriting existing tags.
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.deck_service import get_row_tags, set_row_tags
from app.models import Card, InventoryRow, User
from scripts.sweep_add_grant_ramp_tags import _add_ramp, plan

_seq = itertools.count(1)


def _fresh():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    return sessionmaker(bind=e, expire_on_commit=False)


def _card(s, name, oracle, type_line="Artifact"):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line=type_line,
        oracle_text=oracle,
        color_identity="",
    )
    s.add(c)
    s.flush()
    return c


def _row(s, user_id, card, tags=None):
    r = InventoryRow(
        user_id=user_id, card_id=card.id, quantity=1, finish="normal", is_pending=False
    )
    s.add(r)
    s.flush()
    if tags:
        set_row_tags(r, tags)
    return r


def test_plan_adds_ramp_only_to_attributable_untagged_rows(monkeypatch, tmp_path):
    s = _fresh()()
    u = User(username="u1", password_hash="x")
    s.add(u)
    s.flush()

    crypto = _card(
        s, "Cryptolith Rite", 'Creatures you control have "{T}: Add one mana of any color."'
    )
    voyage = _card(
        s,
        "Collective Voyage",
        "Each player searches their library for up to X basic land cards, puts them onto the battlefield tapped.",
        type_line="Sorcery",
    )
    solring = _card(s, "Sol Ring", "{T}: Add {C}{C}.")  # ramp, but NOT attributable to #139
    utopia = _card(
        s,
        "Utopia Vow",
        'Enchant creature\nEnchanted creature has "{T}: Add one mana of any color."',
        type_line="Enchantment — Aura",
    )

    r_crypto = _row(s, u.id, crypto)  # no tags
    r_voyage = _row(s, u.id, voyage)
    r_solring = _row(s, u.id, solring)  # untagged ramp but not attributable -> skip
    _row(s, u.id, utopia)  # tagger doesn't derive ramp -> skip
    r_already = _row(
        s, u.id, crypto, tags=[{"tag": "Ramp", "source": "user", "confidence": "high"}]
    )
    s.commit()

    targets = {t["row_id"] for t in plan(s)}
    assert r_crypto.id in targets  # "you control" grant
    assert r_voyage.id in targets  # searches their library
    assert r_solring.id not in targets  # ramp, but not a #139 case
    assert r_already.id not in targets  # already has Ramp — no-op


def test_apply_adds_ramp_preserving_existing_tags():
    s = _fresh()()
    u = User(username="u1", password_hash="x")
    s.add(u)
    s.flush()
    crypto = _card(
        s, "Cryptolith Rite", 'Creatures you control have "{T}: Add one mana of any color."'
    )
    r = _row(s, u.id, crypto, tags=[{"tag": "Draw", "source": "auto", "confidence": "medium"}])
    s.commit()

    for t in plan(s):
        _add_ramp(s, t["row_id"])
    s.commit()

    tags = get_row_tags(s.get(InventoryRow, r.id))
    assert "Ramp" in tags and "Draw" in tags  # added without clobbering
