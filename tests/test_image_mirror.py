"""#44 — self-hosted card image mirror (img.cartarch.com).

Pins the URL contract (/{scryfall_id}[/back]/{size}.{ext}, ext=png only for
size "png"), the Scryfall onerror fallback URL, and one end-to-end render:
card detail serves the mirror src with the fallback attribute wired.
"""

from app.dependencies import mirror_image_url, scryfall_image_fallback
from app.models import Card


def test_mirror_url_contract():
    sid = "abc-123"
    assert mirror_image_url(sid) == "https://img.cartarch.com/abc-123/normal.jpg"
    assert mirror_image_url(sid, "large") == "https://img.cartarch.com/abc-123/large.jpg"
    assert mirror_image_url(sid, "png") == "https://img.cartarch.com/abc-123/png.png"
    assert (
        mirror_image_url(sid, "normal", "back")
        == "https://img.cartarch.com/abc-123/back/normal.jpg"
    )
    assert mirror_image_url(sid, "png", "back") == "https://img.cartarch.com/abc-123/back/png.png"


def test_scryfall_fallback_url():
    sid = "abc-123"
    assert scryfall_image_fallback(sid, "large") == (
        "https://api.scryfall.com/cards/abc-123?format=image&version=large"
    )
    assert scryfall_image_fallback(sid, "normal", "back") == (
        "https://api.scryfall.com/cards/abc-123?format=image&version=normal&face=back"
    )


def test_card_detail_serves_mirror_src_with_fallback(client, db):
    card = Card(
        scryfall_id="sid-mirror-1",
        name="Sol Ring",
        set_code="tst",
        set_name="Test",
        collector_number="1",
        image_url="https://cards.scryfall.io/normal/front/x.jpg",  # guard signal only
    )
    db.add(card)
    db.commit()

    resp = client.get(f"/cards/{card.id}")
    assert resp.status_code == 200
    # hero art is the "large" mirror variant...
    assert "https://img.cartarch.com/sid-mirror-1/large.jpg" in resp.text
    # ...with the loop-safe Scryfall fallback wired on the same tag
    assert "this.onerror=null" in resp.text
    assert "api.scryfall.com/cards/sid-mirror-1?format=image" in resp.text
    # the stored CDN URL is no longer emitted as the src
    assert 'src="https://cards.scryfall.io' not in resp.text


def test_goldfish_payload_carries_scryfall_id_and_mirror_base(client, db, user):
    """#83 — the goldfish JSON payload must carry scryfall_id per card plus
    the mirror base, so goldfish.js can build mirror-first image URLs
    browser-side (the one surface #44's template pass couldn't cover)."""
    import json
    import re

    from app import deck_service
    from app.models import Card, InventoryRow

    card = Card(
        scryfall_id="sid-goldfish-1",
        name="Sol Ring",
        set_code="tst",
        set_name="Test",
        collector_number="1",
        image_url="https://cards.scryfall.io/normal/front/x.jpg",
    )
    db.add(card)
    db.flush()
    deck = deck_service.create_deck(db, user.id, "Goldfish Deck")
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=card.id,
            quantity=1,
            finish="normal",
            storage_location_id=deck.storage_location_id,
            is_pending=False,
        )
    )
    db.commit()

    resp = client.get(f"/decks/{deck.id}/goldfish")
    assert resp.status_code == 200
    m = re.search(
        r'<script id="gf-deck-data" type="application/json">(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    assert m, "goldfish payload script tag missing"
    payload = json.loads(m.group(1))
    assert payload["image_mirror_base"] == "https://img.cartarch.com"
    assert payload["cards"][0]["scryfall_id"] == "sid-goldfish-1"
    # image_url stays in the payload — it's the no-scryfall_id fallback signal
    assert payload["cards"][0]["image_url"] == "https://cards.scryfall.io/normal/front/x.jpg"
