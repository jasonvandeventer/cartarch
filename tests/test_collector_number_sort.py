"""Collector-number ordering is numeric and happens before pagination."""

import re
from types import SimpleNamespace

import pytest

from app import sort_spec
from app.dependencies import templates
from app.inventory_service import list_inventory_rows
from app.models import Card, InventoryRow


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_both_shared_sorters_use_numeric_collector_numbers(direction):
    numbers = ["10", "2", "12a", "12", "001", "12b"]
    rows = [
        SimpleNamespace(
            id=i,
            card=SimpleNamespace(name=f"Card {i}", collector_number=n),
        )
        for i, n in enumerate(numbers)
    ]
    expected = ["001", "2", "10", "12", "12a", "12b"]
    if direction == "desc":
        expected.reverse()
    result = sort_spec.sort_inventory_rows(rows[:], "collector", direction)
    assert [r.card.collector_number for r in result] == expected
    items = [{"id": r.id, "card": r.card} for r in rows]
    result = sort_spec.sort_showcase_items(items, "collector", direction)
    assert [it["card"].collector_number for it in result] == expected


@pytest.mark.parametrize(
    "options",
    [
        sort_spec.COLLECTION_SORT_OPTIONS,
        sort_spec.DECK_SORT_OPTIONS,
        sort_spec.LOCATION_SORT_OPTIONS,
        sort_spec.SHOWCASE_SORT_OPTIONS,
        sort_spec.PICKER_SORT_OPTIONS,
    ],
)
def test_each_card_sort_control_offers_collector_number(options):
    html = templates.env.get_template("_sort_controls.html").render(
        sort_options=options, sort="collector", direction="asc"
    )
    assert '<option value="collector" selected>Collector Number</option>' in html


def test_collection_page_sorts_before_paging(client, db, user):
    rows = []
    # Name and insertion order disagree with numeric collector order.
    for number in range(52, 0, -1):
        card = Card(
            scryfall_id=f"collector-sort-{number}",
            name=f"Collector Test {53 - number:02}",
            set_code="tst",
            collector_number=str(number),
        )
        db.add(card)
        db.flush()
        row = InventoryRow(user_id=user.id, card_id=card.id, quantity=1, is_pending=True)
        db.add(row)
        db.flush()
        rows.append(row)
    db.commit()
    expected = [r.id for r in reversed(rows)]
    for direction, ordered in (("asc", expected), ("desc", expected[::-1])):
        for page in (1, 2):
            response = client.get(
                f"/collection?sort=collector&direction={direction}&view=rows&page={page}"
            )
            assert response.status_code == 200
            assert '<option value="collector" selected>Collector Number</option>' in response.text
            rendered = [
                int(i)
                for i in re.findall(
                    r'<article class="collection-row" data-row-id="(\d+)"', response.text
                )
            ]
            assert rendered == ordered[(page - 1) * 50 : page * 50]
    result, total = list_inventory_rows(db, user.id, sort="collector", direction="asc", per_page=2)
    assert total == 52
    assert [r.id for r in result] == expected[:2]
