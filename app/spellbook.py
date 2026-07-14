import time

import requests

_CACHE: dict = {}
_CACHE_TTL = 3600
_COMBO_CACHE_VERSION = 1

_API_URL = "https://backend.commanderspellbook.com/find-my-combos/"


def fetch_deck_combos(main_names: list[str], commander_names: list[str]) -> dict | None:
    """POST card lists to CommanderSpellbook and return parsed included combos.

    #103 Phase A — returns ``None`` on any network/parse failure so the caller
    (the combo-refresh daemon) can distinguish "Spellbook was unreachable" from
    "this deck genuinely has no combos" and retry next pass instead of
    persisting a wrong empty result."""
    cache_key = (_COMBO_CACHE_VERSION, frozenset(main_names + commander_names))
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached["ts"] < _CACHE_TTL:
        return cached["data"]

    payload = {
        "commanders": [{"card": n} for n in commander_names],
        "main": [{"card": n} for n in main_names],
    }

    try:
        resp = requests.post(_API_URL, json=payload, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception:
        return None

    results = raw.get("results", {})
    deck_set = set(main_names + commander_names)

    included = [_parse_combo(c, deck_set) for c in results.get("included", [])]

    data = {"included": included}
    _CACHE[cache_key] = {"ts": time.time(), "data": data}
    return data


def _parse_combo(combo: dict, deck_set: set) -> dict:
    uses_names = [u["card"]["name"] for u in combo.get("uses", [])]
    produces = [p["feature"]["name"] for p in combo.get("produces", [])]
    return {
        "id": combo.get("id", ""),
        "card_names": uses_names,
        "owned": [n for n in uses_names if n in deck_set],
        "missing": [],
        "description": combo.get("description", "").strip(),
        "results": produces,
        "prerequisites": combo.get("easyPrerequisites", "").strip(),
        "mana_needed": combo.get("manaNeeded", ""),
        "popularity": combo.get("popularity", 0),
    }
