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
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from deckbooks import editing, services
from deckbooks.config import BASE_DIR, DECKBOOK_ID
from deckbooks.models import DECISION_STATUSES, VALID_FINISHES
from deckbooks.repository import exists

app = FastAPI(title="Cartarch Deckbooks (prototype)")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["statuses"] = DECISION_STATUSES
templates.env.globals["finishes"] = VALID_FINISHES


def _ctx(request: Request, active: str = "", **extra) -> dict:
    return {
        "request": request,
        "deckbook": services.get_deckbook(),
        "active": active,
        **extra,
    }


def _needs_init(request: Request) -> HTMLResponse | None:
    if exists(DECKBOOK_ID):
        return None
    return templates.TemplateResponse(
        "deckbook/needs_init.html", _ctx(request, deckbook={}), status_code=503
    )


@app.get("/", response_class=HTMLResponse)
def cover(request: Request):
    return _needs_init(request) or templates.TemplateResponse(
        "deckbook/cover.html", _ctx(request, active="cover", commander=_commander_view())
    )


@app.get("/overview", response_class=HTMLResponse)
def overview(request: Request):
    return _needs_init(request) or templates.TemplateResponse(
        "deckbook/overview.html", _ctx(request, active="overview", **services.overview())
    )


# "Collection" (was /gallery) and "Ledger" (was /checklist) — the museum-register
# naming the deck's identity calls for.
@app.get("/collection", response_class=HTMLResponse)
def collection(request: Request):
    return _needs_init(request) or templates.TemplateResponse(
        "deckbook/gallery.html", _ctx(request, active="collection", chapters=services.chapters())
    )


@app.get("/ledger", response_class=HTMLResponse)
def ledger(request: Request):
    return _needs_init(request) or templates.TemplateResponse(
        "deckbook/checklist.html", _ctx(request, active="ledger", cards=services.gallery())
    )


@app.get("/card/{deck_card_id}", response_class=HTMLResponse)
def card_detail(request: Request, deck_card_id: str):
    guard = _needs_init(request)
    if guard:
        return guard
    view = services.card_detail(deck_card_id)
    if view is None:
        return templates.TemplateResponse(
            "deckbook/not_found.html", _ctx(request, active="collection"), status_code=404
        )
    return templates.TemplateResponse(
        "deckbook/card_detail.html", _ctx(request, active="collection", card=view)
    )


@app.post("/card/{deck_card_id}/decision")
async def edit_decision(request: Request, deck_card_id: str):
    """Apply a card-detail edit. Plain HTML form POST (no framework), so the
    prototype works with JS off; redirects back to the card (PRG). Unknown
    fields are ignored; a bad value normalizes rather than erroring."""
    if not exists(DECKBOOK_ID):
        return RedirectResponse("/", status_code=303)
    form = dict(await request.form())
    try:
        editing.update_decision(deck_card_id, form)
    except editing.CardNotFound:
        return RedirectResponse("/gallery", status_code=303)
    return RedirectResponse(f"/card/{deck_card_id}?saved=1", status_code=303)


@app.post("/card/{deck_card_id}/select-printing")
def select_printing(
    deck_card_id: str,
    scryfall_id: str = Form(...),
    finish: str = Form("normal"),
    role: str = Form("selected"),
):
    """One-click 'make this the definitive / museum printing' from the candidate
    browser. role ∈ {selected, museum}."""
    if not exists(DECKBOOK_ID):
        return RedirectResponse("/", status_code=303)
    key = "museum" if role == "museum" else "selected"
    editing.update_decision(
        deck_card_id, {f"{key}_scryfall_id": scryfall_id, f"{key}_finish": finish}
    )
    return RedirectResponse(f"/card/{deck_card_id}?saved=1", status_code=303)


def _commander_view() -> dict | None:
    for c in services.gallery():
        if c.get("role") == "Commander":
            return c
    return None


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8800)


if __name__ == "__main__":
    main()
