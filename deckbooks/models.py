"""Deckbook domain vocabulary + derived-state rules.

The persisted form is plain JSON (see repository.py) — deliberately no ORM and
no heavyweight dataclass graph for a local prototype (YAGNI). This module holds
the controlled vocabularies and the pure functions that DERIVE progress from a
card record, so "is this curated?" is defined once and never drifts between the
dashboard and the checklist.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1

# Section 11 — the small controlled decision enum. A card's PRIMARY status; the
# museum/proxy flags are separate booleans on the decision, not statuses.
DECISION_STATUSES = (
    "pending",  # no meaningful review yet
    "research",  # comparing candidates, not finalized
    "keep",  # current owned printing is the definitive copy
    "upgrade",  # a different authentic printing selected, to acquire
    "proxy",  # a printing desired as a playable substitute, authentic not worth it
    "museum",  # notable/documented, not an active deck-copy target
    "not_applicable",
)
DEFAULT_STATUS = "pending"

# The three finishes a printing can be recorded in (matches the app's
# inventory finish tokens). An unknown value normalizes to "normal".
VALID_FINISHES = ("normal", "foil", "etched")

# Section 17 — reuse Cartarch's role vocabulary shape. The deck data carries no
# per-card roles, so init derives a rough role from the type line (editable
# later); these are the curated CATEGORIES the dashboard rolls up into, matching
# the concept PDF's dashboard (Commander / Mana Base / Ramp / Draw / Interaction
# / Threats). "role" is the fine-grained tag; "category" is the dashboard bucket.
ROLES = (
    "Commander",
    "Land",
    "Ramp",
    "Draw",
    "Interaction",
    "Protection",
    "Recursion",
    "Utility",
    "Enabler",
    "Payoff",
    "Threat",
    "Finisher",
    "Other",
)

# role → dashboard category (the PDF's six curation-status rows).
ROLE_CATEGORY = {
    "Commander": "Commander",
    "Land": "Mana Base",
    "Ramp": "Ramp",
    "Draw": "Draw",
    "Interaction": "Interaction",
    "Protection": "Interaction",
    "Recursion": "Utility",
    "Utility": "Utility",
    "Enabler": "Utility",
    "Payoff": "Threats",
    "Threat": "Threats",
    "Finisher": "Threats",
    "Other": "Utility",
}
CATEGORY_ORDER = ("Commander", "Mana Base", "Ramp", "Draw", "Interaction", "Threats", "Utility")

# The Collection reads as chapters (#8). CHAPTER_SEQUENCE is the book's reading
# order (distinct from CATEGORY_ORDER, which is the dashboard order); each
# chapter opens with a divider carrying its flavor line — in the deck's voice.
CHAPTER_SEQUENCE = ("Commander", "Ramp", "Draw", "Interaction", "Threats", "Utility", "Mana Base")
# Deck-agnostic fallback flavor. A book supplies its own themed lines via
# identity.chapter_flavor (catalog); these are the neutral defaults for any
# chapter a book doesn't override.
CHAPTER_FLAVOR = {
    "Commander": "The heart of the deck.",
    "Ramp": "The engine that fuels everything else.",
    "Draw": "Where the deck refills its hand.",
    "Interaction": "Answers held in reserve.",
    "Threats": "How the game actually ends.",
    "Utility": "The pieces that make it all hum.",
    "Mana Base": "The ground it all stands on.",
}
ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X")


def normalize_status(raw: str | None) -> str:
    """Coerce an untrusted status to the enum (unknown → default), mirroring the
    app's normalize_* posture (a bad value never blocks a write)."""
    value = (raw or "").strip().lower()
    return value if value in DECISION_STATUSES else DEFAULT_STATUS


def category_for_role(role: str | None) -> str:
    return ROLE_CATEGORY.get(role or "Other", "Utility")


# ── Derived completion states (Section 12) ──────────────────────────────────
# Pure functions over one card record dict. The optional museum/proxy goals
# deliberately do NOT gate the primary deck-completion metrics.


def curation_complete(card: dict[str, Any]) -> bool:
    """The printing decision is finalized — the primary curation signal."""
    return bool(card.get("decision", {}).get("finalized"))


def deck_copy_complete(card: dict[str, Any]) -> bool:
    """Finalized AND the selected printing is owned AND installed in the deck."""
    acq = card.get("acquisition", {})
    return curation_complete(card) and bool(acq.get("target_owned")) and bool(acq.get("installed"))


def fully_documented(card: dict[str, Any]) -> bool:
    """Deck-copy complete AND the acquisition provenance was recorded."""
    acq = card.get("acquisition", {})
    return deck_copy_complete(card) and bool(acq.get("source_recorded"))


def is_upgrade_target(card: dict[str, Any]) -> bool:
    return card.get("decision", {}).get("status") == "upgrade"


def is_proxy_candidate(card: dict[str, Any]) -> bool:
    # `or {}` because an empty decision stores these keys as None (present but
    # null), so a bare .get(key, {}) returns None, not the default.
    return bool((card.get("decision", {}).get("proxy_candidate") or {}).get("desired"))


def has_museum_piece(card: dict[str, Any]) -> bool:
    return bool((card.get("decision", {}).get("museum_printing") or {}).get("scryfall_id"))


def is_custom_proxy(card: dict[str, Any]) -> bool:
    """Reviewed and selected for a bespoke OSHA Violation custom proxy (distinct
    from proxying an official printing)."""
    return bool(card.get("decision", {}).get("custom_proxy_candidate"))


def is_no_museum_edition(card: dict[str, Any]) -> bool:
    """Reviewed; the Definitive printing is sufficient — no separate Museum
    edition or custom proxy is wanted."""
    return bool(card.get("decision", {}).get("no_museum_edition"))


# The Museum-page state for a card. Only "chosen" counts toward the Museum
# total; a card may be BOTH chosen and a custom-proxy candidate (the official
# image still leads, with an extra badge — see is_custom_proxy). The reviewed
# states are NOT "awaiting": "awaiting" means no Museum decision has been made.
def museum_state(card: dict[str, Any]) -> str:
    if has_museum_piece(card):
        return "chosen"
    if is_custom_proxy(card):
        return "custom_proxy"
    if is_no_museum_edition(card):
        return "no_edition"
    return "awaiting"


MUSEUM_STATE_RANK = {"chosen": 0, "custom_proxy": 1, "no_edition": 2, "awaiting": 3}
