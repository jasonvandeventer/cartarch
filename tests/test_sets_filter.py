"""The /sets page filters instantly by set name OR code.

Requested 2026-08-14. Wiring only — `list-filter.js` already does this for
seven other bounded lists, so the page emits the contract attributes and loads
the engine rather than growing a query param or a second script.

The #168 rule is the one that bites here: emitting `data-list-filter` WITHOUT
loading the engine is a silent dead control. `test_commander_picker_ui.py`
guards that class repo-wide; these pin this instance's specifics.
"""

from app.models import Card, InventoryRow


def _own(db, user, set_code, set_name, n=1):
    for i in range(n):
        c = Card(
            scryfall_id=f"{set_code}-{i}",
            name=f"{set_name} Card {i}",
            set_code=set_code,
            set_name=set_name,
            collector_number=str(i),
            type_line="Creature",
        )
        db.add(c)
        db.flush()
        db.add(
            InventoryRow(
                user_id=user.id, card_id=c.id, finish="normal", quantity=1, is_pending=False
            )
        )
    db.commit()


def test_the_filter_box_renders_and_loads_its_engine(client, db, user):
    _own(db, user, "neo", "Kamigawa: Neon Dynasty")
    html = client.get("/sets").text
    assert "data-list-filter" in html
    # #168: the attributes are inert without the script.
    assert "list-filter.js" in html


def test_each_card_carries_both_code_and_name_as_filter_text(client, db, user):
    _own(db, user, "neo", "Kamigawa: Neon Dynasty")
    html = client.get("/sets").text
    # Typing either half must match, so both are in data-filter-text.
    assert 'data-filter-text="neo Kamigawa: Neon Dynasty"' in html


def test_the_target_selector_matches_the_cards_it_must_hide(client, db, user):
    _own(db, user, "neo", "Kamigawa: Neon Dynasty")
    _own(db, user, "dmu", "Dominaria United")
    html = client.get("/sets").text
    assert 'data-list-filter-target=".sets-card-editorial"' in html
    # A target that matches nothing is the other way to ship a dead control.
    assert html.count('class="editorial-card sets-card-editorial"') == 2


def test_the_empty_state_is_wired_and_starts_hidden(client, db, user):
    _own(db, user, "neo", "Kamigawa: Neon Dynasty")
    html = client.get("/sets").text
    assert 'data-list-filter-empty="#sets-filter-empty"' in html
    assert 'id="sets-filter-empty"' in html
    assert "hidden" in html.split('id="sets-filter-empty"')[1][:120]


def test_no_filter_box_when_there_are_no_sets(client, db, user):
    """A control that can never do anything teaches people to ignore controls."""
    html = client.get("/sets").text
    assert "data-list-filter" not in html
