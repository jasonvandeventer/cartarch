"""Grid thumbs offer the mirror's `small` to 1x screens (2026-08-21).

A ~140px grid thumb always fetched `normal` — 488x680, 152 KB — which is 8.4x
the bytes of `small` (146x204, 18 KB) for a box a third the size. Alex reported
a shared showcase on the trade screen pulling 3,282 images / 40.9 MB and taking
1.77 minutes to settle.

`srcset` + `sizes` hands the choice to the browser: a 1x screen takes `small`,
a 2x screen still takes `normal`, so nothing gets softer where it would show.
Chromium-verified per device-pixel-ratio; this pins the markup that makes the
choice possible at all.
"""

from __future__ import annotations

import re

from app.models import Card, InventoryRow, StorageLocation


def _seed(db, user):
    loc = StorageLocation(user_id=user.id, name="Binder", type="binder", mode="managed")
    db.add(loc)
    db.flush()
    card = Card(
        scryfall_id="0000579f-7b35-4ed3-b44c-db2a538066fe",
        name="Fury Sliver",
        set_code="tsp",
        collector_number="157",
        type_line="Creature — Sliver",
        image_url="https://img.example.invalid/x.jpg",
    )
    db.add(card)
    db.flush()
    db.add(
        InventoryRow(
            card_id=card.id,
            user_id=user.id,
            quantity=1,
            finish="normal",
            is_pending=False,
            storage_location_id=loc.id,
        )
    )
    db.commit()


def test_the_collection_grid_thumb_offers_both_sizes(client, db, user):
    _seed(db, user)
    page = client.get("/collection")
    assert page.status_code == 200

    m = re.search(r'<img class="inventory-thumb".*?>', page.text, re.S)
    assert m, "the collection grid has no inventory-thumb img"
    tag = m.group(0)
    assert "/small.jpg 146w" in tag, tag
    assert "/normal.jpg 488w" in tag, tag
    # `sizes` is what makes a 1x screen eligible for `small` at all — without it
    # the browser assumes 100vw and always takes the larger candidate.
    assert 'sizes="138px"' in tag, tag
    # The plain src stays `normal` for anything that ignores srcset, and the
    # mirror-404 fallback is unchanged.
    assert "/normal.jpg" in tag and "onerror" in tag
