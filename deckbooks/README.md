# Cartarch Deckbooks — local prototype

A premium digital collector's catalog for a single Commander deck: **OSHA
Violation** (*Bello, Bard of the Brambles*). It records, for every card, which
exact printing belongs in the definitive deck, why, whether it's owned/installed,
and whether another printing is a museum piece or proxy candidate — presented as
a book, not a CRUD dashboard.

Runs locally, stores state as human-readable JSON, and **reuses Cartarch's
existing infrastructure instead of rebuilding it**. It does not touch the
production database or the running app.

## Run it

```bash
# 1. Seed the deckbook from your local Cartarch data (reads dev-data/mana_archive.db
#    read-only; no network, no writes to that DB).
python -m deckbooks.init_deck

# 2. Serve the book.
python -m deckbooks.app          # http://127.0.0.1:8800
#   (or: uvicorn deckbooks.app:app --reload)

# Tests
python -m pytest deckbooks/tests
```

`python -m deckbooks.init_deck --refresh` re-syncs deck membership (adds new
cards, marks removed ones) **without overwriting any finalized decision**.

## What it reuses (the whole point)

| Need | Reused from Cartarch |
|------|----------------------|
| Card images | the self-hosted **image mirror** (`img.cartarch.com/{scryfall_id}[/back]/{size}.jpg`), same URL contract as `app/dependencies.py:mirror_image_url`, with the Scryfall API `onerror` fallback. **No second cache, no downloads.** |
| Printing metadata | the local **`scryfall_cards`** table (set, collector, type, image_url, layout) — offline, no network |
| Deck contents | the deck row + `inventory_rows` in the local Cartarch DB |

Deckbook records key cards by **Scryfall printing UUID** only — never a server
image path — so the data stays portable and later-importable into Cartarch. The
image URL is resolved at request time by `deckbooks/image_resolver.py`.

## Layout

```
deckbooks/
├── config.py          # paths + the reused mirror/DB locations (all server-specific config)
├── models.py          # decision-status enum, role→category map, derived completion states
├── repository.py      # atomic JSON load/save (deckbook / decisions / revisions)
├── image_resolver.py  # the adapter over the mirror + scryfall_cards (no download infra)
├── services.py        # hydrate cards with images, DERIVE progress metrics
├── init_deck.py       # `python -m deckbooks.init_deck`
├── app.py             # standalone FastAPI/Jinja app
├── data/osha-violation/   # deckbook.json · decisions.json · revisions.json (committed, inspectable)
├── templates/ · static/   # the book UI
└── tests/             # foundation tests (kept OUT of the app's CI gate)
```

## Data model (portable)

Each card in `decisions.json` records `current_printing` + a `decision`
(`status`, `finalized`, `selected_printing`, `museum_printing`,
`proxy_candidate`, `verdict`, `reasoning`) + an `acquisition` block — all keyed
on Scryfall UUIDs. Progress metrics are **derived** from these records at read
time, never stored.

Decision statuses: `pending · research · keep · upgrade · proxy · museum ·
not_applicable`. Museum and proxy are separate flags, not statuses — a card can
be `keep` while also flagging a museum/proxy printing.

## Current state (this milestone)

Read-only volume: **cover · overview dashboard · gallery · checklist · card
detail** (with the definitive-vs-museum comparison and the printing browser).
Bello is finalized (BLC #1 foil deck copy; BLC #101 raised-foil *Imagine:
Critters* museum/proxy); Arcane Signet, Greater Good, Mana Reflection, and
Akroma's Memorial (on order) seed the research queue; the other 83 cards are
`pending`.

**Not yet built (next milestone):** editing (status / checkbox / printing
selection → finalize → revision). The data model, repository, and revision log
already support it; only the write routes + forms remain. See
`INTEGRATION.md` for the Cartarch-integration boundary.
