# Cartarch Deckbooks — local prototype

A premium digital collector's catalog for your Commander decks (a **library**
of deckbooks; ships with **OSHA Violation** and **Sam & Frodo**). It records, for every card, which
exact printing belongs in the definitive deck, why, whether it's owned/installed,
and whether another printing is a museum piece or proxy candidate — presented as
a book, not a CRUD dashboard.

Runs locally, stores state as human-readable JSON, and **reuses Cartarch's
existing infrastructure instead of rebuilding it**. It does not touch the
production database or the running app.

## Run it

```bash
# 1. Seed a deckbook from your local Cartarch data (read-only; no network).
python -m deckbooks.init_deck                 # osha-violation (default)
python -m deckbooks.init_deck sam-and-frodo   # a specific deckbook
python -m deckbooks.init_deck --all           # every catalog deckbook

# 2. Serve. A library index at / lists every deckbook; each lives at /{book}/…
python -m deckbooks.app          # http://127.0.0.1:8800
#   (or: uvicorn deckbooks.app:app --reload)

# Tests
python -m pytest deckbooks/tests
```

Adding a deckbook is (mostly) one entry in `deckbooks/catalog.py` — the source
deck's name in the local Cartarch DB plus its display identity — then
`init_deck <id>`.

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

## Current state

**Cover · overview dashboard · gallery · checklist · card detail** (with the
definitive-vs-museum comparison and the printing browser), **plus editing**:

- On any card, set status / verdict / reasoning, toggle the acquisition
  checkboxes (owned / installed / source / proxy), and **Finalize** — which
  stamps the date and appends a revision.
- Pick the definitive or museum printing straight from the comparison browser
  (a one-click form per printing).
- All edits are plain HTML form POSTs → `303` redirect (PRG), so it works with
  JS off. Progress metrics on the dashboard update from the stored decisions;
  edits survive a restart (they're in `decisions.json` / `revisions.json`).

Seed state: Bello finalized (BLC #1 foil deck copy; BLC #101 raised-foil
*Imagine: Critters* museum/proxy); Arcane Signet, Greater Good, Mana Reflection,
and Akroma's Memorial (on order) in the research queue; the other 83 `pending`.

**Deferred (not blocking):** collection-aware ownership, a shareable URL, PDF
export. See `INTEGRATION.md` for the Cartarch boundary — none change the data model.
