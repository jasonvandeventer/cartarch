"""Byte-identical contract test for the v3.25.0 local Scryfall cache seam.

Pytest module (same pattern as
tests/test_deck_service.py). Invoke via:

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_scryfall_cache.py

The seam's correctness rests entirely on a cache-path value being
indistinguishable from an API-path value. The API path is exactly
`_normalize_card_payload(raw)` (the network only delivers `raw`); the cache
path is "store those normalized values in scryfall_cards, read them back,
reconstruct via `_cached_row_to_payload`". This test simulates the storage
round-trip with a real in-memory SQLite table (so INTEGER/REAL/NULL
coercion is genuinely exercised) and asserts:

  * full dict equality (got == expected),
  * identical key order / shape (the 22-key normalizer contract),
  * full_art is a Python bool, not the stored INTEGER,
  * the None-vs-"" contract: colorless card has colors=None AND
    color_identity="" (distinguishable), empty mana_cost stays "" not None,
  * legalities/frame_effects round-trip as verbatim JSON text.

No network, no app DB — pure functions plus a throwaway sqlite3 DB.
"""

from __future__ import annotations

import json
import sqlite3

from app.scryfall import (
    _CACHE_COLUMNS,
    _cached_row_to_payload,
    _normalize_card_payload,
    card_constructor_kwargs,
)

# Mirrors scripts/migrate_v3_25_0_scryfall_cards.py. Kept inline so the test
# is self-contained; test_columns_match_normalizer() guards drift between
# _CACHE_COLUMNS and the normalizer key order.
_DDL = """
CREATE TABLE scryfall_cards (
    scryfall_id      TEXT PRIMARY KEY,
    name             TEXT,
    set_code         TEXT,
    set_name         TEXT,
    collector_number TEXT,
    rarity           TEXT,
    image_url        TEXT,
    type_line        TEXT,
    oracle_text      TEXT,
    price_usd        TEXT,
    price_usd_foil   TEXT,
    price_usd_etched TEXT,
    colors           TEXT,
    color_identity   TEXT,
    mana_cost        TEXT,
    cmc              REAL,
    legalities       TEXT,
    full_art         INTEGER,
    frame_effects    TEXT,
    set_type         TEXT,
    layout           TEXT,
    produced_tokens  TEXT,
    loyalty          TEXT,
    defense          TEXT
)
"""

_COLS = [c.strip() for c in _CACHE_COLUMNS.split(",")]


# ---------------------------------------------------------------------------
# Raw Scryfall fixtures (shapes copied from real API payloads)
# ---------------------------------------------------------------------------

# 1. Normal single-face colored card.
RAW_NORMAL = {
    "id": "bolt-lea-161",
    "name": "Lightning Bolt",
    "set": "lea",
    "set_name": "Limited Edition Alpha",
    "collector_number": "161",
    "rarity": "common",
    "image_uris": {"small": "bolt-s", "normal": "bolt-n", "large": "bolt-l"},
    "type_line": "Instant",
    "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    "prices": {"usd": "1.50", "usd_foil": None, "usd_etched": None},
    "colors": ["R"],
    "color_identity": ["R"],
    "mana_cost": "{R}",
    "cmc": 1.0,
    "legalities": {"modern": "legal", "legacy": "legal", "commander": "legal"},
    "full_art": False,
    "frame_effects": [],
    "set_type": "core",
    "layout": "normal",
}

# 2. Multi-face MDFC: no top-level image_uris/oracle_text/type_line/mana_cost
#    -> exercises the normalizer's card_faces merge.
RAW_MULTIFACE = {
    "id": "valki-khm-364",
    "name": "Valki, God of Lies // Tibalt, Cosmic Impostor",
    "set": "khm",
    "set_name": "Kaldheim",
    "collector_number": "364",
    "rarity": "mythic",
    "prices": {"usd": "5.00", "usd_foil": "8.00", "usd_etched": None},
    "colors": ["B", "R"],
    "color_identity": ["B", "R"],
    "cmc": 2.0,
    "legalities": {"modern": "legal", "pioneer": "legal"},
    "full_art": False,
    "frame_effects": [],
    "set_type": "expansion",
    "layout": "modal_dfc",
    "card_faces": [
        {
            "oracle_text": "When Valki, God of Lies enters the battlefield, each opponent exiles a creature card from their hand.",
            "type_line": "Legendary Creature — God",
            "mana_cost": "{B}{B}",
            "image_uris": {"normal": "valki-n", "large": "valki-l"},
        },
        {
            "oracle_text": "Tibalt, Cosmic Impostor does devil things.",
            "type_line": "Legendary Planeswalker — Tibalt",
            "mana_cost": "{4}{B}{R}",
            "loyalty": "7",
            "image_uris": {"normal": "tibalt-n"},
        },
    ],
}

