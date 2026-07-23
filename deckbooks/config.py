"""Runtime configuration — paths and the reused-cache location.

Everything server-specific lives HERE (or in env), never in the persisted
deckbook JSON: the JSON references cards by Scryfall UUID only, so it stays
portable and later-importable into Cartarch.
"""

from __future__ import annotations

import os
from pathlib import Path

# The prototype's own tree (data + assets live under here).
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# The Cartarch SQLite DB the prototype READS (deck contents + the scryfall_cards
# metadata cache). Read-only — the prototype never writes here. Defaults to the
# local dev DB; override with DECKBOOK_CARTARCH_DB for a different snapshot.
CARTARCH_DB = Path(
    os.getenv("DECKBOOK_CARTARCH_DB", str(BASE_DIR.parent / "dev-data" / "mana_archive.db"))
)

# Cartarch's self-hosted card-image mirror (issue #44) — the "existing local
# image cache" this prototype reuses instead of building its own. Same env var
# and default the app uses, so we hit the exact same mirror. Contract:
# {base}/{scryfall_id}[/back]/{size}.{ext}, ext=png only for size "png".
IMAGE_MIRROR_BASE_URL = os.getenv("IMAGE_MIRROR_BASE_URL", "https://img.cartarch.com")

# One deckbook per directory under data/. The prototype ships exactly one.
DECKBOOK_ID = "osha-violation"


def deckbook_dir(deckbook_id: str = DECKBOOK_ID) -> Path:
    return DATA_DIR / deckbook_id
