"""MTGJSON price ingest tests (MTGJSON ingest issue).

The MTGJSON fetch is mocked with in-memory fixtures (sample identifiers + price
JSON) — no live network. Asserts the Scryfall-id → uuid join populates
``card_prices``, the tcgplayer → cardkingdom → cardsphere provider fallback order,
finish matching (normal vs foil), cardmarket (EUR) exclusion, transient-miss
last-known retention, and manual-override precedence + survival across re-ingest.
"""

from __future__ import annotations

from app.jobs import price_ingest
from app.jobs.price_ingest import run_ingest, set_price_override
from app.models import Card, CardPrice

# --- Fixtures: sample identifiers + price JSON ------------------------------

# scryfall_id -> uuid
_IDENTIFIERS = {
    "sid-a": "uuid-a",  # tcgplayer wins over cardkingdom; has a foil too
    "sid-b": "uuid-b",  # no tcgplayer -> falls back to cardkingdom
    "sid-c": "uuid-c",  # only cardsphere; cardmarket (EUR) must be ignored
    # sid-d intentionally absent -> unresolved-to-uuid log path
}


def _paper(**providers):
    """Build a MTGJSON ``paper`` dict from {provider: {finish: {date: value}}}."""
    return {p: {"retail": finishes} for p, finishes in providers.items()}


_PRICES = {
    "uuid-a": _paper(
        tcgplayer={"normal": {"2026-06-25": 1.49, "2026-06-26": 1.5}, "foil": {"2026-06-26": 5.0}},
        cardkingdom={"normal": {"2026-06-26": 1.4}},
    ),
    "uuid-b": _paper(
        cardkingdom={"normal": {"2026-06-26": 2.0}},
        cardsphere={"normal": {"2026-06-26": 2.1}},
    ),
    "uuid-c": _paper(
        cardsphere={"normal": {"2026-06-26": 3.0}},
        cardmarket={"normal": {"2026-06-26": 9.99}},  # EUR — never displayed
    ),
}


def _patch_fetch(monkeypatch, identifiers, prices):
    # stream_identifiers yields (uuid, scryfall_id); the fixture is keyed the
    # other way (scryfall_id -> uuid), so flip it here.
    monkeypatch.setattr(
        price_ingest,
        "stream_identifiers",
        lambda: iter([(uuid, sid) for sid, uuid in identifiers.items()]),
    )
    monkeypatch.setattr(price_ingest, "stream_prices", lambda: iter(list(prices.items())))


def _seed_cards(db):
    for sid in ("sid-a", "sid-b", "sid-c", "sid-d"):
        db.add(Card(scryfall_id=sid, name=sid, set_code="tst", collector_number="1"))
    db.commit()


def _price_row(db, sid, finish="normal"):
    return (
        db.query(CardPrice).filter(CardPrice.scryfall_id == sid, CardPrice.finish == finish).first()
    )


def _card(db, sid):
    return db.query(Card).filter(Card.scryfall_id == sid).first()


# --- Tests ------------------------------------------------------------------


def test_join_populates_card_prices(db, monkeypatch):
    _seed_cards(db)
    _patch_fetch(monkeypatch, _IDENTIFIERS, _PRICES)

    stats = run_ingest(db)

    # 3 printings resolved to a uuid; sid-d unresolved.
    assert stats["printings"] == 3
    assert stats["unresolved"] == 1
    # card_prices rows were created via the scryfall-id -> uuid join.
    assert _price_row(db, "sid-a") is not None
    assert _price_row(db, "sid-a", "foil") is not None
    assert _price_row(db, "sid-d") is None  # never mapped


def test_provider_fallback_order(db, monkeypatch):
    _seed_cards(db)
    _patch_fetch(monkeypatch, _IDENTIFIERS, _PRICES)
    run_ingest(db)

    # A: tcgplayer present -> wins over cardkingdom; most-recent date chosen.
    assert _card(db, "sid-a").price_usd == "1.5"
    # B: no tcgplayer -> cardkingdom (not cardsphere).
    assert _card(db, "sid-b").price_usd == "2.0"
    # C: only cardsphere.
    assert _card(db, "sid-c").price_usd == "3.0"


def test_cardmarket_excluded(db, monkeypatch):
    _seed_cards(db)
    _patch_fetch(monkeypatch, _IDENTIFIERS, _PRICES)
    run_ingest(db)

    # C's only USD provider is cardsphere; the 9.99 EUR cardmarket value must
    # never surface and no column should hold it.
    row = _price_row(db, "sid-c")
    assert row.cardsphere_retail == "3.0"
    assert "9.99" not in (
        row.tcgplayer_retail or "",
        row.cardkingdom_retail or "",
        row.cardsphere_retail or "",
    )
    assert _card(db, "sid-c").price_usd == "3.0"


def test_finish_matching(db, monkeypatch):
    _seed_cards(db)
    _patch_fetch(monkeypatch, _IDENTIFIERS, _PRICES)
    run_ingest(db)

    card = _card(db, "sid-a")
    assert card.price_usd == "1.5"  # normal
    assert card.price_usd_foil == "5.0"  # foil resolved from its own finish
    assert card.price_usd_etched is None  # no etched data -> no price


def test_never_priced_renders_no_price(db, monkeypatch):
    _seed_cards(db)
    _patch_fetch(monkeypatch, _IDENTIFIERS, _PRICES)
    run_ingest(db)

    # sid-d never mapped -> no card_prices row, Card price stays None ("no price").
    assert _card(db, "sid-d").price_usd is None


def test_transient_miss_keeps_last_known(db, monkeypatch):
    _seed_cards(db)
    _patch_fetch(monkeypatch, _IDENTIFIERS, _PRICES)
    run_ingest(db)

    first_stamp = _price_row(db, "sid-a").price_updated_at
    assert first_stamp is not None

    # Re-ingest where A reports only cardmarket (no USD provider for any finish):
    # a transient miss. Last-known value is kept and the stamp does NOT advance.
    miss = {"uuid-a": _paper(cardmarket={"normal": {"2026-06-27": 7.0}})}
    _patch_fetch(monkeypatch, {"sid-a": "uuid-a"}, miss)
    run_ingest(db)

    row = _price_row(db, "sid-a")
    assert row.tcgplayer_retail == "1.5"  # kept, not wiped to NULL
    assert row.price_updated_at == first_stamp  # not refreshed -> surfaces staleness
    assert _card(db, "sid-a").price_usd == "1.5"


def test_manual_override_precedence_and_survival(db, monkeypatch):
    _seed_cards(db)
    _patch_fetch(monkeypatch, _IDENTIFIERS, _PRICES)
    run_ingest(db)

    # Override wins immediately over the resolved provider price.
    set_price_override(db, "sid-a", "normal", "99.00")
    assert _card(db, "sid-a").price_usd == "99.00"
    assert _price_row(db, "sid-a").manual_override == "99.00"

    # Re-ingest with a new provider value: the override survives and still wins.
    bumped = {"uuid-a": _paper(tcgplayer={"normal": {"2026-06-28": 1.75}})}
    _patch_fetch(monkeypatch, {"sid-a": "uuid-a"}, bumped)
    run_ingest(db)

    row = _price_row(db, "sid-a")
    assert row.manual_override == "99.00"
    assert row.tcgplayer_retail == "1.75"  # provider value still updated underneath
    assert _card(db, "sid-a").price_usd == "99.00"  # override still displayed

    # Clearing the override falls back to the provider chain.
    set_price_override(db, "sid-a", "normal", "")
    assert _price_row(db, "sid-a").manual_override is None
    assert _card(db, "sid-a").price_usd == "1.75"
