"""Seed the OSHA Violation deckbook from local Cartarch data.

    python -m deckbooks.init_deck            # create if absent, else no-op
    python -m deckbooks.init_deck --refresh  # re-sync deck rows, PRESERVE decisions

Reads the deck contents + printing metadata from the local Cartarch SQLite DB
(config.CARTARCH_DB, read-only) — NOT the production DB, and no network. Keeps
each card's exact Scryfall UUID + finish (Section 8 primary-key rule), seeds
every card `pending`, applies the one finalized Bello decision, and seeds the
four-card research queue. Idempotent: --refresh adds/marks-removed deck rows but
never overwrites a finalized decision (Section 19/20).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sqlite3

from deckbooks import repository as repo
from deckbooks.config import CARTARCH_DB, DECKBOOK_ID
from deckbooks.models import SCHEMA_VERSION


def _today() -> str:
    """UTC date string (ruff DTZ: no naive date.today()); this is a one-shot CLI."""
    return _dt.datetime.now(tz=_dt.UTC).date().isoformat()


COMMANDER_NAME = "Bello, Bard of the Brambles"
DECK_NAME = "Bello, Bard of the Brambles"  # the local deck row is named for its commander

# The one finalized decision (concept PDF, Decision 001). UUIDs verified from
# the local scryfall_cards cache, not invented.
BELLO_DECK_COPY = "31e4b7a1-b377-49d2-a92e-4bcb0db35f16"  # BLC #1 foil
BELLO_MUSEUM = "a1b46777-bf87-4cbd-9e85-ab0be33f0362"  # BLC #101 raised-foil "Imagine: Critters"

# Research queue (Section 14). The first three are in the deck; Akroma's
# Memorial is "on order" (not yet a deck row), seeded from the catalog.
RESEARCH_NAMES = ("Arcane Signet", "Greater Good", "Mana Reflection")
AKROMA = "Akroma's Memorial"

# ── Role classification (Section 17) ────────────────────────────────────────
# The deck data carries NO roles, so derive a STARTING role from the type line +
# a few well-known names. This is role classification (allowed + editable), NOT
# printing-decision inference (forbidden). Everything falls back to a type-based
# bucket; the curator re-tags from the card detail page.
_RAMP_NAMES = {
    "Arcane Signet",
    "Sol Ring",
    "Fellwar Stone",
    "Mind Stone",
    "Thran Dynamo",
    "Gilded Lotus",
    "Hedron Archive",
    "Thought Vessel",
    "Cultivate",
    "Rampant Growth",
    "Explore",
    "Sakura-Tribe Elder",
    "Burnished Hart",
    "Llanowar Loamspeaker",
    "Lotus Cobra",
    "Sanctum Weaver",
    "Selvala, Heart of the Wilds",
    "Fellwar",
}
_DRAW_NAMES = {
    "Harmonize",
    "Greater Good",
    "Garruk's Uprising",
    "Sunbird's Invocation",
    "Rain of Riches",
    "Outpost Siege",
    "Garruk's Packleader",
    "Path of Discovery",
}
_INTERACTION_NAMES = {
    "Abrade",
    "Beast Within",
    "Chaos Warp",
    "Blasphemous Act",
    "Decimate",
    "Starstorm",
    "Spine of Ish Sah",
    "Echoing Assault",
    "Big Score",
}


def _derive_role(name: str, type_line: str | None, is_commander: bool) -> str:
    if is_commander:
        return "Commander"
    tl = (type_line or "").lower()
    if "land" in tl.split("—")[0]:
        return "Land"
    if name in _RAMP_NAMES:
        return "Ramp"
    if name in _DRAW_NAMES:
        return "Draw"
    if name in _INTERACTION_NAMES:
        return "Interaction"
    if "creature" in tl:
        return "Threat"
    return "Utility"


def _empty_decision() -> dict:
    return {
        "status": "pending",
        "finalized": False,
        "finalized_at": None,
        "selected_printing": None,
        "museum_printing": None,
        "proxy_candidate": None,
        "verdict": None,
        "reasoning": [],
        "custom_proxy_candidate": False,
        "no_museum_edition": False,
    }


def _empty_acquisition(owned: bool, installed: bool) -> dict:
    return {
        "target_owned": owned,
        "installed": installed,
        "source_recorded": False,
        "acquired_at": None,
        "source": None,
        "price_paid": None,
        "condition": None,
        "language": "en",
    }


def _fetch_deck_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.name AS name, c.scryfall_id AS sid, ir.finish AS finish,
               ir.quantity AS qty, ir.is_proxy AS is_proxy, sc.type_line AS type_line
        FROM inventory_rows ir
        JOIN decks d ON d.storage_location_id = ir.storage_location_id
        JOIN cards c ON c.id = ir.card_id
        LEFT JOIN scryfall_cards sc ON sc.scryfall_id = c.scryfall_id
        WHERE d.name = ?
        ORDER BY c.name
        """,
        (DECK_NAME,),
    ).fetchall()
    out = []
    for r in rows:
        is_commander = r["name"] == COMMANDER_NAME
        out.append(
            {
                "deck_card_id": r["sid"],  # scryfall_id is unique per deck row here
                "card_name": r["name"],
                "quantity": r["qty"],
                "role": _derive_role(r["name"], r["type_line"], is_commander),
                "current_printing": {"scryfall_id": r["sid"], "finish": r["finish"]},
                "status": "active",
                "decision": _empty_decision(),
                "acquisition": _empty_acquisition(owned=not r["is_proxy"], installed=True),
                "notes": [],
            }
        )
    return out


