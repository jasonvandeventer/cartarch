"""#180 — Scryfall's ``edhrec_rank`` as a bounded popularity prior.

The one licensed, explicitly-offered play-frequency signal available: EDHREC has
no official API and playgroup.gg's public API is commander-level only. It rides
the existing Scryfall seam (29th key) and feeds ``score_candidate`` as a
tie-breaker that must never outvote commander fit.
"""

import app.legacy_tables  # noqa
from app import deck_service
from app import recommendation_service as rec
from app.models import Card
from app.scryfall import _normalize_card_payload, card_constructor_kwargs

RAW = {
    "id": "sf-rank",
    "name": "Sol Ring",
    "set": "c21",
    "set_name": "Commander 2021",
    "collector_number": "263",
    "rarity": "uncommon",
    "type_line": "Artifact",
    "oracle_text": "{T}: Add {C}{C}.",
    "cmc": 1.0,
    "edhrec_rank": 1,
}


def _cand(card):
    return rec.CandidateCard(card, 1, 1, None, [], [], [])


def _card(**kw):
    base = dict(
        name="X",
        scryfall_id="s",
        set_code="tst",
        set_name="T",
        collector_number="1",
        rarity="rare",
        type_line="Artifact",
        oracle_text="",
        cmc=2.0,
    )
    base.update(kw)
    return Card(**base)


# --- the seam ---------------------------------------------------------------


def test_normalizer_carries_the_rank_last():
    payload = _normalize_card_payload(RAW)
    assert payload["edhrec_rank"] == 1
    assert list(payload)[-1] == "edhrec_rank", "must be appended LAST — byte-identical seam"


def test_a_card_without_a_rank_normalizes_to_none():
    payload = _normalize_card_payload({k: v for k, v in RAW.items() if k != "edhrec_rank"})
    assert payload["edhrec_rank"] is None


def test_the_rank_is_a_card_column_and_survives_the_constructor():
    """It is a Card ORM column, so card_constructor_kwargs must NOT strip it."""
    payload = _normalize_card_payload(RAW)
    card = Card(**card_constructor_kwargs(payload))
    assert card.edhrec_rank == 1


# --- the scoring prior ------------------------------------------------------


def test_tiers_award_most_to_the_most_played():
    top, _ = rec.edhrec_rank_bonus(_card(edhrec_rank=1))
    mid, _ = rec.edhrec_rank_bonus(_card(edhrec_rank=2000))
    low, _ = rec.edhrec_rank_bonus(_card(edhrec_rank=9000))
    assert top > mid > low > 0


def test_an_unranked_card_is_neutral_never_penalized():
    """NULL is missing data — a card whose row predates the backfill, a token,
    a non-EDH-legal card. Scoring it negative would punish the un-refreshed
    half of a collection."""
    assert rec.edhrec_rank_bonus(_card(edhrec_rank=None)) == (0.0, None)
    assert rec.edhrec_rank_bonus(_card(edhrec_rank=0)) == (0.0, None)


def test_a_rank_past_the_last_tier_earns_nothing():
    assert rec.edhrec_rank_bonus(_card(edhrec_rank=50_000)) == (0.0, None)


def test_the_reason_names_the_actual_rank():
    _, reason = rec.edhrec_rank_bonus(_card(edhrec_rank=1))
    assert reason and "#1" in reason and "Commander" in reason


def test_the_prior_cannot_outvote_commander_fit():
    """THE bound that matters: a global rank knows a card is strong and nothing
    about whether it fits THIS commander. A top-500 staple that misses the
    theme must still lose to an on-theme card that has no rank at all."""
    commander = _card(
        name="Atraxa, Praetors' Voice",
        type_line="Legendary Creature — Phyrexian Angel Horror",
        oracle_text="At the beginning of your end step, put a +1/+1 counter on "
        "each creature you control.",
    )
    themes = rec.extract_themes(commander)
    staple = _cand(_card(name="Staple", edhrec_rank=1))
    on_theme = _cand(_card(name="Fits", oracle_text="Put a +1/+1 counter on target creature."))
    intent = rec.DeckBuildIntent(commander_card_id=1)

    # Precondition, or the comparison below proves nothing (the vacuous-test trap).
    assert deck_service.card_matches_theme(on_theme.card, themes)
    assert not deck_service.card_matches_theme(staple.card, themes)

    assert rec.score_candidate(on_theme, themes, intent) > rec.score_candidate(
        staple, themes, intent
    )


def test_the_prior_breaks_a_tie_between_otherwise_equal_cards():
    themes = rec.extract_themes(_card(name="Vanilla", type_line="Legendary Creature — Bear"))
    intent = rec.DeckBuildIntent(commander_card_id=1)
    ranked = _cand(_card(name="A", edhrec_rank=1))
    unranked = _cand(_card(name="B"))
    assert rec.score_candidate(ranked, themes, intent) > rec.score_candidate(
        unranked, themes, intent
    )
    assert any("Commander" in r for r in ranked.reasons), "the reason must reach the user"
