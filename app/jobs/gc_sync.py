"""#123 — Game Changers list sync: game_changer_cards vs Scryfall ``is:gamechanger``.

Scryfall mirrors the official Commander Game Changers list as the
``is:gamechanger`` search predicate. This job compares the local
``game_changer_cards`` table against it and, on --apply:

  - adds    → INSERT a new row stamped with a fresh date-real rules_version
              (today, ISO) and date_added; history never rewritten.
  - removes → set active = FALSE + date_removed on the existing row; its
              original rules_version stays (that IS the history).

Floors are never touched here: staleness is derived — a persisted estimate
whose rules_version differs from ``gc_list_version()`` is stale, and the
combo-refresh daemon re-floors it on its next pass (no Spellbook call).

Invokable as ``python -m app.jobs.gc_sync`` (report-only) or with ``--apply``
— the oracle_ingest standing-manual-invocation precedent; no CronJob in v1.
"""

from __future__ import annotations

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

# Scryfall REQUIRES a descriptive, non-default User-Agent (the v4.6.4 lesson —
# default agents are rejected with HTTP 400 generic_user_agent).
_HEADERS = {"User-Agent": "Cartarch/1.0 (+https://cartarch.com)", "Accept": "application/json"}
_SEARCH_URL = "https://api.scryfall.com/cards/search?q=is%3Agamechanger&unique=cards"


def fetch_gamechanger_names() -> list[str] | None:
    """Every card name Scryfall currently tags ``is:gamechanger``.

    None on any network/parse failure (report it, change nothing) — the same
    fail-closed contract as the Spellbook fetch.
    """
    names: list[str] = []
    url: str | None = _SEARCH_URL
    try:
        while url:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            names.extend(c["name"] for c in data.get("data", []) if c.get("name"))
            url = data.get("next_page") if data.get("has_more") else None
    except Exception as exc:  # noqa: BLE001 — report-and-stop, never partial-apply
        print(f"[gc-sync] Scryfall fetch failed: {exc!r}", flush=True)
        return None
    return sorted(set(names))


def diff_game_changers(session: Session, scryfall_names: list[str]) -> dict:
    """Pure comparison: the local ACTIVE list vs Scryfall's current list."""
    local = {
        r[0] for r in session.execute(text("SELECT card_name FROM game_changer_cards WHERE active"))
    }
    remote = set(scryfall_names)
    return {
        "adds": sorted(remote - local),
        "removes": sorted(local - remote),
        "unchanged": len(local & remote),
    }


def apply_game_changer_sync(session: Session, scryfall_names: list[str], *, today: str) -> dict:
    """Apply a detected delta, stamping a new date-real rules_version.

    ``today`` is the ISO date to stamp (passed in — deterministic for tests).
    No delta → no writes, no stamp. Never rewrites history: removals keep
    their original rules_version and gain date_removed; re-adds are new rows.
    """
    delta = diff_game_changers(session, scryfall_names)
    if not delta["adds"] and not delta["removes"]:
        return {**delta, "applied": False, "rules_version": None}

    for name in delta["adds"]:
        card_id = session.execute(
            text("SELECT id FROM cards WHERE name = :n LIMIT 1"), {"n": name}
        ).scalar()
        session.execute(
            text(
                "INSERT INTO game_changer_cards"
                " (card_id, card_name, source, date_added, active, rules_version)"
                " VALUES (:cid, :n, 'scryfall is:gamechanger sync', :d, :t, :v)"
            ),
            {"cid": card_id, "n": name, "d": today, "t": True, "v": today},
        )
    for name in delta["removes"]:
        session.execute(
            text(
                "UPDATE game_changer_cards SET active = :f, date_removed = :d"
                " WHERE card_name = :n AND active"
            ),
            {"f": False, "d": today, "n": name},
        )
    session.commit()
    return {**delta, "applied": True, "rules_version": today}


def main(apply: bool = False) -> None:
    from app.db import SessionLocal
    from app.timeutil import utc_now

    names = fetch_gamechanger_names()
    if names is None:
        raise SystemExit(1)
    print(f"[gc-sync] Scryfall currently lists {len(names)} Game Changers", flush=True)
    with SessionLocal() as session:
        delta = diff_game_changers(session, names)
        print(
            f"[gc-sync] adds={len(delta['adds'])} removes={len(delta['removes'])} "
            f"unchanged={delta['unchanged']}",
            flush=True,
        )
        for n in delta["adds"]:
            print(f"  + {n}", flush=True)
        for n in delta["removes"]:
            print(f"  - {n}", flush=True)
        if not apply:
            print("[gc-sync] report only — pass --apply to write", flush=True)
            return
        result = apply_game_changer_sync(session, names, today=utc_now().date().isoformat())
        if result["applied"]:
            print(
                f"[gc-sync] applied; new rules_version {result['rules_version']} — "
                "affected floors will re-evaluate on the daemon's next passes",
                flush=True,
            )
        else:
            print("[gc-sync] no delta — nothing written", flush=True)


if __name__ == "__main__":
    import sys

    main(apply="--apply" in sys.argv)
