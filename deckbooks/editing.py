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

from deckbooks import image_resolver, repository
from deckbooks.context import get_book
from deckbooks.models import ROLES, VALID_FINISHES, normalize_status


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
        r for r in repository.load_revisions(get_book()) if r.get("deck_card_id") == deck_card_id
    ]
    return len(existing) + 1


def _log_revision(card: dict, change_type: str, reason: str, previous: dict | None) -> None:
    decision = card["decision"]
    repository.append_revision(
        get_book(),
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
    cards = repository.load_cards(get_book())
    card = _load_one(cards, deck_card_id)
    decision = card["decision"]
    acquisition = card["acquisition"]

    was_finalized = bool(decision.get("finalized"))
    prev_snapshot = {
        "status": decision["status"],
        "selected_scryfall_id": (decision.get("selected_printing") or {}).get("scryfall_id"),
    }

    if form.get("role") in ROLES:  # role lives on the card, not the decision
        card["role"] = form["role"]
    if "status" in form:
        decision["status"] = normalize_status(form.get("status"))
    if "verdict" in form:
        decision["verdict"] = (form.get("verdict") or "").strip() or None
    if "reasoning" in form:
        decision["reasoning"] = [
            line.strip() for line in (form.get("reasoning") or "").splitlines() if line.strip()
        ]

    # The CURRENT (physical) copy in the deck — what you actually own/ordered.
    # Distinct from the selected/museum decision. Changing the printing preserves
    # the existing finish unless a new one is given (people rarely change finish
    # when correcting which printing they hold).
    if form.get("current_scryfall_id"):
        existing_finish = (card.get("current_printing") or {}).get("finish") or "normal"
        card["current_printing"] = _printing(
            form.get("current_scryfall_id"), form.get("current_finish") or existing_finish
        )

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

    # Museum review states (custom OSHA proxy vs. no separate edition). Only
    # touched when the field is present, so a decision edit that omits them
    # leaves the state alone. "No separate edition" is exclusive — it means
    # nothing separate is wanted, so it clears any official pick + proxy flag.
    if "custom_proxy_candidate" in form:
        decision["custom_proxy_candidate"] = _truthy(form.get("custom_proxy_candidate"))
    if "no_museum_edition" in form:
        decision["no_museum_edition"] = _truthy(form.get("no_museum_edition"))
        if decision["no_museum_edition"]:
            decision["museum_printing"] = None
            decision["proxy_candidate"] = None
            decision["custom_proxy_candidate"] = False

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

    repository.save_cards(get_book(), cards)
    if change_type:
        _log_revision(card, change_type, form.get("reason", ""), prev_snapshot)
    return card


def apply_destinations(text: str) -> dict:
    """Bulk-apply ChatGPT's Destination picks. Each line:
    `Card Name | SET #collector | finish | reason` (finish/reason optional). The
    printing is resolved name-scoped by (set, collector) so a typo'd set can't
    select a different card; unmatched lines are reported, never guessed."""
    cards = repository.load_cards(get_book())
    by_name: dict[str, dict] = {}
    for c in cards:
        if c.get("status") != "removed":
            by_name.setdefault(c["card_name"].lower(), c)

    applied = 0
    unmatched: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "```")) or set(line) <= set("| -"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            unmatched.append({"line": raw, "why": "need 'Name | SET #collector'"})
            continue
        name, setcn = parts[0], parts[1]
        finish = parts[2].lower() if len(parts) > 2 and parts[2] else "normal"
        reason = parts[3] if len(parts) > 3 else ""
        card = by_name.get(name.lower())
        if card is None:
            unmatched.append({"line": raw, "why": f"no card named {name!r}"})
            continue
        toks = setcn.replace("#", " ").split()
        if len(toks) < 2:
            unmatched.append({"line": raw, "why": "expected 'SET #collector'"})
            continue
        sid = image_resolver.printing_id_by_set_collector(card["card_name"], toks[0], toks[1])
        if not sid:
            unmatched.append({"line": raw, "why": f"no {toks[0].upper()} #{toks[1]}"})
            continue
        if finish not in VALID_FINISHES:
            finish = "normal"
        card["decision"]["selected_printing"] = {"scryfall_id": sid, "finish": finish}
        if reason:
            card["decision"]["verdict"] = reason
        applied += 1

    repository.save_cards(get_book(), cards)
    return {"applied": applied, "unmatched": unmatched}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _parse_price(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return round(float(str(value).replace("$", "").strip()), 2)
    except (TypeError, ValueError):
        return None
