"""Unit tests for the shared card-list SORT spec (v3.36.11).

Pins the single-source-of-truth sort behavior the shared dropdown depends on:
rarity rank, WUBRG color order, finish-aware/computed Python sorts, NULL/0
placement, and the stable name->id tiebreaker that keeps equal-key rows from
reshuffling across HTMX swaps. Pure helpers are tested on synthetic item dicts
(no DB); the Showcase wiring is covered in tests/test_share_service.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from app import sort_spec


def _card(name="X", cmc=None, colors="", set_code="aaa", collector="1", rarity=None):
    return SimpleNamespace(
        name=name,
        cmc=cmc,
        colors=colors,
        set_code=set_code,
        collector_number=collector,
        rarity=rarity,
    )


def _item(idx, card, available=1, price=0.0, added_at=0):
    return {
        "id": idx,
        "card": card,
        "finish": "normal",
        "available": available,
        "effective_price": price,
        "added_at": added_at,
    }


# --- rarity rank -------------------------------------------------------------


def test_rarity_rank_ascending_common_to_bonus():
    order = ["common", "uncommon", "rare", "mythic", "special", "bonus"]
    ranks = [sort_spec.rarity_rank(r) for r in order]
    assert ranks == sorted(ranks)
    assert ranks == [0, 1, 2, 3, 4, 5]


def test_rarity_rank_unknown_and_null_sort_last():
    assert sort_spec.rarity_rank(None) == sort_spec.RARITY_RANK_UNKNOWN
    assert sort_spec.rarity_rank("token") == sort_spec.RARITY_RANK_UNKNOWN
    assert sort_spec.rarity_rank(None) > sort_spec.rarity_rank("mythic")
    # Case-insensitive.
    assert sort_spec.rarity_rank("MYTHIC") == sort_spec.rarity_rank("mythic")


# --- color order -------------------------------------------------------------


def test_color_order_wubrg_then_multi_then_colorless():
    keys = {
        "W": sort_spec.color_sort_value(_card(colors="W")),
        "U": sort_spec.color_sort_value(_card(colors="U")),
        "B": sort_spec.color_sort_value(_card(colors="B")),
        "R": sort_spec.color_sort_value(_card(colors="R")),
        "G": sort_spec.color_sort_value(_card(colors="G")),
        "multi": sort_spec.color_sort_value(_card(colors="W U")),
        "colorless": sort_spec.color_sort_value(_card(colors="")),
    }
    assert keys["W"] < keys["U"] < keys["B"] < keys["R"] < keys["G"]
    assert keys["G"] < keys["multi"] < keys["colorless"]


# --- showcase item sorting ---------------------------------------------------


def test_sort_by_name_asc_and_desc():
    items = [
        _item(1, _card(name="Bayou")),
        _item(2, _card(name="Aether Vial")),
        _item(3, _card(name="Counterspell")),
    ]
    asc = sort_spec.sort_showcase_items(list(items), "name", "asc")
    assert [it["card"].name for it in asc] == ["Aether Vial", "Bayou", "Counterspell"]
    desc = sort_spec.sort_showcase_items(list(items), "name", "desc")
    assert [it["card"].name for it in desc] == ["Counterspell", "Bayou", "Aether Vial"]


def test_cmc_nulls_sort_last_in_both_directions():
    items = [
        _item(1, _card(name="A", cmc=3.0)),
        _item(2, _card(name="B", cmc=None)),
        _item(3, _card(name="C", cmc=1.0)),
    ]
    asc = sort_spec.sort_showcase_items(list(items), "cmc", "asc")
    assert [it["card"].cmc for it in asc] == [1.0, 3.0, None]
    desc = sort_spec.sort_showcase_items(list(items), "cmc", "desc")
    assert [it["card"].cmc for it in desc] == [3.0, 1.0, None]


def test_price_zero_and_null_sort_last():
    items = [
        _item(1, _card(name="A"), price=5.0),
        _item(2, _card(name="B"), price=0.0),
        _item(3, _card(name="C"), price=12.5),
        _item(4, _card(name="D"), price=None),
    ]
    desc = sort_spec.sort_showcase_items(list(items), "price", "desc")
    names = [it["card"].name for it in desc]
    # Non-zero by price desc, then the 0/None group last (tiebreaker name asc).
    assert names == ["C", "A", "B", "D"]
    asc = sort_spec.sort_showcase_items(list(items), "price", "asc")
    assert [it["card"].name for it in asc] == ["A", "C", "B", "D"]


def test_available_uses_computed_quantity():
    items = [
        _item(1, _card(name="A"), available=2),
        _item(2, _card(name="B"), available=9),
        _item(3, _card(name="C"), available=5),
    ]
    desc = sort_spec.sort_showcase_items(list(items), "available", "desc")
    assert [it["available"] for it in desc] == [9, 5, 2]


def test_rarity_sort_orders_by_rank_not_alpha():
    items = [
        _item(1, _card(name="A", rarity="mythic")),
        _item(2, _card(name="B", rarity="common")),
        _item(3, _card(name="C", rarity="rare")),
    ]
    asc = sort_spec.sort_showcase_items(list(items), "rarity", "asc")
    assert [it["card"].rarity for it in asc] == ["common", "rare", "mythic"]


def test_set_collector_tiebreaker_is_lexical():
    # Documented decision (d): lexical collector tiebreak => "#10" before "#2".
    items = [
        _item(1, _card(name="A", set_code="abc", collector="2")),
        _item(2, _card(name="B", set_code="abc", collector="10")),
    ]
    asc = sort_spec.sort_showcase_items(list(items), "set", "asc")
    assert [it["card"].collector_number for it in asc] == ["10", "2"]


def test_stable_tiebreaker_breaks_equal_keys_by_name_then_id():
    # All same cmc -> ties must resolve deterministically by name, then id.
    items = [
        _item(3, _card(name="Zebra", cmc=2.0)),
        _item(1, _card(name="Apple", cmc=2.0)),
        _item(2, _card(name="Apple", cmc=2.0)),
    ]
    out = sort_spec.sort_showcase_items(list(items), "cmc", "asc")
    assert [(it["card"].name, it["id"]) for it in out] == [
        ("Apple", 1),
        ("Apple", 2),
        ("Zebra", 3),
    ]
    # Descending the primary key must NOT reshuffle the equal-key tie order.
    out_desc = sort_spec.sort_showcase_items(list(items), "cmc", "desc")
    assert [(it["card"].name, it["id"]) for it in out_desc] == [
        ("Apple", 1),
        ("Apple", 2),
        ("Zebra", 3),
    ]


def test_unknown_sort_key_leaves_order_untouched():
    items = [_item(1, _card(name="B")), _item(2, _card(name="A"))]
    out = sort_spec.sort_showcase_items(list(items), "bogus", "asc")
    assert [it["id"] for it in out] == [1, 2]


def test_added_default_respects_direction():
    items = [
        _item(1, _card(name="A"), added_at=10),
        _item(2, _card(name="B"), added_at=30),
        _item(3, _card(name="C"), added_at=20),
    ]
    desc = sort_spec.sort_showcase_items(list(items), "added", "desc")
    assert [it["added_at"] for it in desc] == [30, 20, 10]


def test_invalid_direction_defaults_to_desc():
    assert sort_spec.normalize_direction("sideways") == "desc"
    assert sort_spec.normalize_direction(None) == "desc"
    assert sort_spec.normalize_direction("asc") == "asc"


def test_showcase_options_include_canonical_seven_plus_added():
    keys = [k for k, _ in sort_spec.SHOWCASE_SORT_OPTIONS]
    assert keys[0] == "added"
    for k in ("name", "cmc", "color", "set", "rarity", "price", "available"):
        assert k in keys
