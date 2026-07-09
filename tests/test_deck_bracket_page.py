"""#82 — Bracket Evaluation page: read-only loader round-trip + route wiring.

Covers the genuinely new logic: `load_persisted_estimate` (SQL rows -> template
dict) and the GET route's ownership gate + empty state. The estimator compute
itself is exercised by the existing bracket_v2_service suite; here we only care
that persisted rows come back in the template shape and the route is scoped.
"""

import app.legacy_tables  # noqa: F401 — registers deck_bracket_* tables on Base.metadata
from app.bracket_v2_service import (
    BracketEstimate,
    Finding,
    load_persisted_estimate,
    persist_estimate,
)
from app.models import Deck, StorageLocation, User


def _deck(s, user_id):
    loc = StorageLocation(user_id=user_id, name="d", type="deck", mode="manual")
    s.add(loc)
    s.flush()
    deck = Deck(user_id=user_id, name="d", storage_location_id=loc.id)
    s.add(deck)
    s.flush()
    return deck


def test_load_persisted_estimate_none_when_absent(db, user):
    deck = _deck(db, user.id)
    assert load_persisted_estimate(db, deck.id) is None


def test_load_persisted_estimate_round_trip(db, user):
    deck = _deck(db, user.id)
    est = BracketEstimate(
        mechanics_bracket=3,
        final_bracket=4,
        findings=[Finding("fast_mana", "Sol Ring", "warning", "Fast mana present")],
        score=57.0,
        intent_bracket=4,
        confidence_tagging_coverage=0.5,
        confidence_mechanics_clarity=1.0,
        confidence_intent_alignment=0.8,
    )
    persist_estimate(db, deck.id, est)

    got = load_persisted_estimate(db, deck.id)
    assert got["bracket"] == 4
    assert got["mechanics_bracket"] == 3
    assert got["intent_bracket"] == 4
    assert got["score"] == 57.0
    assert got["confidence"]["tagging_coverage"] == 0.5
    assert got["confidence"]["intent_alignment"] == 0.8
    assert len(got["findings"]) == 1
    assert got["findings"][0]["severity"] == "warning"
    assert got["findings"][0]["message"] == "Fast mana present"
    assert got["generated_at"] is not None


def test_get_bracket_empty_state(client, db, user):
    deck = _deck(db, user.id)
    db.commit()
    resp = client.get(f"/decks/{deck.id}/bracket")
    assert resp.status_code == 200
    assert "No evaluation yet" in resp.text


def test_get_bracket_populated_renders(client, db, user):
    deck = _deck(db, user.id)
    persist_estimate(
        db,
        deck.id,
        BracketEstimate(
            mechanics_bracket=3,
            final_bracket=4,
            findings=[Finding("fast_mana", "Sol Ring", "warning", "Fast mana present")],
            score=57.0,
            intent_bracket=4,
            confidence_tagging_coverage=0.5,
        ),
    )
    db.commit()
    resp = client.get(f"/decks/{deck.id}/bracket")
    assert resp.status_code == 200
    assert "bracket-v2-badge" in resp.text
    assert "Fast mana present" in resp.text
    assert "Re-evaluate" in resp.text


def test_get_bracket_non_owner_404(client, db):
    """A deck owned by someone else is a 404, not a data leak."""
    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.commit()
    deck = _deck(db, other.id)
    db.commit()
    resp = client.get(f"/decks/{deck.id}/bracket")
    assert resp.status_code == 404
