"""Collection-aware deck recommendations (issue #51).

Commander picker -> generated Brew preview -> create-as-Brew. Deterministic,
local-data-only; the heavy lifting lives in ``app.recommendation_service``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import recommendation_service as rec_service
from app.dependencies import (
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
)
from app.models import User
from app.recommendation_service import DeckBuildIntent

router = APIRouter()


@router.get("/recommendations/commander")
def commander_picker(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    candidates = rec_service.list_commander_candidates(session, current_user.id)
    return render(
        request,
        "recommendations/commander_picker.html",
        {
            "title": "Brew a deck",
            "current_user": current_user,
            "candidates": candidates,
        },
    )


def _intent_from_query(
    card_id: int,
    allow_proxies: bool,
    use_cards_in_other_decks: bool,
    primary_theme: str,
    avoid_themes: str,
) -> DeckBuildIntent:
    avoid = {t.strip() for t in (avoid_themes or "").split(",") if t.strip()}
    return DeckBuildIntent(
        commander_card_id=card_id,
        primary_theme=(primary_theme or None),
        avoid_themes=avoid,
        allow_proxies=allow_proxies,
        use_cards_in_other_decks=use_cards_in_other_decks,
    )


@router.get("/recommendations/commander/{card_id}/preview")
def commander_preview(
    request: Request,
    card_id: int,
    allow_proxies: bool = Query(False),
    use_cards_in_other_decks: bool = Query(False),
    primary_theme: str = Query(""),
    avoid_themes: str = Query(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    intent = _intent_from_query(
        card_id, allow_proxies, use_cards_in_other_decks, primary_theme, avoid_themes
    )
    rec = rec_service.generate_recommendation(session, current_user.id, intent)
    return render(
        request,
        "recommendations/preview.html",
        {
            "title": "Brew preview",
            "current_user": current_user,
            "rec": rec,
            "intent": intent,
            "card_id": card_id,
        },
    )


@router.post("/recommendations/commander/{card_id}/create-brew")
def commander_create_brew(
    request: Request,
    card_id: int,
    deck_name: str = Form(""),
    allow_proxies: bool = Form(False),
    use_cards_in_other_decks: bool = Form(False),
    primary_theme: str = Form(""),
    avoid_themes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    intent = _intent_from_query(
        card_id, allow_proxies, use_cards_in_other_decks, primary_theme, avoid_themes
    )
    rec = rec_service.generate_recommendation(session, current_user.id, intent)
    if not rec.mainboard:
        # validation failed hard (e.g. ineligible commander) — bounce back to
        # the preview, which shows the warnings
        return RedirectResponse(
            url=f"/recommendations/commander/{card_id}/preview", status_code=303
        )

    name = (deck_name or "").strip() or _default_brew_name(session, current_user.id, rec)
    name = _unique_deck_name(session, current_user.id, name)
    deck = rec_service.create_brew_from_recommendation(session, current_user.id, rec, name)
    return RedirectResponse(url=f"/decks/{deck.id}?created=brew", status_code=303)


def _default_brew_name(session: Session, user_id: int, rec) -> str:
    base = (rec.commander.name if rec.commander else "Brew") or "Brew"
    return f"{base} (Brew)"


def _unique_deck_name(session: Session, user_id: int, name: str) -> str:
    from app.models import Deck

    existing = {d.name for d in session.query(Deck.name).filter(Deck.user_id == user_id).all()}
    if name not in existing:
        return name
    i = 2
    while f"{name} {i}" in existing:
        i += 1
    return f"{name} {i}"
