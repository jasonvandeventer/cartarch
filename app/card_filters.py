"""Colour + card-type filter facets — ONE definition, every surface (v4.12.1).

The wishlist and trade surfaces all filter the SAME two ways (by colour and by
card type), and they do it CLIENT-side: the trade picker must not reload (its
selection lives only in the page's JS Maps), and the wishlist lists are bounded
and already fully rendered. So the semantics live here, in Python, and are
emitted as ``data-colors`` / ``data-types`` attributes that ``list-filter.js``
compares — the same split the sort control uses (values server-side, comparison
in JS), so no filter rule is ever written twice.

**Colour semantics — deliberately NOT the Collection facet's rule.** The
Collection pip filter uses SUBSET ("castable in a deck of these colours", à la
Scryfall ``id<=``), which makes colourless Sol Ring match a Green selection.
That is right for a multi-pip deck-building facet and wrong for a single-choice
dropdown labelled "Green", where a user means *cards that are green*. So this
is CONTAINS-this-colour, with explicit Colourless and Multicolour choices, and
it reads off ``color_identity`` (the field the rest of the app filters on).

Both helpers read only persisted ``Card`` columns — no Scryfall, request-path
safe (see the request-path network invariant).
"""

from __future__ import annotations

# (value, label). The empty value is "no filter" and every consumer renders it
# first; the letters match the WUBRG convention used throughout the codebase.
COLOR_FILTER_OPTIONS: list[tuple[str, str]] = [
    ("", "Any colour"),
    ("W", "White"),
    ("U", "Blue"),
    ("B", "Black"),
    ("R", "Red"),
    ("G", "Green"),
    ("C", "Colourless"),
    ("M", "Multicolour"),
]

# The eight card types a player actually filters by. Supertypes (Legendary,
# Basic, Snow) and subtypes (Goblin, Equipment) are deliberately absent — they
# multiply the list without helping someone scan a wishlist.
TYPE_FILTER_OPTIONS: list[tuple[str, str]] = [
    ("", "Any type"),
    ("creature", "Creature"),
    ("instant", "Instant"),
    ("sorcery", "Sorcery"),
    ("artifact", "Artifact"),
    ("enchantment", "Enchantment"),
    ("land", "Land"),
    ("planeswalker", "Planeswalker"),
    ("battle", "Battle"),
]

_TYPE_WORDS = frozenset(value for value, _label in TYPE_FILTER_OPTIONS if value)


def color_filter_token(color_identity: str | None) -> str:
    """WUBRG letters of a card's colour identity, as a compact sorted string.

    ``"U B"`` / ``"BU"`` both normalize to ``"BU"``; colourless (or an unfetched
    NULL identity) is ``""``. The JS reads it as: a letter choice matches when
    the token CONTAINS that letter, "C" matches the empty token, and "M"
    matches a token longer than one letter.
    """
    letters = {ch for ch in (color_identity or "").upper() if ch in "WUBRG"}
    return "".join(sorted(letters))


def type_filter_token(type_line: str | None) -> str:
    """Space-separated card-type words from a type line, lowercased.

    Reads only the portion BEFORE the em dash — subtypes follow it — matching
    ``inventory_service.is_oversized_card``'s convention, and keeps every type
    word so an "Artifact Creature" matches both filters. Unrecognized words
    (supertypes, oddities) are dropped, so the attribute stays a small closed
    vocabulary the JS can compare exactly.
    """
    head = (type_line or "").lower().split("—")[0]
    return " ".join(word for word in head.split() if word in _TYPE_WORDS)


def card_filter_tokens(card) -> tuple[str, str]:
    """``(colors, types)`` for any card-like object (ORM Card or a sanitized
    projection — both expose ``color_identity`` and ``type_line``). A missing
    card yields two empty strings, which read as "no colour, no type" and are
    simply never matched by an active filter."""
    if card is None:
        return "", ""
    return (
        color_filter_token(getattr(card, "color_identity", None)),
        type_filter_token(getattr(card, "type_line", None)),
    )