# 5. Single-faced planeswalker: top-level loyalty (the common PW case).
RAW_PLANESWALKER = {
    "id": "lili-dka-104",
    "name": "Liliana of the Veil",
    "set": "dka",
    "set_name": "Dark Ascension",
    "collector_number": "104",
    "rarity": "mythic",
    "image_uris": {"normal": "lili-n"},
    "type_line": "Legendary Planeswalker — Liliana",
    "oracle_text": "+1: Each player discards a card.",
    "prices": {"usd": "12.00", "usd_foil": None, "usd_etched": None},
    "colors": ["B"],
    "color_identity": ["B"],
    "mana_cost": "{1}{B}{B}",
    "cmc": 3.0,
    "legalities": {"modern": "legal", "commander": "legal"},
    "full_art": False,
    "frame_effects": [],
    "set_type": "expansion",
    "layout": "normal",
    "loyalty": "3",
}

# 6. Battle (front face carries `defense`) — the loyalty analogue, and the
#    defense face-fallback case (top-level type/oracle absent on the DFC).
RAW_BATTLE = {
    "id": "invasion-mom-30",
    "name": "Invasion of Zendikar // Awakened Skyclave",
    "set": "mom",
    "set_name": "March of the Machine",
    "collector_number": "30",
    "rarity": "uncommon",
    "prices": {"usd": "0.50", "usd_foil": "1.00", "usd_etched": None},
    "colors": ["G"],
    "color_identity": ["G"],
    "cmc": 3.0,
    "legalities": {"standard": "legal", "commander": "legal"},
    "full_art": False,
    "frame_effects": [],
    "set_type": "expansion",
    "layout": "transform",
    "card_faces": [
        {
            "oracle_text": "When Invasion of Zendikar enters, search your library for up to two basic land cards.",
            "type_line": "Battle — Siege",
            "mana_cost": "{2}{G}",
            "defense": "4",
            "image_uris": {"normal": "invasion-n"},
        },
        {
            "oracle_text": "Awakened Skyclave's power and toughness are each equal to the number of lands you control.",
            "type_line": "Creature — Elemental",
            "mana_cost": "",
            "image_uris": {"normal": "skyclave-n"},
        },
    ],
}

# 3. Rich legalities (restricted/banned/not_legal) + colorless: colors=[] ->
#    colors_str None, color_identity=[] -> "". The None-vs-"" contract card.
RAW_LEGALITIES_COLORLESS = {
    "id": "lotus-lea-232",
    "name": "Black Lotus",
    "set": "lea",
    "set_name": "Limited Edition Alpha",
    "collector_number": "232",
    "rarity": "rare",
    "image_uris": {"normal": "lotus-n"},
    "type_line": "Artifact",
    "oracle_text": "{T}, Sacrifice Black Lotus: Add three mana of any one color.",
    "prices": {"usd": None, "usd_foil": None, "usd_etched": None},
    "colors": [],
    "color_identity": [],
    "mana_cost": "{0}",
    "cmc": 0.0,
    "legalities": {
        "vintage": "restricted",
        "legacy": "banned",
        "commander": "banned",
        "modern": "not_legal",
        "standard": "not_legal",
    },
    "full_art": False,
    "frame_effects": [],
    "set_type": "core",
    "layout": "normal",
}

# 4. full_art True + non-empty frame_effects + image_uris missing 'normal'
#    (image fallback) + empty mana_cost (stays "" not None) + colors=[] but
#    color_identity=["G"] (the contrast to fixture 3's color_identity="").
RAW_FULLART = {
    "id": "forest-unh-140",
    "name": "Forest",
    "set": "unh",
    "set_name": "Unhinged",
    "collector_number": "140",
    "rarity": "common",
    "image_uris": {"large": "forest-l", "small": "forest-s"},
    "type_line": "Basic Land — Forest",
    "oracle_text": "({T}: Add {G}.)",
    "prices": {"usd": "0.25", "usd_foil": "1.00", "usd_etched": None},
    "colors": [],
    "color_identity": ["G"],
    "mana_cost": "",
    "cmc": 0.0,
    "legalities": {"commander": "legal"},
    "full_art": True,
    "frame_effects": ["extendedart"],
    "set_type": "funny",
    "layout": "normal",
}

