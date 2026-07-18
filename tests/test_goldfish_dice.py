"""#142 — goldfish dice roller + coin flip. Client-side; the pytest-testable
surface is that the goldfish page renders the control (all dice + coin, wired to
#118's shared js-dismiss). Roll math + dismissal are browser behavior, verified
manually / via Playwright.
"""

from __future__ import annotations

import itertools

from app import deck_service
from app.models import Card, InventoryRow

_seq = itertools.count(1)


def _deck(db, user):
    deck = deck_service.create_deck(db, user.id, "Goldfish Test", format_name="commander")
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name="Llanowar Elves",
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line="Creature — Elf Druid",
        mana_cost="{G}",
        cmc=1,
        colors="G",
        color_identity="G",
        oracle_text="{T}: Add {G}.",
        rarity="common",
    )
    db.add(c)
    db.flush()
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=c.id,
            quantity=1,
            finish="normal",
            storage_location_id=deck.storage_location_id,
            is_pending=False,
        )
    )
    db.commit()
    return deck


def test_goldfish_page_renders_dice_control(client, db, user):
    deck = _deck(db, user)
    r = client.get(f"/decks/{deck.id}/goldfish")
    assert r.status_code == 200
    body = r.text

    # the control exists and opts into #118's shared dismissal
    assert "gf-dice" in body and "js-dismiss gf-dice" in body
    # all six dice + a coin, in the d2 slot
    for sides in (4, 6, 8, 10, 12, 20):
        assert f'data-die="{sides}"' in body
    assert 'data-die="coin"' in body
    # the last-result box lives in the summary (visible without reopening)
    assert 'id="gf-dice-result"' in body
