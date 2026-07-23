"""Card-image resolution — a thin adapter over Cartarch's existing mirror.

The revised handoff is explicit: do NOT build a second image cache. Cartarch
already serves every printing from a self-hosted mirror keyed by Scryfall UUID
(issue #44), and stores per-printing metadata (set, collector, image_url, faces)
in the `scryfall_cards` table (issue #83 threads scryfall_id through every
surface). This adapter reuses both:

  * images   → the mirror URL contract (build a URL; the browser fetches it,
               with a Scryfall API onerror fallback — exactly what the app does)
  * metadata → the local `scryfall_cards` cache (offline, no network)

So there is no download step, no local file tree, no rate limiting, no headers,
no hashing here — that infrastructure already exists in the app and is not
reconstructed. Deckbook records key on the Scryfall UUID; this maps a UUID to a
renderable URL at request time, so no server path is ever persisted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from deckbooks.config import CARTARCH_DB, IMAGE_MIRROR_BASE_URL

# Size preference per the handoff: large first, then normal.
DEFAULT_SIZE = "large"


def mirror_image_url(scryfall_id: str, size: str = DEFAULT_SIZE, face: str = "front") -> str:
    """The exact URL contract app/dependencies.py:mirror_image_url produces.

    Reused verbatim rather than imported, to keep the prototype decoupled from
    the app package's import side effects (app.db mkdir etc.); the contract is
    stable and documented in that module. face="back" appends the "/back"
    segment the mirror uses for the reverse of a double-faced card.
    """
    ext = "png" if size == "png" else "jpg"
    back = "/back" if face == "back" else ""
    return f"{IMAGE_MIRROR_BASE_URL}/{scryfall_id}{back}/{size}.{ext}"


def scryfall_api_fallback(scryfall_id: str, size: str = DEFAULT_SIZE, face: str = "front") -> str:
    """Scryfall's image redirect — the <img onerror> target for a printing the
    mirror hasn't ingested yet. Same fallback the app's img_fallback macro uses."""
    url = f"https://api.scryfall.com/cards/{scryfall_id}?format=image&version={size}"
    return url + "&face=back" if face == "back" else url


@dataclass(frozen=True)
class PrintingMeta:
    """The `scryfall_cards` fields a deckbook surface needs for a printing."""

    scryfall_id: str
    name: str
    set_code: str
    set_name: str | None
    collector_number: str
    rarity: str | None
    type_line: str | None
    image_url: str | None  # Scryfall CDN URL — the app's "has an image" signal
    layout: str | None  # transform/mdfc/split/adventure → whether a back face exists

    @property
    def has_back_face(self) -> bool:
        return (self.layout or "") in {
            "transform",
            "modal_dfc",
            "double_faced_token",
            "art_series",
        }


# Card layouts Scryfall renders as two physical faces (front + back image).
_META_COLS = (
    "scryfall_id, name, set_code, set_name, collector_number, rarity, type_line, image_url, layout"
)


def _row_to_meta(row: sqlite3.Row) -> PrintingMeta:
    return PrintingMeta(*[row[c] for c in _META_COLS.replace(" ", "").split(",")])


def _connect() -> sqlite3.Connection:
    # Read-only URI connection — the prototype must never write the app's DB.
    conn = sqlite3.connect(f"file:{CARTARCH_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_printing(scryfall_id: str) -> PrintingMeta | None:
    """One printing's metadata from the local cache, or None if not cached."""
    if not scryfall_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_META_COLS} FROM scryfall_cards WHERE scryfall_id = ?", (scryfall_id,)
        ).fetchone()
    return _row_to_meta(row) if row else None


def get_printings(scryfall_ids: list[str]) -> dict[str, PrintingMeta]:
    """Batched metadata lookup (used to hydrate a whole deckbook page at once)."""
    ids = [i for i in scryfall_ids if i]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_META_COLS} FROM scryfall_cards WHERE scryfall_id IN ({placeholders})", ids
        ).fetchall()
    return {r["scryfall_id"]: _row_to_meta(r) for r in rows}


def list_printings_for_name(name: str) -> list[PrintingMeta]:
    """Every cached printing of a card NAME — the candidate-comparison source.

    Ordered set_code then collector for a stable gallery; no network (the whole
    catalog is already in scryfall_cards). Excludes digital-only Alchemy
    rebalances ("A-…"), which are unpriceable/unbuyable in paper.
    """
    if not name:
        return []
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_META_COLS} FROM scryfall_cards "
            "WHERE name = ? AND name NOT LIKE 'A-%' "
            "ORDER BY set_code, collector_number",
            (name,),
        ).fetchall()
    return [_row_to_meta(r) for r in rows]
