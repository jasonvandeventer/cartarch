"""Issue #70 — brew proxy semantics on the per-row (Location-column) CSV
import path.

Pre-#70, rows resolving to a brew deck via the v3.30.15 per-row Location
bypass were persisted directly placed with is_proxy=False — the bypass never
consulted the brew proxy rule that lives in
_commit_deck_import_with_reconciliation. Option D (the locked design) fixes
this by PARTITIONING rows after _build_line_to_location_map: rows resolving
to a brew deck's storage location reroute through the reconciliation commit
handler (owned → pulled real, unowned → proxy); every other row (drawers,
boxes, non-brew decks) keeps the bypass unchanged.

Also covers the auto-create leg: an ``is_brew`` CSV column (same strict
empty/'true'/'false' grammar as ``is_proxy``) marks a Location name so that
auto-creating it as a deck lands it as a brew.
"""

from __future__ import annotations

import itertools

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.legacy_tables  # noqa: F401 — registers deck_bracket_* tables standalone
from app import deck_service
from app.db import Base
from app.models import Card, Deck, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _fresh():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _user(s, username="u1") -> User:
    u = User(username=username, password_hash="x")
    s.add(u)
    s.flush()
    return u


def _card(s, name="Sol Ring") -> Card:
    """Non-stale Card so persist_import_rows resolves it locally (no network)."""
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="Test",
        collector_number=str(next(_seq)),
        rarity="rare",
        type_line="Artifact",
        oracle_text="x",
        image_url="http://x/img.png",
        color_identity="",
        set_type="expansion",
    )
    s.add(c)
    s.flush()
    return c


def _loc(s, user_id, name, type_="box", mode="managed") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode=mode)
    s.add(loc)
    s.flush()
    return loc


def _place(s, user_id, card, loc_id, qty=1, proxy=False) -> InventoryRow:
    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=qty,
        finish="normal",
        is_proxy=proxy,
        storage_location_id=loc_id,
        is_pending=False,
    )
    s.add(row)
    s.flush()
    return row


def _client(sm, user):
    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    def _db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _db
    main.app.dependency_overrides[get_current_user] = lambda: user
    main.app.dependency_overrides[require_csrf_token] = lambda: None
    return TestClient(main.app, follow_redirects=False)


def _clear_overrides():
    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    for dep in (get_db_session, get_current_user, require_csrf_token):
        main.app.dependency_overrides.pop(dep, None)


def _rows_at(s, loc_id):
    return s.query(InventoryRow).filter(InventoryRow.storage_location_id == loc_id).all()


def _perrow_form(card_loc_pairs, choices, is_brew=None):
    """Parallel-array form for POST /import/commit taking the v3.30.15 per-row
    Location resolution path (no reconcile_action fields → no reconciliation).

    card_loc_pairs: list of (Card, location_name); choices: list of
    (choice_name, choice_id, choice_type) resolution triples.
    """
    n = len(card_loc_pairs)
    data = {
        "filename": "cards.csv",
        "target_location_id": "0",
        "line_number": [str(i + 1) for i in range(n)],
        "name": [""] * n,
        "scryfall_id": [c.scryfall_id for c, _ in card_loc_pairs],
        "set_code": [""] * n,
        "collector_number": [""] * n,
        "finish": ["normal"] * n,
        "quantity": ["1"] * n,
        "location": [loc for _, loc in card_loc_pairs],
        "location_choice_name": [name for name, _, _ in choices],
        "location_choice_id": [str(cid) for _, cid, _ in choices],
        "location_choice_type": [ctype for _, _, ctype in choices],
    }
    if is_brew is not None:
        data["is_brew"] = is_brew
    return data


# --------------------------------------------------------------------------- #
# 4a — per-row import into an EXISTING brew deck applies the proxy rule
# --------------------------------------------------------------------------- #


def test_perrow_existing_brew_proxies_unowned_moves_owned():
    sm = _fresh()
    s = sm()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "MyBrew", is_brew=True)
    box = _loc(s, u.id, "Box")
    owned = _card(s, "Smothering Tithe")
    unowned = _card(s, "Rhystic Study")
    _place(s, u.id, owned, box.id, qty=1, proxy=False)
    s.commit()

    c = _client(sm, u)
    try:
        r = c.post(
            "/import/commit",
            data=_perrow_form(
                [(owned, "MyBrew"), (unowned, "MyBrew")],
                [("MyBrew", brew.storage_location_id, "")],
            ),
        )
        assert r.status_code == 200, r.text
    finally:
        _clear_overrides()

    rows = {r.card_id: r for r in _rows_at(s, brew.storage_location_id)}
    assert rows[owned.id].is_proxy is False  # owned → pulled real copy
    assert rows[unowned.id].is_proxy is True  # unowned → proxy (the #70 bug)
    # The owned copy MOVED — no duplicate real row left in the box.
    assert _rows_at(s, box.id) == []


# --------------------------------------------------------------------------- #
# 4b — per-row import into a NON-brew deck: bypass path unchanged, no proxies
# --------------------------------------------------------------------------- #


