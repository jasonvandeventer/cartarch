"""Write side: mutate a card's decision/acquisition + log revisions.

All mutations go through here (the repository is dumb JSON I/O), so the two
load-bearing rules live in ONE place:

  * a card is found by deck_card_id, mutated, saved — never duplicated;
  * every FINALIZE (and every printing/status change to a finalized card)
    appends an immutable revision, so the history is append-only.

Kept separate from services.py (read side) so the read path stays obviously
side-effect-free.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from deckbooks import repository
from deckbooks.config import DECKBOOK_ID
from deckbooks.models import VALID_FINISHES, normalize_status


class CardNotFound(ValueError):
    pass


def _now_date() -> str:
    return _dt.datetime.now(tz=_dt.UTC).date().isoformat()


def _load_one(cards: list[dict], deck_card_id: str) -> dict:
    card = next((c for c in cards if c.get("deck_card_id") == deck_card_id), None)
    if card is None:
        raise CardNotFound(deck_card_id)
    return card


def _next_revision_no(deck_card_id: str) -> int:
    existing = [
        r for r in repository.load_revisions(DECKBOOK_ID) if r.get("deck_card_id") == deck_card_id
    ]
    return len(existing) + 1


def _log_revision(card: dict, change_type: str, reason: str, previous: dict | None) -> None:
    decision = card["decision"]
    repository.append_revision(
        DECKBOOK_ID,
        {
            "deck_card_id": card["deck_card_id"],
            "card_name": card["card_name"],
            "revision": _next_revision_no(card["deck_card_id"]),
            "changed_at": _now_date(),
            "change_type": change_type,
            "previous": previous,
            "current": {
                "status": decision["status"],
                "selected_scryfall_id": (decision.get("selected_printing") or {}).get(
                    "scryfall_id"
                ),
            },
            "reason": reason or f"{change_type.replace('_', ' ')}",
        },
    )


def _printing(scryfall_id: str | None, finish: str | None) -> dict | None:
    if not scryfall_id:
        return None
    finish = (finish or "normal").strip().lower()
    if finish not in VALID_FINISHES:
        finish = "normal"
    return {"scryfall_id": scryfall_id, "finish": finish}


# ── Public mutations ────────────────────────────────────────────────────────


def update_decision(deck_card_id: str, form: dict[str, Any]) -> dict:
    """Apply an edit from the card-detail form. `form` keys (all optional):
    status, verdict, reasoning (newline-joined text), selected_scryfall_id/finish,
    museum_scryfall_id/finish, proxy_desired, proxy_printed, target_owned,
    installed, source_recorded, source, price_paid, condition, finalize.
    Returns the updated card."""
    cards = repository.load_cards(DECKBOOK_ID)
    card = _load_one(cards, deck_card_id)
    decision = card["decision"]
    acquisition = card["acquisition"]

    was_finalized = bool(decision.get("finalized"))
    prev_snapshot = {
        "status": decision["status"],
        "selected_scryfall_id": (decision.get("selected_printing") or {}).get("scryfall_id"),
    }

    if "status" in form:
        decision["status"] = normalize_status(form.get("status"))
    if "verdict" in form:
        decision["verdict"] = (form.get("verdict") or "").strip() or None
    if "reasoning" in form:
        decision["reasoning"] = [
            line.strip() for line in (form.get("reasoning") or "").splitlines() if line.strip()
        ]

    if form.get("selected_scryfall_id") is not None:
        decision["selected_printing"] = _printing(
            form.get("selected_scryfall_id"), form.get("selected_finish")
        )
    if form.get("museum_scryfall_id") is not None:
        sid = form.get("museum_scryfall_id") or None
        decision["museum_printing"] = _printing(sid, form.get("museum_finish"))
        # A proxy candidate points at the museum printing by convention; keep it
        # in sync unless explicitly cleared below.
        if sid and not decision.get("proxy_candidate"):
            decision["proxy_candidate"] = {"scryfall_id": sid, "desired": False, "printed": False}
        if not sid:
            decision["proxy_candidate"] = None

    if decision.get("proxy_candidate"):
        decision["proxy_candidate"]["desired"] = _truthy(form.get("proxy_desired"))
        decision["proxy_candidate"]["printed"] = _truthy(form.get("proxy_printed"))

    # Acquisition checkboxes + provenance.
    for flag in ("target_owned", "installed", "source_recorded"):
        if flag in form:
            acquisition[flag] = _truthy(form.get(flag))
    for field in ("source", "condition"):
        if field in form:
            acquisition[field] = (form.get(field) or "").strip() or None
    if "price_paid" in form:
        acquisition["price_paid"] = _parse_price(form.get("price_paid"))

    # Finalize is explicit — it stamps the date and logs a revision. Editing a
    # card that's ALREADY finalized also logs (the historical record must show a
    # changed decision), but doesn't re-stamp finalized_at.
    finalize = _truthy(form.get("finalize"))
    change_type = None
    if finalize and not was_finalized:
        decision["finalized"] = True
        decision["finalized_at"] = _now_date()
        change_type = "decision_finalized"
    elif was_finalized:
        change_type = "decision_revised"

    repository.save_cards(DECKBOOK_ID, cards)
    if change_type:
        _log_revision(card, change_type, form.get("reason", ""), prev_snapshot)
    return card


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _parse_price(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return round(float(str(value).replace("$", "").strip()), 2)
    except (TypeError, ValueError):
        return None
