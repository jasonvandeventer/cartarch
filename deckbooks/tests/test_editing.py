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
    initialize("osha-violation", refresh=False)

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
    initialize("osha-violation", refresh=False)
    sid = "28180667-cc1e-4f64-9a69-00425ef85ba0"
    editing.update_decision(sid, {"status": "keep", "finalize": "1"})
    editing.update_decision(sid, {"status": "upgrade"})  # change after finalize

    revs = [r for r in repository.load_revisions("osha-violation") if r["deck_card_id"] == sid]
    assert [r["change_type"] for r in revs] == ["decision_finalized", "decision_revised"]


def test_select_printing_sets_the_definitive_copy(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    initialize("osha-violation", refresh=False)
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
    initialize("osha-violation", refresh=False)
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


def test_card_briefing_lists_printings_and_criteria(tmp_path, monkeypatch):
    """The ChatGPT briefing carries the aesthetic criteria + every printing +
    the selection rule, so a model reasons over data, not screenshots."""
    from deckbooks import briefing
    from deckbooks.tests.test_foundation import _isolate

    _isolate(tmp_path, monkeypatch)
    initialize("osha-violation", refresh=False)
    text = briefing.card_briefing("28180667-cc1e-4f64-9a69-00425ef85ba0")  # Arcane Signet
    assert text is not None
    assert "Aesthetic pillars" in text and "Selection rule" in text
    assert "Every official printing" in text and "scryfall.com/card/" in text
    # Policy v3: two-stage Current + Destination framing, steer away from
    # rarity/price defaults, and no leftover Definitive/Museum-pick labels.
    assert "Current printing" in text and "Destination printing" in text
    assert "current printing and destination upgrade" in text.lower()
    assert "rarest" in text.lower()
    assert "Definitive" not in text and "Museum piece" not in text
    # Unknown card → None (route turns it into a 404).
    assert briefing.card_briefing("nope") is None


def test_set_current_printing_preserves_finish(tmp_path, monkeypatch):
    """Setting 'My copy' changes the physical printing but keeps the finish
    unless a new one is given (correcting the printing, not the finish)."""
    from deckbooks import editing, repository
    from deckbooks.tests.test_foundation import _isolate

    _isolate(tmp_path, monkeypatch)
    initialize("osha-violation", refresh=False)
    sol = "e5ba8c01-b6f5-486d-b300-cbae2c2b5edf"  # Sol Ring, seeded as foil
    editing.update_decision(sol, {"current_scryfall_id": "some-other-print"})
    card = next(c for c in repository.load_cards("osha-violation") if c["deck_card_id"] == sol)
    assert card["current_printing"] == {"scryfall_id": "some-other-print", "finish": "foil"}


def test_museum_states_and_no_edition_clears_picks(tmp_path, monkeypatch):
    """The four Museum states, and that 'no separate edition' is exclusive —
    it clears any official pick / proxy so the card can't be both."""
    from deckbooks import editing, repository, services
    from deckbooks.models import museum_state
    from deckbooks.tests.test_foundation import _isolate

    _isolate(tmp_path, monkeypatch)
    initialize("osha-violation", refresh=False)
    cards = {c["card_name"]: c for c in repository.load_cards("osha-violation")}

    arcane = cards["Arcane Signet"]["deck_card_id"]
    sol = cards["Sol Ring"]["deck_card_id"]

    # custom proxy (no official pick) → 'custom_proxy'
    editing.update_decision(arcane, {"custom_proxy_candidate": "1"})
    a = next(c for c in repository.load_cards("osha-violation") if c["deck_card_id"] == arcane)
    assert museum_state(a) == "custom_proxy"

    # no separate edition clears any official pick + proxy → 'no_edition'
    editing.update_decision(sol, {"museum_scryfall_id": "x", "museum_finish": "foil"})
    editing.update_decision(sol, {"no_museum_edition": "1"})
    s = next(c for c in repository.load_cards("osha-violation") if c["deck_card_id"] == sol)
    assert s["decision"]["museum_printing"] is None
    assert museum_state(s) == "no_edition"

    # museum_wall now renders the DESTINATION (selected) printing as the binder,
    # so its totals are chosen/awaiting only — the museum-pick states above still
    # come from models.museum_state (a separate per-card decision function).
    t = services.museum_wall()["totals"]
    assert "custom_proxy" not in t and "no_edition" not in t
    assert t["chosen"] >= 1  # Bello has a selected/destination printing
    # Bello keeps its official pick → still 'chosen', not awaiting.
    bello = next(
        c for c in repository.load_cards("osha-violation") if c["card_name"].startswith("Bello")
    )
    assert museum_state(bello) == "chosen"
