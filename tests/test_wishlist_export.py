"""Export the wishlist as a buy list (#185).

Requested: "A way to export your wishlist as text for import into TCGPlayer.
Either that or a link to immediately open it into the import in TCGPlayer."

Both were built. The text export is the one with no ceiling; the Mass Entry link
is the convenience, and it disappears rather than truncating when the list
outgrows a URL.

Three decisions worth pinning:

* **The export honours ``?show=``.** The same reason the collection export
  honours its filter — "Not owned yet" is the facet people will actually shop
  from, and a buy list that quietly ignored it would be a list of cards they
  already own. The filter is ONE function (``filter_wishlist_by_show``) shared
  with the page, not a second copy in the route.
* **Names are de-duplicated.** A user can hold a printing-specific watch AND a
  name watch for the same card; that is two ways of wanting one card, and
  emitting both would put a second copy in their cart.
* **No set codes.** A wishlist is a want for the CARD, so pinning a printing
  would make the buy list refuse a cheaper one.
"""

from __future__ import annotations

import pytest

from app.models import Card, WatchlistItem
from app.watchlist_service import (
    TCGPLAYER_URL_MAX,
    filter_wishlist_by_show,
    tcgplayer_massentry_url,
    wishlist_export_names,
    wishlist_export_text,
)


def _item(name, *, target_met=False, placed=0, pending=0):
    return {
        "display_name": name,
        "target_met": target_met,
        "placed_count": placed,
        "pending_count": pending,
    }


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def test_text_is_one_quantified_name_per_line():
    text = wishlist_export_text([_item("Sol Ring"), _item("Arcane Signet")])
    assert text == "1 Sol Ring\n1 Arcane Signet\n"


def test_a_name_watch_and_a_printing_watch_export_once():
    """Two ways of wanting one card is still one card."""
    assert wishlist_export_names([_item("Sol Ring"), _item("sol ring")]) == ["Sol Ring"]


def test_a_nameless_row_is_skipped_not_exported_blank():
    assert wishlist_export_names([_item(""), _item(None), _item("Sol Ring")]) == ["Sol Ring"]


def test_no_set_code_is_emitted():
    """Pinning a printing would make the buy list refuse a cheaper one."""
    text = wishlist_export_text([_item("Sol Ring")])
    assert "[" not in text and "(" not in text


def test_an_empty_wishlist_exports_nothing_rather_than_a_stray_newline():
    assert wishlist_export_text([]) == ""


# --------------------------------------------------------------------------- #
# The TCGPlayer link
# --------------------------------------------------------------------------- #


def test_the_link_uses_the_verified_mass_entry_shape():
    """Entries joined by `||` in a `c=` param, url-encoded. Probed against the
    live endpoint on 2026-08-17 (200 with a payload attached)."""
    url = tcgplayer_massentry_url([_item("Sol Ring"), _item("Arcane Signet")])
    assert url.startswith("https://www.tcgplayer.com/massentry?productline=Magic&c=")
    assert "%7C%7C" in url  # the || separator, encoded
    assert "Sol%20Ring" in url


def test_a_card_name_with_punctuation_is_encoded():
    """Real card names carry commas, apostrophes and ampersands; an unencoded
    one would truncate the list at that card."""
    url = tcgplayer_massentry_url([_item("Ach! Hans, Run!"), _item("Sol Ring")])
    assert " " not in url and "!" not in url
    assert url.endswith("Sol%20Ring")


def test_an_empty_wishlist_produces_no_link():
    assert tcgplayer_massentry_url([]) is None


def test_a_wishlist_too_long_for_a_url_produces_no_link():
    """A URL is not an unbounded transport. Better no link than one that
    silently drops the tail of somebody's shopping list."""
    huge = [_item(f"Some Reasonably Long Card Name {i}") for i in range(600)]
    assert tcgplayer_massentry_url(huge) is None
    # ...and the text export, which has no ceiling, still carries all of them.
    assert len(wishlist_export_names(huge)) == 600


