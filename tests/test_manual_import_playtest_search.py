"""The import printing picker must request Scryfall's playtest-card extras."""

from urllib.parse import parse_qs, urlparse

import pytest

from app import scryfall


@pytest.mark.parametrize(
    "name,set_code,number,is_extra",
    [
        ("Fludge, Gunk Guardian", "mb2", "593", True),
        ("Sol Ring", "cmm", "396", False),
    ],
)
def test_import_search_renders_playtest_and_regular_printings(
    client, monkeypatch, name, set_code, number, is_extra
):
    calls = []

    def search(url):
        params = parse_qs(urlparse(url).query)
        calls.append(params)
        # Model Scryfall's observed exclusion, not a fixture that always succeeds.
        if is_extra and params.get("include_extras") != ["true"]:
            return None
        return {
            "data": [
                {
                    "id": "test-printing-id",
                    "name": name,
                    "set": set_code,
                    "set_name": "Mystery Booster 2" if is_extra else "Commander Masters",
                    "collector_number": number,
                    "rarity": "rare",
                    "image_uris": {},
                    "prices": {},
                }
            ],
            "has_more": False,
        }

    monkeypatch.setattr(scryfall, "_get_json", search)
    response = client.post("/import/manual/search", data={"name": name, "csrf_token": "x"})
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["unique"] == ["prints"]
    # The search heading repeats the query even with zero results: assert the
    # selectable printing, not merely the presence of the searched card name.
    assert f"{set_code.upper()} #{number}" in response.text
    assert 'value="test-printing-id"' in response.text
    assert 'action="/import/manual/preview"' in response.text


def test_import_search_still_follows_next_page(monkeypatch):
    calls = []
    next_page = "https://api.scryfall.com/cards/search?page=2&include_extras=true"

    def search(url):
        calls.append(url)
        if len(calls) == 1:
            assert parse_qs(urlparse(url).query)["include_extras"] == ["true"]
            return {"data": [{"id": "first"}], "has_more": True, "next_page": next_page}
        assert url == next_page
        return {"data": [{"id": "second"}], "has_more": False}

    monkeypatch.setattr(scryfall, "_get_json", search)
    assert [c["id"] for c in scryfall.search_cards_by_name("Fludge")] == ["first", "second"]
    assert len(calls) == 2
