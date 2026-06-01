"""Unit tests for the Collection color-identity facet filter (v3.32.x).

Standalone runner (no pytest dependency — matches tests/test_share_service).
Invoke via:

    DATA_DIR=dev-data DEV_MODE=true python -m tests.test_color_facet

Pins the commander-legal "within" semantics: a card matches a color-pip
selection iff its ``color_identity`` is a SUBSET of the selected colors
(castable in a Commander deck of those colors, à la Scryfall ``id<=``).
This is the v3.32.x replacement for the prior "identity contains ALL
selected colors" (superset) rule. Covers:
  - mono / multi selections include only same-or-narrower identities
  - colorless cards (identity "") match ANY non-empty selection
  - NULL identity (not yet fetched) is excluded — can't be confirmed
  - "C" selected alone filters to colorless (incl. NULL); "C" alongside
    colors is a redundant no-op
"""

from __future__ import annotations

import itertools
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.inventory_service import apply_collection_facet_filters
from app.models import Card, InventoryRow

_scryfall_seq = itertools.count(1)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _make_row(session, name: str, color_identity: str | None, user_id: int = 1) -> str:
    """Create a placed InventoryRow for a card with the given identity.

    ``color_identity`` is space-separated WUBRG ("" = colorless,
    ``None`` = not yet fetched from Scryfall). Returns the card name so
    tests can assert on the result set by name.
    """
    card = Card(
        scryfall_id=f"sid-{next(_scryfall_seq)}",
        name=name,
        set_code="TST",
        collector_number=str(next(_scryfall_seq)),
        color_identity=color_identity,
    )
    session.add(card)
    session.flush()
    session.add(
        InventoryRow(
            card_id=card.id,
            user_id=user_id,
            quantity=1,
            finish="normal",
            is_pending=False,
        )
    )
    session.commit()
    return name


def _matches(session, facet_colors: str) -> set[str]:
    query = session.query(InventoryRow).join(Card)
    query = apply_collection_facet_filters(session, query, 1, facet_colors=facet_colors)
    return {row.card.name for row in query.all()}


def _seed(session) -> None:
    _make_row(session, "Mono-W", "W")
    _make_row(session, "Azorius", "U W")
    _make_row(session, "Mono-G", "G")
    _make_row(session, "Golgari", "B G")
    _make_row(session, "Sol Ring", "")  # colorless
    _make_row(session, "Unfetched", None)  # NULL identity


def _check(label: str, got: set[str], expected: set[str]) -> int:
    if got == expected:
        print(f"  [OK] {label}")
        return 0
    print(f"  [FAIL] {label}\n        expected {sorted(expected)}\n        got      {sorted(got)}")
    return 1


def test_mono_selection() -> int:
    """G selects mono-G + colorless; excludes anything with a non-G color."""
    s = _fresh_session()
    _seed(s)
    return _check("G → mono-G + colorless only", _matches(s, "G"), {"Mono-G", "Sol Ring"})


def test_multi_selection() -> int:
    """WU selects mono-W, Azorius, and colorless (all subsets of {W,U})."""
    s = _fresh_session()
    _seed(s)
    return _check("WU → W + WU + colorless", _matches(s, "WU"), {"Mono-W", "Azorius", "Sol Ring"})


def test_colorless_matches_any_selection() -> int:
    """A colorless card is a subset of every non-empty selection (Sol Ring
    is legal in a mono-G deck) — the key behavior change from the old rule."""
    s = _fresh_session()
    _seed(s)
    failed = 0
    failed += 0 if "Sol Ring" in _matches(s, "G") else 1
    failed += 0 if "Sol Ring" in _matches(s, "WUBRG") else 1
    if failed:
        print("  [FAIL] colorless card not matched by a color selection")
    else:
        print("  [OK] colorless card matches any color selection")
    return failed


def test_null_excluded() -> int:
    """NULL identity (not yet fetched) can't be confirmed → excluded from a
    color selection."""
    s = _fresh_session()
    _seed(s)
    got = _matches(s, "G")
    if "Unfetched" in got:
        print("  [FAIL] NULL-identity card leaked into a color selection")
        return 1
    print("  [OK] NULL-identity card excluded from a color selection")
    return 0


def test_c_alone_is_colorless() -> int:
    """C selected alone → colorless only (identity ""; NULL tolerated as
    'no colors known', preserving the prior C-pip behavior)."""
    s = _fresh_session()
    _seed(s)
    return _check("C alone → colorless + NULL", _matches(s, "C"), {"Sol Ring", "Unfetched"})


def test_c_with_colors_is_noop() -> int:
    """C alongside colors is redundant (colorless already matches a color
    selection) → GC behaves exactly like G; NULL stays excluded."""
    s = _fresh_session()
    _seed(s)
    return _check("GC == G (C no-op)", _matches(s, "GC"), {"Mono-G", "Sol Ring"})


def main() -> None:
    tests = [
        ("Mono selection", test_mono_selection),
        ("Multi selection", test_multi_selection),
        ("Colorless matches any selection", test_colorless_matches_any_selection),
        ("NULL identity excluded", test_null_excluded),
        ("C alone is colorless", test_c_alone_is_colorless),
        ("C with colors is a no-op", test_c_with_colors_is_noop),
    ]
    total_failed = 0
    for title, fn in tests:
        print(f"\n=== {title} ===")
        total_failed += fn()
    print("\n" + "=" * 60)
    if total_failed:
        print(f"TOTAL: {total_failed} failed")
        sys.exit(1)
    print("TOTAL: all passed")


if __name__ == "__main__":
    main()
