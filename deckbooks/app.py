"""Local Deckbook web app — standalone FastAPI/Jinja2, MULTI-deckbook.

Run:  python -m deckbooks.app        (or: uvicorn deckbooks.app:app --reload)

A library index at `/` lists every deckbook; each book lives under `/{book}/…`
(cover, overview, collection, ledger, museum, card detail). Decoupled from the
production app — imports nothing from `app`, cannot touch prod. Images come from
Cartarch's mirror; metadata from the local scryfall_cards cache. The current book
is set per-request (deckbooks.context) so the read/write services stay simple.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from deckbooks import briefing, catalog, editing, services
from deckbooks.config import BASE_DIR
from deckbooks.context import use_book
from deckbooks.models import DECISION_STATUSES, ROLES, VALID_FINISHES
from deckbooks.repository import exists

app = FastAPI(title="Cartarch Deckbooks (prototype)")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["statuses"] = DECISION_STATUSES
templates.env.globals["finishes"] = VALID_FINISHES
templates.env.globals["roles"] = ROLES


def _known(book: str) -> bool:
    """A book is servable if it's in the catalog or already has data on disk."""
    return catalog.get_config(book) is not None or exists(book)


def _ctx(request: Request, book: str, active: str = "", **extra) -> dict:
    return {
        "request": request,
        "book": book,
        "deckbook": services.get_deckbook(),
        "active": active,
        **extra,
    }


def _enter(request: Request, book: str) -> HTMLResponse | None:
    """Set the current book and guard it. Returns a response (404 / needs-init)
    to return as-is, or None when the book is ready to render."""
    if not _known(book):
        return templates.TemplateResponse(
            "deckbook/no_book.html", {"request": request, "book": book}, status_code=404
        )
    use_book(book)
    if not exists(book):
        return templates.TemplateResponse(
            "deckbook/needs_init.html",
            {"request": request, "book": book, "deckbook": {}},
            status_code=503,
        )
    return None


@app.get("/", response_class=HTMLResponse)
def library(request: Request):
    books = []
    for bid in catalog.list_book_ids():
        cfg = catalog.get_config(bid)
        initialized = exists(bid)
        progress = None
        if initialized:
            use_book(bid)
            progress = services.progress()
        books.append(
            {
                "id": bid,
                "name": cfg["name"],
                "commanders": cfg["commander_names"],
                "subtitle": cfg["subtitle"],
                "initialized": initialized,
                "progress": progress,
            }
        )
    return templates.TemplateResponse("deckbook/library.html", {"request": request, "books": books})


@app.get("/{book}", response_class=HTMLResponse)
def cover(request: Request, book: str):
    return _enter(request, book) or templates.TemplateResponse(
        "deckbook/cover.html", _ctx(request, book, active="cover", commander=_commander_view())
    )


@app.get("/{book}/overview", response_class=HTMLResponse)
def overview(request: Request, book: str):
    return _enter(request, book) or templates.TemplateResponse(
        "deckbook/overview.html", _ctx(request, book, active="overview", **services.overview())
    )


@app.get("/{book}/collection", response_class=HTMLResponse)
def collection(request: Request, book: str):
    return _enter(request, book) or templates.TemplateResponse(
        "deckbook/gallery.html",
        _ctx(request, book, active="collection", chapters=services.chapters()),
    )


@app.get("/{book}/ledger", response_class=HTMLResponse)
def ledger(request: Request, book: str):
    return _enter(request, book) or templates.TemplateResponse(
        "deckbook/checklist.html", _ctx(request, book, active="ledger", **services.ledger())
    )


@app.get("/{book}/museum", response_class=HTMLResponse)
def museum(request: Request, book: str):
    return _enter(request, book) or templates.TemplateResponse(
        "deckbook/museum.html", _ctx(request, book, active="museum", **services.museum_wall())
    )


@app.get("/{book}/card/{deck_card_id}", response_class=HTMLResponse)
def card_detail(request: Request, book: str, deck_card_id: str):
    guard = _enter(request, book)
    if guard:
        return guard
    view = services.card_detail(deck_card_id)
    if view is None:
        return templates.TemplateResponse(
            "deckbook/not_found.html", _ctx(request, book, active="collection"), status_code=404
        )
    return templates.TemplateResponse(
        "deckbook/card_detail.html", _ctx(request, book, active="collection", card=view)
    )


@app.get("/{book}/card/{deck_card_id}/briefing", response_class=PlainTextResponse)
def card_briefing(book: str, deck_card_id: str):
    if not _known(book):
        return PlainTextResponse("No such deckbook.", status_code=404)
    use_book(book)
    text = briefing.card_briefing(deck_card_id)
    if text is None:
        return PlainTextResponse("Card not found in this deckbook.", status_code=404)
    return PlainTextResponse(text)


@app.get("/{book}/deck-briefing", response_class=PlainTextResponse)
def deck_briefing(book: str):
    """Whole-deck Destination briefing for ChatGPT (every card + notable printings)."""
    if not _known(book):
        return PlainTextResponse("No such deckbook.", status_code=404)
    use_book(book)
    return PlainTextResponse(briefing.deck_briefing())


@app.post("/{book}/import-destinations")
def import_destinations(book: str, picks: str = Form("")):
    """Apply ChatGPT's Destination picks in bulk (one line per card)."""
    if not _known(book) or not exists(book):
        return RedirectResponse("/", status_code=303)
    use_book(book)
    result = editing.apply_destinations(picks)
    return RedirectResponse(
        f"/{book}/overview?imported={result['applied']}&skipped={len(result['unmatched'])}",
        status_code=303,
    )


@app.post("/{book}/card/{deck_card_id}/decision")
async def edit_decision(request: Request, book: str, deck_card_id: str):
    """Apply a card-detail edit (plain form POST → PRG redirect)."""
    if not _known(book) or not exists(book):
        return RedirectResponse("/", status_code=303)
    use_book(book)
    form = dict(await request.form())
    try:
        editing.update_decision(deck_card_id, form)
    except editing.CardNotFound:
        return RedirectResponse(f"/{book}/collection", status_code=303)
    return RedirectResponse(f"/{book}/card/{deck_card_id}?saved=1", status_code=303)


@app.post("/{book}/card/{deck_card_id}/select-printing")
def select_printing(
    book: str,
    deck_card_id: str,
    scryfall_id: str = Form(...),
    finish: str = Form("normal"),
    role: str = Form("selected"),
):
    """One-click 'make this the current / destination printing' (finish-aware)."""
    if not _known(book) or not exists(book):
        return RedirectResponse("/", status_code=303)
    use_book(book)
    if role == "current":
        editing.update_decision(
            deck_card_id, {"current_scryfall_id": scryfall_id, "current_finish": finish}
        )
    else:
        key = "museum" if role == "museum" else "selected"
        editing.update_decision(
            deck_card_id, {f"{key}_scryfall_id": scryfall_id, f"{key}_finish": finish}
        )
    return RedirectResponse(f"/{book}/card/{deck_card_id}?saved=1", status_code=303)


def _commander_view() -> dict | None:
    for c in services.gallery():
        if c.get("role") == "Commander":
            return c
    return None


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8800)


if __name__ == "__main__":
    main()
