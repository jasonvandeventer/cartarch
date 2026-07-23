"""Editing milestone: mutations persist, finalize logs a revision, a
printing pick from the browser updates the decision, and edits survive a
process restart (fresh load from disk)."""

from __future__ import annotations

from deckbooks import editing, repository, services
from deckbooks.init_deck import initialize
from deckbooks.models import curation_complete

# Reuse the foundation test's isolation helper (tmp data dir + fixture DB).
from deckbooks.tests.test_foundation import _isolate


def test_finalize_persists_and_logs_one_revision(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    initialize(refresh=False)

    sid = "28180667-cc1e-4f64-9a69-00425ef85ba0"  # Arcane Signet (research)
    editing.update_decision(
        sid,
        {
            "status": "keep",
            "verdict": "The BLC signet fits the workshop frame.",
            "reasoning": "Clean art\nCheap to own",
            "finalize": "1",
        },
    )

    # Re-read from disk — a fresh process would see the same (survives restart).
    card = next(c for c in repository.load_cards("osha-violation") if c["deck_card_id"] == sid)
    assert curation_complete(card)
    assert card["decision"]["finalized_at"]
    assert card["decision"]["reasoning"] == ["Clean art", "Cheap to own"]

    revs = [r for r in repository.load_revisions("osha-violation") if r["deck_card_id"] == sid]
    assert len(revs) == 1 and revs[0]["change_type"] == "decision_finalized"

    # And the derived dashboard moved: 2 finalized now (Bello + Arcane Signet).
    assert services.progress()["curated"] == 2


def test_editing_a_finalized_card_appends_a_revision(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    initialize(refresh=False)
    sid = "28180667-cc1e-4f64-9a69-00425ef85ba0"
    editing.update_decision(sid, {"status": "keep", "finalize": "1"})
    editing.update_decision(sid, {"status": "upgrade"})  # change after finalize

    revs = [r for r in repository.load_revisions("osha-violation") if r["deck_card_id"] == sid]
    assert [r["change_type"] for r in revs] == ["decision_finalized", "decision_revised"]


def test_select_printing_sets_the_definitive_copy(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    initialize(refresh=False)
    sid = "28180667-cc1e-4f64-9a69-00425ef85ba0"
    editing.update_decision(
        sid, {"selected_scryfall_id": "some-other-print", "selected_finish": "foil"}
    )
    card = next(c for c in repository.load_cards("osha-violation") if c["deck_card_id"] == sid)
    assert card["decision"]["selected_printing"] == {
        "scryfall_id": "some-other-print",
        "finish": "foil",
    }


def test_unknown_card_and_bad_values_do_not_crash(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    initialize(refresh=False)
    try:
        editing.update_decision("nope", {"status": "keep"})
        raise AssertionError("expected CardNotFound")
    except editing.CardNotFound:
        pass
    sid = "28180667-cc1e-4f64-9a69-00425ef85ba0"
    # Garbage status + price normalize rather than raise.
    editing.update_decision(sid, {"status": "banana", "price_paid": "not a number"})
    card = next(c for c in repository.load_cards("osha-violation") if c["deck_card_id"] == sid)
    assert card["decision"]["status"] == "pending"  # normalized
    assert card["acquisition"]["price_paid"] is None