FIXTURES = [
    ("normal single-face", RAW_NORMAL),
    ("multi-face MDFC", RAW_MULTIFACE),
    ("rich legalities + colorless", RAW_LEGALITIES_COLORLESS),
    ("full_art + frame_effects + image fallback", RAW_FULLART),
    ("single-face planeswalker (top-level loyalty)", RAW_PLANESWALKER),
    ("battle (front-face defense)", RAW_BATTLE),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _roundtrip(expected: dict) -> dict:
    """Store `expected` in a throwaway SQLite table and reconstruct it via
    the real _cached_row_to_payload — the genuine cache path including
    INTEGER/REAL/NULL storage coercion.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(_DDL)
        placeholders = ", ".join("?" for _ in _COLS)
        conn.execute(
            f"INSERT INTO scryfall_cards ({_CACHE_COLUMNS}) VALUES ({placeholders})",
            [expected[c] for c in _COLS],
        )
        conn.commit()
        row = conn.execute(f"SELECT {_CACHE_COLUMNS} FROM scryfall_cards").fetchone()
        return _cached_row_to_payload(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_columns_match_normalizer() -> tuple[int, int]:
    """_CACHE_COLUMNS must list exactly the normalizer's keys, in order —
    this is what makes the column-order guarantee real.
    """
    passed = failed = 0
    norm_keys = list(_normalize_card_payload(RAW_NORMAL).keys())
    if _COLS == norm_keys:
        print(f"  [OK] _CACHE_COLUMNS matches normalizer ({len(_COLS)} keys, in order)")
        passed += 1
    else:
        print(f"  [FAIL] column/normalizer mismatch:\n    cols={_COLS}\n    norm={norm_keys}")
        failed += 1
    if len(_COLS) == 24:
        print("  [OK] 24 columns (v3.36.1 added loyalty + defense as the 23rd + 24th)")
        passed += 1
    else:
        print(f"  [FAIL] expected 24 columns, got {len(_COLS)}")
        failed += 1
    assert failed == 0


def test_byte_identical() -> tuple[int, int]:
    passed = failed = 0
    for label, raw in FIXTURES:
        expected = _normalize_card_payload(raw)
        got = _roundtrip(expected)

        if got == expected:
            print(f"  [OK] {label}: cache-path == API-path")
            passed += 1
        else:
            diff = {k: (expected[k], got.get(k)) for k in expected if expected[k] != got.get(k)}
            print(f"  [FAIL] {label}: dicts differ at {diff}")
            failed += 1

        if list(got.keys()) == list(expected.keys()):
            print(f"  [OK] {label}: key order preserved")
            passed += 1
        else:
            print(f"  [FAIL] {label}: key order changed")
            failed += 1

        if isinstance(got["full_art"], bool) and got["full_art"] == expected["full_art"]:
            print(f"  [OK] {label}: full_art is bool ({got['full_art']!r})")
            passed += 1
        else:
            print(f"  [FAIL] {label}: full_art type/value wrong: {got['full_art']!r}")
            failed += 1
    assert failed == 0


def test_none_vs_empty_contract() -> tuple[int, int]:
    """colors=NULL must stay None and color_identity="" must stay "" — and
    the two must remain distinguishable through the round-trip.
    """
    passed = failed = 0

    exp_c = _normalize_card_payload(RAW_LEGALITIES_COLORLESS)
    got_c = _roundtrip(exp_c)
    if exp_c["colors"] is None and exp_c["color_identity"] == "":
        print("  [OK] normalizer: colorless -> colors=None, color_identity=''")
        passed += 1
    else:
        print(
            f"  [FAIL] normalizer colorless contract: {exp_c['colors']!r} / {exp_c['color_identity']!r}"
        )
        failed += 1
    if got_c["colors"] is None and got_c["color_identity"] == "":
        print("  [OK] cache: colors=None and color_identity='' distinguishable")
        passed += 1
    else:
        print(
            f"  [FAIL] cache colorless contract: {got_c['colors']!r} / {got_c['color_identity']!r}"
        )
        failed += 1

    exp_g = _normalize_card_payload(RAW_FULLART)
    got_g = _roundtrip(exp_g)
    if got_g["colors"] is None and got_g["color_identity"] == "G" and got_g["mana_cost"] == "":
        print(
            "  [OK] cache: colors=None vs color_identity='G' (non-empty), empty mana_cost stays ''"
        )
        passed += 1
    else:
        print(
            f"  [FAIL] contrast card: colors={got_g['colors']!r} "
            f"id={got_g['color_identity']!r} mana={got_g['mana_cost']!r}"
        )
        failed += 1
    assert failed == 0


def test_legalities_verbatim() -> tuple[int, int]:
    """legalities/frame_effects are returned as the stored JSON text,
    verbatim and JSON-loadable — same as the API path.
    """
    passed = failed = 0
    for label, raw in FIXTURES:
        expected = _normalize_card_payload(raw)
        got = _roundtrip(expected)
        ok = (
            isinstance(got["legalities"], str)
            and got["legalities"] == expected["legalities"]
            and json.loads(got["legalities"]) == (raw.get("legalities") or {})
            and isinstance(got["frame_effects"], str)
            and got["frame_effects"] == expected["frame_effects"]
            and json.loads(got["frame_effects"]) == (raw.get("frame_effects") or [])
        )
        if ok:
            print(f"  [OK] {label}: legalities/frame_effects verbatim JSON text")
            passed += 1
        else:
            print(
                f"  [FAIL] {label}: legalities={got['legalities']!r} "
                f"frame_effects={got['frame_effects']!r}"
            )
            failed += 1
    assert failed == 0


# invariant: architecture.md → "Card-constructor sites must use
# card_constructor_kwargs(payload)" (produced_tokens is the sole seam-key-
# without-a-Card-column; splatting the full payload is the v3.30.22 prod-500).
def test_card_construction_on_cache_miss() -> tuple[int, int]:
    """Build a real ``Card`` from a freshly-normalized payload via
    ``card_constructor_kwargs`` — the cache-MISS path the import / switch-
    printing flows take (``Card(**card_constructor_kwargs(payload))``).

    Smoke gates that pre-seed ``scryfall_cards`` only exercise the cache-HIT
    read path and never splat a payload into ``Card(**...)``; this is the
    v3.30.22 production-500 lesson (``produced_tokens`` in the 22-key payload
    is NOT a Card column). This test pins that:

      * the strip still works (construction does not raise), and
      * the v3.36.1 additions ``loyalty`` / ``defense`` ARE real Card columns
        — they survive the strip and land on the constructed Card.
    """
    from datetime import datetime

    from app.models import Card

    passed = failed = 0

    pw_payload = _normalize_card_payload(RAW_PLANESWALKER)
    battle_payload = _normalize_card_payload(RAW_BATTLE)

    # produced_tokens must be present in the payload but stripped before splat.
    if "produced_tokens" in pw_payload and "produced_tokens" not in card_constructor_kwargs(
        pw_payload
    ):
        print("  [OK] produced_tokens present in payload, stripped from Card kwargs")
        passed += 1
    else:
        print("  [FAIL] produced_tokens strip contract broken")
        failed += 1

    try:
        pw = Card(**card_constructor_kwargs(pw_payload), updated_at=datetime.utcnow())
        battle = Card(**card_constructor_kwargs(battle_payload), updated_at=datetime.utcnow())
    except TypeError as exc:
        raise AssertionError(f"Card(**payload) raised TypeError: {exc}") from exc

    checks = [
        ("planeswalker loyalty set on Card", pw.loyalty == "3"),
        ("planeswalker defense is None", pw.defense is None),
        ("battle defense set on Card (face fallback)", battle.defense == "4"),
        ("battle loyalty is None", battle.loyalty is None),
        ("Card built cleanly (no produced_tokens attr clash)", pw.name == "Liliana of the Veil"),
    ]
    for label, ok in checks:
        if ok:
            print(f"  [OK] {label}")
            passed += 1
        else:
            print(f"  [FAIL] {label}")
            failed += 1
    assert failed == 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