def test_perrow_non_brew_deck_no_proxy_flagging():
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Normal Deck")  # is_brew False
    unowned = _card(s, "Cyclonic Rift")
    s.commit()

    c = _client(sm, u)
    try:
        r = c.post(
            "/import/commit",
            data=_perrow_form(
                [(unowned, "Normal Deck")],
                [("Normal Deck", deck.storage_location_id, "")],
            ),
        )
        assert r.status_code == 200, r.text
    finally:
        _clear_overrides()

    rows = _rows_at(s, deck.storage_location_id)
    assert len(rows) == 1
    assert rows[0].is_proxy is False
    assert rows[0].is_pending is False


# --------------------------------------------------------------------------- #
# 4c — mixed-destination CSV: brew rows get proxy semantics, drawer rows
# don't, and no rows are dropped
# --------------------------------------------------------------------------- #


def test_perrow_mixed_brew_and_box_destinations():
    sm = _fresh()
    s = sm()
    u = _user(s)
    brew = deck_service.create_deck(s, u.id, "MyBrew", is_brew=True)
    box = _loc(s, u.id, "Box A")
    to_brew = _card(s, "Rhystic Study")
    to_box = _card(s, "Lightning Bolt")
    s.commit()

    c = _client(sm, u)
    try:
        r = c.post(
            "/import/commit",
            data=_perrow_form(
                [(to_brew, "MyBrew"), (to_box, "Box A")],
                [("MyBrew", brew.storage_location_id, ""), ("Box A", box.id, "")],
            ),
        )
        assert r.status_code == 200, r.text
    finally:
        _clear_overrides()

    brew_rows = _rows_at(s, brew.storage_location_id)
    box_rows = _rows_at(s, box.id)
    assert len(brew_rows) == 1 and brew_rows[0].is_proxy is True
    assert len(box_rows) == 1 and box_rows[0].is_proxy is False
    # No rows dropped anywhere.
    assert s.query(InventoryRow).count() == 2


# --------------------------------------------------------------------------- #
# 4d / 4e — auto-create: is_brew column creates a brew deck; absent → normal
# --------------------------------------------------------------------------- #


def test_perrow_auto_create_with_is_brew_creates_brew_and_proxies():
    sm = _fresh()
    s = sm()
    u = _user(s)
    unowned = _card(s, "Mana Crypt")
    s.commit()

    c = _client(sm, u)
    try:
        r = c.post(
            "/import/commit",
            data=_perrow_form(
                [(unowned, "NewBrew")],
                [("NewBrew", 0, "deck")],  # choice_id 0 → auto-create as deck
                is_brew=["true"],
            ),
        )
        assert r.status_code == 200, r.text
    finally:
        _clear_overrides()

    deck = s.query(Deck).filter_by(user_id=u.id, name="NewBrew").one()
    assert deck.is_brew is True
    rows = _rows_at(s, deck.storage_location_id)
    assert len(rows) == 1
    assert rows[0].is_proxy is True  # unowned into a fresh brew → proxy


def test_perrow_auto_create_without_is_brew_creates_normal_deck():
    sm = _fresh()
    s = sm()
    u = _user(s)
    unowned = _card(s, "Ponder")
    s.commit()

    c = _client(sm, u)
    try:
        r = c.post(
            "/import/commit",
            data=_perrow_form(
                [(unowned, "NewDeck")],
                [("NewDeck", 0, "deck")],
            ),
        )
        assert r.status_code == 200, r.text
    finally:
        _clear_overrides()

    deck = s.query(Deck).filter_by(user_id=u.id, name="NewDeck").one()
    assert deck.is_brew is False
    rows = _rows_at(s, deck.storage_location_id)
    assert len(rows) == 1
    assert rows[0].is_proxy is False  # non-brew deck → bypass, no proxy


# --------------------------------------------------------------------------- #
# CSV column plumbing — parse_scanner_csv recognizes/validates is_brew
# --------------------------------------------------------------------------- #


def test_parse_scanner_csv_reads_and_validates_is_brew(monkeypatch):
    import app.import_service as import_service
    from app.scryfall import BulkFetchResult

    payload = {
        "scryfall_id": "sid-x",
        "name": "Sol Ring",
        "set_code": "tst",
        "collector_number": "1",
    }
    monkeypatch.setattr(
        import_service,
        "bulk_refresh_prices",
        lambda ids: BulkFetchResult(cards={"sid-x": payload}),
    )
    csv_text = (
        "scryfall_id,finish,quantity,location,is_brew\n"
        "sid-x,normal,1,NewBrew,true\n"
        "sid-x,normal,1,NewBrew,\n"
        "sid-x,normal,1,NewBrew,banana\n"
    )
    out = import_service.parse_scanner_csv(csv_text.encode())
    assert [r["is_brew"] for r in out["valid_rows"]] == [True, False]
    assert len(out["invalid_rows"]) == 1
    assert "Is Brew" in out["invalid_rows"][0]["reason"]
