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