def _pick_akroma_printing(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT scryfall_id FROM scryfall_cards WHERE name = ? AND name NOT LIKE 'A-%' "
        "ORDER BY set_code LIMIT 1",
        (AKROMA,),
    ).fetchone()
    return row["scryfall_id"] if row else None


def _apply_bello_decision(cards: list[dict]) -> None:
    for card in cards:
        if card["card_name"] != COMMANDER_NAME:
            continue
        card["role"] = "Commander"
        card["decision"] = {
            "status": "keep",
            "finalized": True,
            "finalized_at": "2026-07-22",
            "selected_printing": {"scryfall_id": BELLO_DECK_COPY, "finish": "foil"},
            "museum_printing": {"scryfall_id": BELLO_MUSEUM, "finish": "foil"},
            "proxy_candidate": {"scryfall_id": BELLO_MUSEUM, "desired": True, "printed": False},
            "verdict": (
                "The standard foil remains the definitive deck copy. It presents Bello "
                "clearly, suits the deck's Bloomburrow identity, and leaves the collecting "
                "budget for upgrades that materially improve the finished object. The "
                "raised-foil Imagine treatment is acknowledged as an extraordinary "
                "collector printing, but at roughly five hundred dollars it belongs in the "
                "book as a museum piece and optional proxy rather than a required purchase."
            ),
            "reasoning": [
                "The owned foil already presents Bello clearly across the table.",
                "It fits the deck's warm, storied Bloomburrow visual identity.",
                "The premium raised foil does not improve gameplay.",
                "Its cost would consume budget better spent across the rest of the deck.",
            ],
        }
        card["acquisition"]["target_owned"] = True
        card["acquisition"]["installed"] = True
        return


def _seed_research_queue(cards: list[dict], conn: sqlite3.Connection) -> None:
    by_name = {c["card_name"]: c for c in cards}
    for name in RESEARCH_NAMES:
        if name in by_name:
            by_name[name]["decision"]["status"] = "research"
    # Akroma's Memorial — on order, not yet in the deck. Seed a research entry.
    if AKROMA not in by_name:
        sid = _pick_akroma_printing(conn)
        cards.append(
            {
                "deck_card_id": f"onorder:{AKROMA}",
                "card_name": AKROMA,
                "quantity": 1,
                "role": "Threat",
                "current_printing": {"scryfall_id": sid, "finish": "normal"} if sid else None,
                "status": "on_order",
                "decision": {**_empty_decision(), "status": "research"},
                "acquisition": _empty_acquisition(owned=False, installed=False),
                "notes": ["On order — not yet installed in the physical deck."],
            }
        )


def _revision(deck_card_id: str, card_name: str, status: str, selected: str | None) -> dict:
    # No wall-clock in the app-side scripts elsewhere; here a plain date is fine
    # (this is a one-shot CLI, not a resumable workflow).
    return {
        "deck_card_id": deck_card_id,
        "card_name": card_name,
        "revision": 1,
        "changed_at": _today(),
        "change_type": "decision_finalized",
        "previous": None,
        "current": {"status": status, "selected_scryfall_id": selected},
        "reason": "Initial Deckbook curation decision",
    }


