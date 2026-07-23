"""Local Deckbook web app — standalone FastAPI/Jinja2.

Run:  python -m deckbooks.app        (or: uvicorn deckbooks.app:app --reload)

Read-only in this milestone: cover, overview dashboard, gallery, checklist, card
detail (with the printing-comparison browser). Decoupled from the production app
— it imports nothing from `app` and cannot touch prod data. Images come from
Cartarch's existing mirror via the resolver; metadata from the local
scryfall_cards cache. If the deckbook hasn't been initialized, every page shows a
one-line "run init" prompt rather than crashing.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from deckbooks import services
from deckbooks.config import BASE_DIR, DECKBOOK_ID
from deckbooks.models import DECISION_STATUSES
from deckbooks.repository import exists

app = FastAPI(title="Cartarch Deckbooks (prototype)")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["statuses"] = DECISION_STATUSES


def _ctx(request: Request, **extra) -> dict:
    return {"request": request, "deckbook": services.get_deckbook(), **extra}


def _needs_init(request: Request) -> HTMLResponse | None:
    if exists(DECKBOOK_ID):
        return None
    return templates.TemplateResponse(
        "deckbook/needs_init.html", _ctx(request, deckbook={}), status_code=503
    )


@app.get("/", response_class=HTMLResponse)
def cover(request: Request):
    return _needs_init(request) or templates.TemplateResponse(
        "deckbook/cover.html", _ctx(request, commander=_commander_view())
    )


@app.get("/overview", response_class=HTMLResponse)
def overview(request: Request):
    return _needs_init(request) or templates.TemplateResponse(
        "deckbook/overview.html", _ctx(request, progress=services.progress())
    )


@app.get("/gallery", response_class=HTMLResponse)
def gallery(request: Request):
    return _needs_init(request) or templates.TemplateResponse(
        "deckbook/gallery.html", _ctx(request, cards=services.gallery())
    )


@app.get("/checklist", response_class=HTMLResponse)
def checklist(request: Request):
    return _needs_init(request) or templates.TemplateResponse(
        "deckbook/checklist.html", _ctx(request, cards=services.gallery())
    )


@app.get("/card/{deck_card_id}", response_class=HTMLResponse)
def card_detail(request: Request, deck_card_id: str):
    guard = _needs_init(request)
    if guard:
        return guard
    view = services.card_detail(deck_card_id)
    if view is None:
        return templates.TemplateResponse("deckbook/not_found.html", _ctx(request), status_code=404)
    return templates.TemplateResponse("deckbook/card_detail.html", _ctx(request, card=view))


def _commander_view() -> dict | None:
    for c in services.gallery():
        if c.get("role") == "Commander":
            return c
    return None


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8800)


if __name__ == "__main__":
    main()
