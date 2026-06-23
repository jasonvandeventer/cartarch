"""Deck export round-trips foil/finish status (issue: exporter omits finish).

The deck text-list export (`GET /decks/{id}/export`) must emit the importer's
MTGA-style ``*F*`` foil marker for foil rows, so an export→re-import round-trip
matches a foil copy back to its ``(card_id, finish)`` inventory row instead of
treating it as a brand-new card. Older exports without the marker still parse as
normal finish (backward compatible).

Invoke via:

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_deck_export.py
"""

from __future__ import annotations

import itertools

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import deck_service
from app.db import Base
from app.import_service import _parse_list_line
from app.models import Card, InventoryRow, User

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


def _place(s, user_id, card, loc_id, qty=1, finish="normal", role=None) -> InventoryRow:
    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=qty,
        finish=finish,
        is_proxy=False,
        storage_location_id=loc_id,
        is_pending=False,
        role=role,
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


def test_export_marks_foil_rows_and_omits_marker_for_normal():
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Stash")
    loc_id = deck.storage_location_id
    normal_card = _card(s, "Sol Ring")
    foil_card = _card(s, "Mana Crypt")
    _place(s, u.id, normal_card, loc_id, finish="normal")
    _place(s, u.id, foil_card, loc_id, finish="foil")
    s.commit()

    c = _client(sm, u)
    try:
        r = c.get(f"/decks/{deck.id}/export")
        assert r.status_code == 200
    finally:
        _clear_overrides()

    lines = {ln.split(" ", 1)[1].split(" (")[0]: ln for ln in r.text.splitlines() if " (" in ln}
    # The foil row carries the *F* marker; the normal row does not.
    assert lines["Mana Crypt"].endswith("*F*")
    assert "*F*" not in lines["Sol Ring"]


def test_exported_foil_line_reparses_as_foil():
    """Round-trip: the exported foil line, fed back through the importer's own
    line parser, resolves to finish='foil' so re-import matches (card_id, foil)."""
    sm = _fresh()
    s = sm()
    u = _user(s)
    deck = deck_service.create_deck(s, u.id, "Stash")
    foil_card = _card(s, "Cyclonic Rift")
    _place(s, u.id, foil_card, deck.storage_location_id, finish="foil")
    s.commit()

    c = _client(sm, u)
    try:
        r = c.get(f"/decks/{deck.id}/export")
    finally:
        _clear_overrides()

    foil_line = next(ln for ln in r.text.splitlines() if "Cyclonic Rift" in ln)
    parsed = _parse_list_line(foil_line)
    assert parsed is not None
    assert parsed["finish"] == "foil"


def test_legacy_export_without_marker_parses_as_normal():
    """A pre-fix export line (no *F* marker) still parses — defaults to normal."""
    parsed = _parse_list_line("1 Sol Ring (TST) 1")
    assert parsed is not None
    assert parsed["finish"] == "normal"
