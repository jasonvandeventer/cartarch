"""No page may nest one <form> inside another.

Reported 2026-08-23: "the Send Counter Proposal button does not function". It
was not the button. v4.16.3 put the Grid/List toggle — itself a <form> — INSIDE
the counter form, and a nested form is invalid HTML: the parser drops the inner
start tag and the inner `</form>` closes the OUTER form. Everything after it,
including the submit button and the two hidden JSON fields' siblings, fell out of
the form, so the button belonged to no form and did nothing when pressed.
Measured in Chromium before the fix: `submit.form === null`, and the counter form
held 6 controls instead of 114.

This checks the RENDERED page, not the templates: the toggle arrives through an
`{% include %}`, so a source scan of any one file sees nothing wrong. The
detector is the same rule a browser applies — a `<form>` start tag while another
form is still open.
"""

from __future__ import annotations

import itertools
from html.parser import HTMLParser

import pytest

from app import trade_service as ts
from app.models import (
    Card,
    InventoryRow,
    Playgroup,
    PlaygroupMember,
    Share,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    User,
)

_seq = itertools.count(1)


class _FormNesting(HTMLParser):
    """Reports every <form> opened while another is still open."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.violations: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "form":
            return
        if self.depth:
            attr = dict(attrs)
            self.violations.append(attr.get("action") or attr.get("id") or "<form>")
        self.depth += 1

    def handle_endtag(self, tag):
        if tag == "form" and self.depth:
            self.depth -= 1


def nested_forms(html: str) -> list[str]:
    p = _FormNesting()
    p.feed(html)
    return p.violations


def test_the_detector_matches_the_browser_rule():
    """A guard that found nothing would pass on the page that shipped broken."""
    assert nested_forms("<form action='/a'><form action='/b'></form></form>") == ["/b"]
    assert nested_forms("<form action='/a'></form><form action='/b'></form>") == []
    assert nested_forms("<div><form action='/a'><input></form></div>") == []


@pytest.fixture
def trade(db, user):
    other = User(username=f"o{next(_seq)}@x.com", password_hash="x", display_name="Alex")
    db.add(other)
    db.flush()
    pg = Playgroup(name="Pod", created_by=other.id)
    db.add(pg)
    db.flush()
    db.add_all(
        [
            PlaygroupMember(playgroup_id=pg.id, user_id=other.id, role="owner"),
            PlaygroupMember(playgroup_id=pg.id, user_id=user.id, role="member"),
        ]
    )
    theirs = StorageLocation(user_id=other.id, name="T", type="binder", mode="managed")
    mine = StorageLocation(user_id=user.id, name="M", type="binder", mode="managed")
    sct = Showcase(user_id=other.id, name="T")
    scm = Showcase(user_id=user.id, name="M")
    db.add_all([theirs, mine, sct, scm])
    db.flush()

    def _row(owner_id, name, loc):
        c = Card(
            scryfall_id=f"sid-{next(_seq)}",
            name=name,
            set_code="tst",
            collector_number="1",
            type_line="Creature",
            image_url="https://img.example.invalid/x.jpg",
            price_usd="2.00",
        )
        db.add(c)
        db.flush()
        r = InventoryRow(
            user_id=owner_id,
            card_id=c.id,
            quantity=1,
            finish="normal",
            is_pending=False,
            storage_location_id=loc.id,
        )
        db.add(r)
        db.flush()
        return r

    their_row = _row(other.id, "Theirs", theirs)
    my_row = _row(user.id, "Mine", mine)
    db.add(ShowcaseItem(showcase_id=sct.id, inventory_row_id=their_row.id, quantity_offered=1))
    si = ShowcaseItem(showcase_id=scm.id, inventory_row_id=my_row.id, quantity_offered=1)
    db.add(si)
    db.flush()
    db.add_all(
        [
            Share(user_id=other.id, showcase_id=sct.id, playgroup_id=pg.id),
            Share(user_id=user.id, showcase_id=scm.id, playgroup_id=pg.id),
        ]
    )
    db.commit()
    t = ts.create_trade(
        db,
        proposer_user_id=other.id,
        recipient_user_id=user.id,
        playgroup_id=pg.id,
        offered=[{"inventory_row_id": their_row.id, "quantity": 1}],
        requested=[{"showcase_item_id": si.id, "quantity": 1}],
    )
    return {"trade": t, "other": other, "pg": pg, "share": db.query(Share).first()}


def test_no_page_with_a_submit_button_nests_a_form(client, db, user, trade):
    """The pages that carry both a big form and a toggle — which is exactly the
    combination that produced the bug."""
    urls = [
        f"/trades/{trade['trade'].id}/counter",
        f"/trades/new?recipient_user_id={trade['other'].id}&playgroup_id={trade['pg'].id}",
        f"/trades/{trade['trade'].id}",
        f"/shares/{trade['share'].id}",
        "/collection",
        "/decks",
    ]
    offenders = {}
    for url in urls:
        resp = client.get(url)
        assert resp.status_code == 200, (url, resp.status_code)
        found = nested_forms(resp.text)
        if found:
            offenders[url] = found
    assert not offenders, f"nested forms (the outer one closes early): {offenders}"


def test_the_counter_form_still_contains_its_submit_and_json_fields(client, db, user, trade):
    """The consequence, stated directly: if the form closes early these fall
    outside it and the button belongs to nothing."""
    page = client.get(f"/trades/{trade['trade'].id}/counter").text
    # Slice from the counter form to the NEXT </form> AFTER it, not to the first
    # one on the page — the toggle's form now precedes it, and an index() from
    # the start of the document would return an empty slice that every
    # assertion below passes against. (#173 recorded this exact trap.)
    start = page.index('id="counter-form"')
    form = page[start : page.index("</form>", start)]
    assert len(form) > 500, "the slice is empty or truncated — check the anchors"
    assert 'id="offered-json"' in form and 'id="requested-json"' in form
    assert "Send counter-proposal" in form


def test_every_picker_search_box_sends_its_value(client, db, user, trade):
    """The other half of the same report: search did nothing on the counter.

    HTMX sends a control's value under its NAME, so an input with `hx-get` and
    no `name` sends nothing and the pane comes back unfiltered — the box looks
    ignored rather than broken. trade_new.html had `name="q"` and
    trade_counter.html did not, which is exactly why search worked on one screen
    and not the other. Both now render the same toolbar; this asserts the
    property on both pages rather than trusting that.
    """
    import re

    urls = {
        "counter": f"/trades/{trade['trade'].id}/counter",
        "construction": (
            f"/trades/new?recipient_user_id={trade['other'].id}&playgroup_id={trade['pg'].id}"
        ),
    }
    for name, url in urls.items():
        html = client.get(url).text
        boxes = re.findall(r"<input[^>]*trade-pick-search[^>]*>", html)
        assert len(boxes) == 2, f"{name}: expected one search box per side, found {len(boxes)}"
        for box in boxes:
            assert 'name="q"' in box, f"{name}: a search box that sends no value\n{box}"
            assert "hx-get" in box, f"{name}: a search box wired to nothing\n{box}"
