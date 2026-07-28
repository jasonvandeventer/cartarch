"""#118 — shared outside-click/Escape dismissal: the mechanism and its opt-ins.

Client-side behavior can't run under pytest; the smallest thing that fails if
the mechanism breaks is: base.html ships the handler, and every inventoried
popover/modal carries its opt-in marker.
"""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"

# (template, marker, expected count)
POPOVER_OPTINS = [
    ("collection.html", "js-dismiss", 1),
    ("_collection_row.html", "js-dismiss", 1),
    ("_macros.html", "js-dismiss", 4),
    ("_inline_create_destination.html", "js-dismiss", 2),
    ("playgroup_detail.html", "js-dismiss", 3),
    ("tokens.html", "js-dismiss", 2),
    ("locations.html", "js-dismiss", 2),
    ("_review_tags_panel.html", "js-dismiss", 1),
]

# game_new.html carried one — the first-player modal — until v4.12.26 moved that
# decision to the game page (it asked at creation, when seats 2..N are still #165
# placeholders). It has no modal now, so it is off this inventory rather than at 0.
MODAL_OPTINS = [
    ("game_detail.html", "data-dismiss", 2),  # end-game + damage-matrix
    ("game_summary.html", "data-dismiss", 1),
    ("games.html", "data-dismiss", 1),
    ("_players_modal.html", "data-dismiss", 1),
    ("_quick_add_modal.html", "data-dismiss", 1),
    ("deck_detail.html", "data-dismiss", 2),  # bulk-move + bulk-delete
]


def test_base_ships_the_shared_dismissal_handler():
    base = (TEMPLATES / "base.html").read_text()
    assert "details.js-dismiss[open]" in base
    assert "data-dismiss" in base
    assert "__dirty" in base


def test_every_inventoried_surface_opts_in():
    for name, marker, expected in POPOVER_OPTINS + MODAL_OPTINS:
        text = (TEMPLATES / name).read_text()
        # count attribute/class occurrences (word-bounded, not e.g. data-dismiss-foo)
        found = len(re.findall(rf'{marker}(?=["\s>])', text))
        assert found >= expected, f"{name}: {found} < {expected} '{marker}' opt-ins"


def test_games_bespoke_escape_handler_removed():
    # the old unguarded handlers would close a dirty notes modal
    games = (TEMPLATES / "games.html").read_text()
    assert "e.target === notesModal" not in games
    assert "e.key === 'Escape'" not in games
