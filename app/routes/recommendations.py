"""Collection-aware deck recommendations (issue #51).

Commander picker -> generated Brew preview -> create-as-Brew. Deterministic,
local-data-only; the heavy lifting lives in ``app.recommendation_service``.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import deck_service
from app import recommendation_service as rec_service
from app.dependencies import (
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
)
from app.models import DeckStrategyProfile, User
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


# --- Deck analyzer (issue #60, P3) ----------------------------------------------


def _owned_deck_or_404(session: Session, deck_id: int, user_id: int):
    deck = deck_service.get_deck(session, deck_id=deck_id, user_id=user_id)
    if not deck:
        # get_deck is owner-scoped, so a non-owner sees 404 (no existence leak).
        raise HTTPException(status_code=404, detail="Deck not found")
    return deck


@router.get("/decks/{deck_id}/analysis")
def deck_analysis(
    request: Request,
    deck_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    deck = _owned_deck_or_404(session, deck_id, current_user.id)
    analysis = rec_service.analyze_deck(session, deck, current_user.id)
    session.commit()  # persist a first-visit auto-seeded profile
    profile_row = (
        session.query(DeckStrategyProfile).filter(DeckStrategyProfile.deck_id == deck.id).first()
    )
    return render(
        request,
        "recommendations/deck_analysis.html",
        {
            "title": f"Deck analysis — {deck.name}",
            "current_user": current_user,
            "deck": deck,
            "analysis": analysis,
            "is_custom_profile": bool(profile_row and profile_row.is_custom),
        },
    )


def _parse_tier(raw: str) -> list[str]:
    """Comma-separated role/subtype slugs; spaces normalize to underscores so
    'topdeck manipulation' and 'topdeck_manipulation' both work."""
    return [t.strip().lower().replace(" ", "_") for t in (raw or "").split(",") if t.strip()]


@router.post("/decks/{deck_id}/analysis/profile")
def deck_analysis_save_profile(
    request: Request,
    deck_id: int,
    high: str = Form(""),
    medium: str = Form(""),
    low: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    deck = _owned_deck_or_404(session, deck_id, current_user.id)
    profile_row = (
        session.query(DeckStrategyProfile).filter(DeckStrategyProfile.deck_id == deck.id).first()
    )
    if profile_row:
        profile = json.loads(profile_row.profile_data)
    else:
        # Form posted before any GET seeded a profile — seed now (keeps the
        # targets/identity), then apply the edit.
        profile = rec_service.analyze_deck(session, deck, current_user.id).profile
    profile["high"] = _parse_tier(high)
    profile["medium"] = _parse_tier(medium)
    profile["low"] = _parse_tier(low)
    rec_service.save_profile(session, deck.id, profile)
    session.commit()
    return RedirectResponse(url=f"/decks/{deck_id}/analysis", status_code=303)


@router.post("/decks/{deck_id}/analysis/profile/reset")
def deck_analysis_reset_profile(
    request: Request,
    deck_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    deck = _owned_deck_or_404(session, deck_id, current_user.id)
    rec_service.reset_profile(session, deck.id)
    session.commit()
    return RedirectResponse(url=f"/decks/{deck_id}/analysis", status_code=303)


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
