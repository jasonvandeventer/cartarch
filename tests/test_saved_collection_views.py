"""Saved views replay the normal Collection query and never store a page/action."""

from urllib.parse import parse_qs, urlsplit

from app.models import Card, InventoryRow, SavedCollectionView, User


def test_save_apply_rename_replace_delete_preserves_live_results(client, db, user):
    card = Card(
        scryfall_id="view-card",
        name="Sol Ring",
        type_line="Artifact",
        set_code="tst",
        collector_number="1",
    )
    other = Card(
        scryfall_id="view-other",
        name="Forest",
        type_line="Basic Land — Forest",
        set_code="tst",
        collector_number="1",
    )
    db.add_all([card, other])
    db.flush()
    db.add_all([InventoryRow(user_id=user.id, card_id=c.id, quantity=1) for c in (card, other)])
    db.commit()
    query = "search=Sol+Ring&view=rows&sort=name&direction=asc&page=7&bulk=deleted&next=https://example.invalid"
    response = client.post(
        "/collection/views/save?" + query, data={"name": "  My rings  "}, follow_redirects=False
    )
    assert response.status_code == 303
    saved = db.query(SavedCollectionView).one()
    assert saved.name == "My rings" and saved.user_id == user.id
    params = parse_qs(saved.query_string)
    assert params["search"] == ["Sol Ring"] and params["view"] == ["rows"]
    assert not {"page", "bulk", "next"}.intersection(params)
    page = client.get(f"/collection/views/{saved.id}")
    assert page.status_code == 200
    assert "Sol Ring" in page.text and "Forest" not in page.text
    assert "My rings" in page.text and 'name="csrf_token"' in page.text
    original = saved.query_string
    client.post(
        "/collection/views/save?search=Forest",
        data={"view_id": saved.id, "name": "Renamed", "replace_filters": "false"},
    )
    db.refresh(saved)
    assert saved.name == "Renamed" and saved.query_string == original
    client.post(
        "/collection/views/save?search=Forest&view=grid",
        data={"view_id": saved.id, "name": "Renamed", "replace_filters": "true"},
    )
    db.refresh(saved)
    page = client.get(f"/collection/views/{saved.id}")
    assert "Forest" in page.text and "Sol Ring" not in page.text
    response = client.post(
        f"/collection/views/{saved.id}/delete?search=Forest", follow_redirects=False
    )
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query)["search"] == ["Forest"]
    assert db.query(SavedCollectionView).count() == 0
    assert db.query(InventoryRow).count() == 2


def test_repeated_facets_names_duplicates_and_owner_scope(client, db, user):
    query = "colors=W&colors=U&types=Artifact&types=Creature&status=pending&finishes=foil&finishes=etched&loc=12&loc=34&price_min=1.25&price_max=10"
    response = client.post(
        "/collection/views/save?" + query, data={"name": '<script>alert("x")</script>'}
    )
    assert response.status_code == 200
    assert '<script>alert("x")</script>' not in response.text
    saved = db.query(SavedCollectionView).one()
    params = parse_qs(saved.query_string)
    assert params["colors"] == ["WU"] and params["loc"] == ["12,34"]
    assert params["types"] == ["Artifact,Creature"]
    assert params["finishes"] == ["foil,etched"]
    original = saved.query_string
    response = client.post("/collection/views/save?search=overwrite", data={"name": saved.name})
    assert "That name is already used" in response.text
    db.refresh(saved)
    assert saved.query_string == original and db.query(SavedCollectionView).count() == 1
    for name in [" ", "a" * 65]:
        assert (
            "Use a name of 1–64" in client.post("/collection/views/save", data={"name": name}).text
        )
    other = User(username="saved-view-other", password_hash="x")
    db.add(other)
    db.flush()
    private = SavedCollectionView(
        user_id=other.id, name="Private bookmark", query_string="search=secret"
    )
    db.add(private)
    db.commit()
    assert "Private bookmark" not in client.get("/collection").text
    assert client.get(f"/collection/views/{private.id}").status_code == 404
    assert (
        client.post(
            "/collection/views/save", data={"view_id": private.id, "name": "Stolen"}
        ).status_code
        == 404
    )
    assert client.post(f"/collection/views/{private.id}/delete").status_code == 404
    db.refresh(private)
    assert private.name == "Private bookmark"


def test_unavailable_location_does_not_silently_widen_a_saved_view(client, db, user):
    saved = SavedCollectionView(
        user_id=user.id, name="Old location", query_string="location_id=999999"
    )
    db.add(saved)
    db.commit()
    response = client.get(f"/collection/views/{saved.id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/collection?view_notice=missing_location"
    assert "was not applied" in client.get(response.headers["location"]).text
