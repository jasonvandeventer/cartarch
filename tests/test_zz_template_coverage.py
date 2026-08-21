"""Every template must be RENDERED by some test, or be allowlisted with a reason.

**This guards a class, not an instance.** A template no test renders can drift
indefinitely, because prod's Jinja is permissive (#157): a missing context key
renders empty instead of raising, so nothing surfaces it. `import_preview.html`
read `row.location_type` — a key `parse_text_list` never emits — for months.
Nothing broke in prod; the gap only appeared when v4.13.23 finally loaded that
page in a test and the STRICT env raised on the first render.

Measured when this was written: **21 of 95 templates had never been rendered by
the suite**, including `_switch_printing_modal.html`, whose scroll bug had been
fixed the day before with a test that read the file as TEXT and never rendered
it, and `_collection_row.html`, which carries the `'%.2f'|format(...)` call
whose TEXT-column variant took `/pending` down in v4.13.12.

**Named `test_zz_` so it collects LAST.** The tracker is filled as other tests
render, so a guard that runs mid-alphabet sees a partial picture — the first
version of this file sat at `test_template_coverage.py` and duly reported
`watchlist.html` and `trade_new.html` as unrendered, because `test_trade_*` and
`test_watchlist_*` had not run yet. Same precedent as
`test_fk_parent_delete.py::test_zzz_print_red_matrix`.

**It SKIPS on a partial run.** `pytest tests/test_one_thing.py` renders almost
nothing, and a guard that fails there would train everyone to ignore it.

**The allowlist is meant to shrink.** Removing an entry is the unit of progress;
adding one requires saying why. The point is not a coverage percentage — it is
that every gap is a deliberate choice somebody wrote down.
"""

import pathlib

import pytest

from tests.conftest import RENDERED_TEMPLATES

_TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "app" / "templates"

# Templates no test renders yet. Each entry is a debt with a stated reason.
# REMOVE an entry when a test starts rendering it — the guard will tell you.
_ALLOWED_UNRENDERED = {
    # Remaining plain GET pages — same treatment as the batch struck off in
    # v4.13.25 (chronicle, privacy, terms, decklist, playgroups, trades, tokens,
    # token_new), which route smoke now renders.
    "drawers.html": "index is gated on user_has_drawers; smoke pins the 403 instead",
    "playgroup_join.html": "join-by-code page",
    "token_detail.html": "one token",
    "token_bulk_add.html": "token bulk add form",
    "set_detail.html": "per-set page",
    # Confirmation / preview screens reached mid-flow.
    "bulk_delete_confirm.html": "reached only from a POST preview",
    "delete_preview.html": "reached only from a POST preview",
    "manual_preview.html": "HTMX fragment of the manual add flow",
    "manual_search_results.html": "HTMX fragment of the manual add flow",
    "_review_tags_panel_content.html": "HTMX fragment of the tag review panel",
    # Partials worth real render tests — highest value in this list, because
    # both carry price formatting of the kind that has already caused a 500.
    "_collection_row.html": "partial; needs a parent context (carries effective_price formatting)",
    "_switch_printing_modal.html": "partial; needs a parent context (carries price formatting)",
}


def _on_disk() -> set[str]:
    return {p.relative_to(_TEMPLATES).as_posix() for p in _TEMPLATES.rglob("*.html")}


# Below this many collected tests we assume a targeted run, not the gate.
_FULL_RUN_MIN_TESTS = 1000


@pytest.fixture(autouse=True)
def _only_on_a_full_run(request):
    collected = getattr(request.session, "testscollected", 0)
    if collected < _FULL_RUN_MIN_TESTS:
        pytest.skip(f"partial run ({collected} tests) — template coverage needs the full suite")


def test_the_tracker_actually_recorded_something():
    """If the conftest hook breaks, every assertion below passes vacuously —
    'nothing was rendered' would read as 'nothing is unrendered'."""
    assert len(RENDERED_TEMPLATES) > 40, (
        f"only {len(RENDERED_TEMPLATES)} templates recorded — the loader hook is not firing"
    )


def test_no_new_template_escapes_test_coverage():
    unrendered = _on_disk() - RENDERED_TEMPLATES - set(_ALLOWED_UNRENDERED)
    assert not unrendered, (
        "template(s) no test renders and not allowlisted:\n  "
        + "\n  ".join(sorted(unrendered))
        + "\n\nRender it in a test (a route smoke case is usually enough), or add it "
        "to _ALLOWED_UNRENDERED with the reason."
    )


def test_the_allowlist_has_no_stale_entries():
    """An allowlisted template that IS now rendered must be removed, or the list
    stops describing reality and quietly re-permits the next gap."""
    on_disk = _on_disk()
    stale = sorted(t for t in _ALLOWED_UNRENDERED if t in RENDERED_TEMPLATES)
    assert not stale, (
        "these are allowlisted but ARE rendered now — delete them from "
        "_ALLOWED_UNRENDERED:\n  " + "\n  ".join(stale)
    )
    gone = sorted(t for t in _ALLOWED_UNRENDERED if t not in on_disk)
    assert not gone, "allowlisted template(s) no longer exist:\n  " + "\n  ".join(gone)


@pytest.mark.parametrize("name", sorted(_ALLOWED_UNRENDERED))
def test_allowlisted_templates_still_exist(name):
    assert (_TEMPLATES / name).exists()
