"""Seed a deckbook from local Cartarch data.

    python -m deckbooks.init_deck                       # osha-violation (default)
    python -m deckbooks.init_deck sam-and-frodo         # a specific deckbook
    python -m deckbooks.init_deck --all                 # every catalog deckbook
    python -m deckbooks.init_deck sam-and-frodo --refresh

Which deckbooks exist (source deck name + identity) is the catalog
(deckbooks/catalog.py); any per-deck seed logic (a pre-made decision, a research
queue) is keyed by id in SEED_HOOKS below. Reads deck contents + printing
metadata from the local Cartarch SQLite DB (config.CARTARCH_DB, read-only) — NOT
the production DB, no network. Keeps each card's exact Scryfall UUID + finish,
seeds every card `pending`. Idempotent: --refresh re-syncs deck rows but never
overwrites a finalized decision.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sqlite3
from collections import defaultdict

from deckbooks import catalog
from deckbooks import repository as repo
from deckbooks.config import CARTARCH_DB
from deckbooks.models import SCHEMA_VERSION


def _today() -> str:
    """UTC date string (ruff DTZ: no naive date.today()); this is a one-shot CLI."""
    return _dt.datetime.now(tz=_dt.UTC).date().isoformat()


# ── OSHA Violation's pre-made seed (concept PDF, Decision 001) ──────────────
# UUIDs verified from
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


def _derive_role(name: str, type_line: str | None, commander_names: set[str]) -> str:
    if name in commander_names:
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


def _fetch_deck_rows(
    conn: sqlite3.Connection, deck_name: str, commander_names: set[str]
) -> list[dict]:
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
        (deck_name,),
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "deck_card_id": r["sid"],  # scryfall_id is unique per deck row here
                "card_name": r["name"],
                "quantity": r["qty"],
                "role": _derive_role(r["name"], r["type_line"], commander_names),
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


_BELLO = "Bello, Bard of the Brambles"


def _apply_bello_decision(cards: list[dict]) -> None:
    for card in cards:
        if card["card_name"] != _BELLO:
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


def _seed_osha(cards: list[dict], conn: sqlite3.Connection, deckbook_id: str) -> None:
    """OSHA Violation's pre-made seed: the finalized Bello decision + the
    research queue + the initial revision. Other deckbooks start blank."""
    _apply_bello_decision(cards)
    _seed_research_queue(cards, conn)
    bello = next((c for c in cards if c["card_name"] == _BELLO), None)
    if bello:
        repo.append_revision(
            deckbook_id, _revision(bello["deck_card_id"], _BELLO, "keep", BELLO_DECK_COPY)
        )


# Per-deckbook seed logic, keyed by id. A deckbook without an entry starts with
# every card `pending` (which is the norm — OSHA is the one with a pre-made seed).
SEED_HOOKS = {"osha-violation": _seed_osha}


def build_deckbook(deckbook_id: str, cfg: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": deckbook_id,
        "name": cfg["name"],
        "commander_names": list(cfg["commander_names"]),
        "subtitle": cfg["subtitle"],
        "edition": {
            "name": "First Edition",
            "revision": 1,
            "created_at": _today(),
            "updated_at": _today(),
        },
        "identity": cfg["identity"],
    }


def initialize(deckbook_id: str, *, refresh: bool) -> dict:
    """Create or refresh one deckbook (by catalog id). Returns a summary dict."""
    cfg = catalog.get_config(deckbook_id)
    if cfg is None:
        raise SystemExit(f"Unknown deckbook {deckbook_id!r} (see deckbooks/catalog.py)")
    if not CARTARCH_DB.exists():
        raise SystemExit(f"Cartarch DB not found: {CARTARCH_DB} (set DECKBOOK_CARTARCH_DB)")

    already = repo.exists(deckbook_id)
    if already and not refresh:
        return {
            "book": deckbook_id,
            "action": "noop",
            "reason": "already initialized; pass --refresh to re-sync",
        }

    commanders = set(cfg["commander_names"])
    conn = sqlite3.connect(f"file:{CARTARCH_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        deck_rows = _fetch_deck_rows(conn, cfg["deck_name"], commanders)
        if not deck_rows:
            raise SystemExit(f"No cards found for deck {cfg['deck_name']!r} in {CARTARCH_DB}")

        if refresh and already:
            cards = _merge_preserving_decisions(repo.load_cards(deckbook_id), deck_rows, conn)
            action = "refreshed"
        else:
            cards = deck_rows
            repo.save_deckbook(deckbook_id, build_deckbook(deckbook_id, cfg))
            hook = SEED_HOOKS.get(deckbook_id)
            if hook:
                hook(cards, conn, deckbook_id)
            action = "created"

        repo.save_cards(deckbook_id, cards)
    finally:
        conn.close()

    finalized = sum(1 for c in cards if c["decision"]["finalized"])
    return {"book": deckbook_id, "action": action, "cards": len(cards), "finalized": finalized}


def _merge_preserving_decisions(
    existing: list[dict], deck_rows: list[dict], conn: sqlite3.Connection
) -> list[dict]:
    """Re-sync deck membership without touching curated decisions (Section 20).

    Existing cards keep their decision/acquisition. New deck rows are added
    pending. A previously-active card no longer in the deck is marked removed
    (never deleted — history is preserved). The Akroma on-order entry (not a
    deck row) is preserved as-is.

    Name-fallback: when a card's physical printing changed (deck_card_id no
    longer matches) but it's unambiguously the SAME card — exactly one new,
    otherwise-unmatched deck row of that name — its curated DECISION (destination
    / museum / verdict / role) is grafted onto the new printing, since a
    Destination pick is a target independent of the printing currently held. The
    1:1 guard keeps duplicate-name rows (basic lands) out of it.
    """
    by_id = {c["deck_card_id"]: c for c in existing}
    live_ids = {r["deck_card_id"] for r in deck_rows}
    out: list[dict] = []
    new_by_name: dict[str, list[dict]] = defaultdict(list)
    for row in deck_rows:
        prior = by_id.get(row["deck_card_id"])
        if prior:
            # Keep decision/acquisition/notes/role; refresh the physical printing.
            prior["current_printing"] = row["current_printing"]
            prior["status"] = "active"
            out.append(prior)
        else:
            out.append(row)  # newly discovered deck card (pending, unless a carry lands below)
            new_by_name[row["card_name"]].append(row)

    def _curated(c: dict) -> bool:
        d = c.get("decision") or {}
        return bool(d.get("selected_printing") or d.get("museum_printing") or d.get("finalized"))

    # Carry forward non-deck entries (on-order) + mark vanished deck cards removed,
    # grafting a curated decision onto the same card's new printing when it's 1:1.
    for card in existing:
        if card["deck_card_id"] in live_ids:
            continue
        if card.get("status") == "on_order":
            out.append(card)
            continue
        cands = new_by_name.get(card["card_name"], [])
        if _curated(card) and len(cands) == 1 and not cands[0].get("_carried"):
            cands[0]["decision"] = card["decision"]
            cands[0]["role"] = card.get("role", cands[0].get("role"))
            cands[0]["_carried"] = True
        card["status"] = "removed"
        out.append(card)
    for r in out:
        r.pop("_carried", None)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Initialize a deckbook from local Cartarch data.")
    ap.add_argument(
        "deckbook", nargs="?", default="osha-violation", help="catalog id (default: osha-violation)"
    )
    ap.add_argument("--all", action="store_true", help="initialize every catalog deckbook")
    ap.add_argument(
        "--refresh", action="store_true", help="re-sync deck rows, preserving finalized decisions"
    )
    args = ap.parse_args()
    ids = catalog.list_book_ids() if args.all else [args.deckbook]
    for deckbook_id in ids:
        print(f"[deckbook:init] {initialize(deckbook_id, refresh=args.refresh)}")


if __name__ == "__main__":
    main()
