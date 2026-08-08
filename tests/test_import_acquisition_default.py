"""A PASTED list is an acquisition, so its surplus copies must not be skipped.

Reported 2026-08-08: pasting four cards to auto-sort, where copies are already
owned, silently imported nothing — the extras never reached Bulk.

The cause is not the drawer-vs-bulk routing, which works. It is that
`find_inventory_matches_for_collection_import` is SYNC-mode reconciliation: when
`total_user_owned >= quantity_needed` it recommends `skip_already_owned` with
`new_qty = 0`, so no `InventoryRow` is created and `route_intake_to_bulk` — which
runs AFTER rows exist — never sees the copies. The surplus that the whole
drawer-vs-bulk feature exists to handle was dropped before routing.

The default already differed by path (`manual_mode`, now `acquisition_default`):
a manual single-card add defaults to `import_new` because it is an acquisition.
A pasted list is the same kind of act and now defaults the same way. A CSV keeps
sync semantics — it is usually a collection export being re-imported, which is
what `docs/collection_import_sync.md` designed the skip for.
"""

import pathlib
import re

import app.legacy_tables  # noqa

_PREVIEW = (
    pathlib.Path(__file__).resolve().parents[1] / "app" / "templates" / "import_preview.html"
).read_text()
_RECON = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app"
    / "templates"
    / "_import_reconciliation.html"
).read_text()


def test_the_paste_preview_declares_its_source(client, db, user):
    """The flag has to reach the page before it can reach the reconcile panel."""
    resp = client.post("/import/list/preview", data={"card_list": "1 Sol Ring"})
    assert resp.status_code == 200
    assert 'name="import_source" value="list"' in resp.text


def test_the_source_input_rides_inside_the_commit_form():
    """`hx-include="closest form"` is how the panel sees it, and the commit POST
    carries it for free. Outside the form it would silently do nothing."""
    i = _PREVIEW.index('name="import_source"')
    form_open = _PREVIEW.rfind("<form", 0, i)
    form_close = _PREVIEW.find("</form>", i)
    assert form_open != -1 and form_close > i, "the hidden input escaped the commit form"


def _own_a_card(db, user, *, sid="sf-acq-1", qty=1):
    """Seed an OWNED copy so reconciliation would otherwise recommend
    `skip_already_owned`. Without this the card is unowned, `import_new` is the
    recommendation anyway, and the assertions below pass while testing nothing —
    which is exactly what the first version of this test did."""
    from app.models import Card, InventoryRow, StorageLocation

    card = Card(
        name="Sol Ring",
        scryfall_id=sid,
        set_code="c21",
        set_name="Commander 2021",
        collector_number="263",
        rarity="uncommon",
    )
    db.add(card)
    loc = StorageLocation(user_id=user.id, name="Drawer 1", type="drawer")
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
    return card


def _reconcile(client, **extra):
    data = {
        "target_location_id": "0",
        "line_number": "1",
        "name": "Sol Ring",
        "scryfall_id": "sf-acq-1",
        "set_code": "c21",
        "collector_number": "263",
        "finish": "normal",
        "quantity": "1",
        "location": "",
        "language": "en",
    }
    data.update(extra)
    return client.post("/import/reconcile-preview", data=data)


def _selected_action(html: str) -> str | None:
    """Which option the panel PRESELECTS — the thing the user actually gets."""
    m = re.search(r'<option value="([a-z_]+)"[^>]*\bselected\b', html)
    return m.group(1) if m else None


def test_a_pasted_list_defaults_to_importing_extra_copies(client, db, user):
    """THE bug. The card IS already owned, so sync-mode would skip it; a pasted
    list must default to importing the extra copy instead, so it becomes an
    InventoryRow and `route_intake_to_bulk` can send the surplus to Bulk."""
    _own_a_card(db, user)
    resp = _reconcile(client, import_source="list")
    assert resp.status_code == 200
    assert _selected_action(resp.text) == "import_new"


def test_a_csv_of_the_same_card_still_defaults_to_skip(client, db, user):
    """The control, and the reason this is a per-path default rather than a
    global flip: re-importing a collection export should still skip what you
    already own."""
    _own_a_card(db, user)
    resp = _reconcile(client)  # no import_source → csv
    assert resp.status_code == 200
    assert _selected_action(resp.text) == "skip_already_owned"


def test_the_banner_tells_the_truth_in_both_modes():
    """The old copy said 'They'll be skipped by default — usually what you want'
    unconditionally. Under acquisition semantics that is simply false, and a
    banner that lies about the default is worse than none."""
    assert "acquisition_default" in _RECON
    assert "imported as EXTRA copies by default" in _RECON
    assert "skipped by default" in _RECON  # the sync branch still says so


def test_the_template_keys_on_acquisition_default_not_manual_mode():
    """`manual_mode` meant 'single manual card'. Widening the concept without
    renaming it would have left the paste path reading as a manual add."""
    assert "manual_mode" not in _RECON
    assert 'default_action = "import_new" if acquisition_default' in _RECON