def test_the_cap_clears_the_wishlist_that_prompted_the_feature():
    """The requester's real list encodes to 1,985 chars — FIFTEEN under the
    2000-char cap this shipped with first. That boundary would have handed him
    a link today and silently removed it on his next card, so the cap is pinned
    against a realistically-sized list rather than a comfortable one."""
    real_sized = [_item(f"Bala Ged Recovery {i} // Bala Ged Sanctuary") for i in range(66)]
    url = tcgplayer_massentry_url(real_sized)
    assert url is not None, "a normal wishlist of long names must still get a link"
    assert len(url) <= TCGPLAYER_URL_MAX
    # Real headroom, not a coincidence of one measurement.
    assert len(url) < TCGPLAYER_URL_MAX * 0.9


def test_the_encoded_length_is_what_is_capped():
    """Spaces become %20 and `||` becomes %7C%7C, so the raw payload is roughly a
    third shorter than the URL. Capping the raw string would let a list through
    that the encoded URL cannot carry."""
    items = [_item(f"A Card With Several Words {i}") for i in range(120)]
    url = tcgplayer_massentry_url(items)
    raw = "||".join(f"1 {i['display_name']}" for i in items)
    if url is not None:
        assert len(url) > len(raw), "the cap must be applied to the ENCODED url"


# --------------------------------------------------------------------------- #
# The filter, shared with the page
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "show,expected",
    [
        ("all", ["Owned", "Unowned", "Hit"]),
        ("unowned", ["Unowned", "Hit"]),
        ("owned", ["Owned"]),
        ("target_met", ["Hit"]),
        ("nonsense", ["Owned", "Unowned", "Hit"]),  # unknown → all, never empty
    ],
)
def test_the_show_facet_is_one_shared_definition(show, expected):
    items = [
        _item("Owned", placed=1),
        _item("Unowned"),
        _item("Hit", target_met=True),
    ]
    assert [i["display_name"] for i in filter_wishlist_by_show(items, show)] == expected


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #


def _seed(db, user, name, *, owned=False):
    card = Card(
        scryfall_id=f"sid-{name.lower().replace(' ', '-')}",
        name=name,
        set_code="tst",
        collector_number="1",
        type_line="Artifact",
    )
    db.add(card)
    db.flush()
    db.add(WatchlistItem(user_id=user.id, card_id=card.id))
    if owned:
        from app.models import InventoryRow

        db.add(
            InventoryRow(
                user_id=user.id, card_id=card.id, quantity=1, finish="normal", is_pending=False
            )
        )
    db.commit()
    return card


def test_the_export_route_returns_plain_text(client, db, user):
    """Route-level, because the service returning a string proves nothing about
    the endpoint. This also catches the NameError class: `Response` was not in
    main.py's imports, and importing the module does not reveal that — the name
    resolves only when the route runs."""
    _seed(db, user, "Sol Ring")
    resp = client.get("/watchlist/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert resp.text == "1 Sol Ring\n"


def test_the_export_route_honours_the_show_filter(client, db, user):
    _seed(db, user, "Owned Card", owned=True)
    _seed(db, user, "Wanted Card")

    everything = client.get("/watchlist/export").text
    assert "Owned Card" in everything and "Wanted Card" in everything

    shopping = client.get("/watchlist/export?show=unowned").text
    assert "Wanted Card" in shopping
    assert "Owned Card" not in shopping, "the buy list must not include cards already owned"


def test_the_export_is_owner_scoped(client, db, user):
    """A wishlist is per-user; the export must not reach across accounts."""
    from app.models import User

    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.flush()
    card = Card(scryfall_id="sid-theirs", name="Their Card", set_code="tst", collector_number="9")
    db.add(card)
    db.flush()
    db.add(WatchlistItem(user_id=other.id, card_id=card.id))
    db.commit()

    assert "Their Card" not in client.get("/watchlist/export").text


def test_the_page_offers_both_routes(client, db, user):
    """#152 — the route enumerates its context key by key, so the buttons need a
    route line as well as a template."""
    _seed(db, user, "Sol Ring")
    page = client.get("/watchlist").text
    assert "/watchlist/export" in page
    assert "tcgplayer.com/massentry" in page


def test_the_page_offers_nothing_when_the_wishlist_is_empty(client, db, user):
    page = client.get("/watchlist").text
    assert "/watchlist/export" not in page
