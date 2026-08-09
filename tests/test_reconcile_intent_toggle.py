"""ADD vs SYNC is stated on the panel, not inferred from which button you clicked.

v4.13.23 made a pasted list default to acquisition and a CSV to sync. That
default is a good guess, not a fact — pasting a decklist to CHECK coverage is a
re-sync, and uploading a CSV of a box you just bought is an acquisition. This
toggle makes the assumption visible and reversible in one click.

**It changes no semantics.** It only sets the per-row action the panel already
supports, so it cannot double a quantity or delete a row. True restore semantics
(the pending-merge key, and whether "restore" deletes rows absent from the file)
remain #150 item 1, deliberately out of scope.
"""

import pathlib
import re

import app.legacy_tables  # noqa
from app.models import Card, InventoryRow, StorageLocation

_RECON = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app"
    / "templates"
    / "_import_reconciliation.html"
).read_text()


def _own(db, user, sid="sf-intent-1", qty=2):
    card = Card(
        name="Owned Card",
        scryfall_id=sid,
        set_code="c21",
        set_name="C21",
        collector_number="1",
        rarity="rare",
    )
    db.add(card)
    loc = StorageLocation(user_id=user.id, name="Box", type="box")
    db.add(loc)
    db.flush()
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=card.id,
            quantity=qty,
            finish="normal",
            is_pending=False,
            storage_location_id=loc.id,
        )
    )
    db.commit()


def _reconcile(client, **extra):
    data = {
        "target_location_id": "0",
        "line_number": "1",
        "name": "Owned Card",
        "scryfall_id": "sf-intent-1",
        "set_code": "c21",
        "collector_number": "1",
        "finish": "normal",
        "quantity": "1",
        "location": "",
        "language": "en",
    }
    data.update(extra)
    return client.post("/import/reconcile-preview", data=data)


def _intent_radios(html: str) -> list[str]:
    """The radio INPUTS, not any mention of the name — the wiring JS always
    renders and contains the same string in a querySelector, which is what the
    first version of the absence test matched."""
    return re.findall(r'<input[^>]*name="reconcile_intent"[^>]*>', html, re.S)


def _checked_intent(html: str) -> str | None:
    """Which radio the panel ships checked — the assumption made visible."""
    for tag in _intent_radios(html):
        if "checked" in tag:
            return re.search(r'value="(\w+)"', tag).group(1)
    return None


def test_the_toggle_appears_when_something_is_already_owned(client, db, user):
    _own(db, user)
    html = _reconcile(client).text
    assert len(_intent_radios(html)) == 2


def test_it_is_absent_when_nothing_is_owned(client, db, user):
    """With nothing owned both modes import identically; the control would be
    noise, and a control that never changes anything teaches people to ignore
    the ones that do."""
    html = _reconcile(client).text  # no owned row seeded
    assert _intent_radios(html) == []


def test_it_reflects_the_paste_default(client, db, user):
    _own(db, user)
    assert _checked_intent(_reconcile(client, import_source="list").text) == "add"


def test_it_reflects_the_csv_default(client, db, user):
    _own(db, user)
    assert _checked_intent(_reconcile(client).text) == "sync"


def test_the_toggle_reuses_the_existing_quantity_handler():
    """It dispatches the same `change` event the per-row handler listens for, so
    the quantity arithmetic has ONE implementation. Duplicating it is how the
    two drift and a partial import silently imports the wrong count."""
    assert "dispatchEvent(new Event('change'))" in _RECON
    assert _RECON.count("hiddenNew.value = Math.max(0, needed - owned)") == 1, (
        "the delta arithmetic exists more than once — the toggle should reuse it"
    )


def test_it_skips_rows_that_do_not_offer_the_option():
    """A row with nothing owned has no skip option; forcing the value would
    submit something the server has to second-guess."""
    assert "if (!sel.querySelector('option[value=\"' + wanted + '\"]')) return;" in _RECON