def build_deckbook() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": DECKBOOK_ID,
        "name": "OSHA Violation",
        "commander_names": [COMMANDER_NAME],
        "subtitle": (
            "Animated relics. Ancient enchantments. Absolutely no regard for workplace safety."
        ),
        "edition": {
            "name": "First Edition",
            "revision": 1,
            "created_at": _today(),
            "updated_at": _today(),
        },
        "identity": {
            "mission": (
                "Bello awakens forgotten relics and ancient enchantments into enormous, "
                "indestructible attackers. Every permanent should feel at home in an "
                "overgrown magical workshop where abandoned curiosities have come alive."
            ),
            "pillars": [
                {
                    "name": "Enchanted Relics",
                    "description": "Artifacts should feel storied, strange, and recently awakened.",
                },
                {
                    "name": "Living Wilderness",
                    "description": "Forests, vines, wood, moss, and elemental magic unite the deck.",
                },
                {
                    "name": "Joyful Chaos",
                    "description": "The deck should feel whimsical even while producing absurd "
                    "combat steps.",
                },
            ],
            "palette": [
                "warm bronze",
                "weathered wood",
                "moss green",
                "parchment",
                "rune-light gold",
            ],
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    }


def initialize(*, refresh: bool) -> dict:
    """Create or refresh the deckbook. Returns a small summary dict."""
    if not CARTARCH_DB.exists():
        raise SystemExit(f"Cartarch DB not found: {CARTARCH_DB} (set DECKBOOK_CARTARCH_DB)")

    already = repo.exists(DECKBOOK_ID)
    if already and not refresh:
        return {"action": "noop", "reason": "already initialized; pass --refresh to re-sync"}

    conn = sqlite3.connect(f"file:{CARTARCH_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        deck_rows = _fetch_deck_rows(conn)
        if not deck_rows:
            raise SystemExit(f"No cards found for deck {DECK_NAME!r} in {CARTARCH_DB}")

        if refresh and already:
            cards = _merge_preserving_decisions(repo.load_cards(DECKBOOK_ID), deck_rows, conn)
            action = "refreshed"
        else:
            cards = deck_rows
            _apply_bello_decision(cards)
            _seed_research_queue(cards, conn)
            repo.save_deckbook(DECKBOOK_ID, build_deckbook())
            # Record the one finalized decision as revision 1.
            bello = next(c for c in cards if c["card_name"] == COMMANDER_NAME)
            repo.append_revision(
                DECKBOOK_ID,
                _revision(bello["deck_card_id"], COMMANDER_NAME, "keep", BELLO_DECK_COPY),
            )
            action = "created"

        repo.save_cards(DECKBOOK_ID, cards)
    finally:
        conn.close()

    finalized = sum(1 for c in cards if c["decision"]["finalized"])
    return {"action": action, "cards": len(cards), "finalized": finalized}


def _merge_preserving_decisions(
    existing: list[dict], deck_rows: list[dict], conn: sqlite3.Connection
) -> list[dict]:
    """Re-sync deck membership without touching curated decisions (Section 20).

    Existing cards keep their decision/acquisition. New deck rows are added
    pending. A previously-active card no longer in the deck is marked removed
    (never deleted — history is preserved). The Akroma on-order entry (not a
    deck row) is preserved as-is.
    """
    by_id = {c["deck_card_id"]: c for c in existing}
    live_ids = {r["deck_card_id"] for r in deck_rows}
    out: list[dict] = []
    for row in deck_rows:
        prior = by_id.get(row["deck_card_id"])
        if prior:
            # Keep decision/acquisition/notes/role; refresh the physical printing.
            prior["current_printing"] = row["current_printing"]
            prior["status"] = "active"
            out.append(prior)
        else:
            out.append(row)  # newly discovered deck card, pending
    # Carry forward non-deck entries (on-order) + mark vanished deck cards removed.
    for card in existing:
        if card["deck_card_id"] in live_ids:
            continue
        if card.get("status") == "on_order":
            out.append(card)
        else:
            card["status"] = "removed"
            out.append(card)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Initialize the OSHA Violation deckbook.")
    ap.add_argument(
        "--refresh", action="store_true", help="re-sync deck rows, preserving finalized decisions"
    )
    args = ap.parse_args()
    summary = initialize(refresh=args.refresh)
    print(f"[deckbook:init] {summary}")


if __name__ == "__main__":
    main()
