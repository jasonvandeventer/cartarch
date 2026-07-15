"""#103 Phase A — combo-refresh daemon logic.

Walks every deck, fingerprints its played card list, and — only when the
fingerprint changed — fetches combos from CommanderSpellbook, persists them to
``deck_combos``, and recomputes + persists the deck's bracket estimate with the
combo signal restored (``estimate_bracket_v2(combos=...)`` has run with
``combos=None`` since v3.27.9).

Design invariants (design-of-record on #103):
  * The fingerprint diff IS the cache invalidation — no hooks in any deck-write
    path. The daemon re-fingerprints each pass; unchanged decks cost one local
    query and zero network.
  * Spellbook failure (``compute_deck_combos`` → None) persists NOTHING — the
    stale fingerprint stays, so the deck retries next pass. A genuinely empty
    result ({"included": []}) is persisted like any other.
  * Runs entirely off the request path (the v3.27.9 invariant).
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.bracket_v2_service import estimate_bracket_v2, gc_list_version, persist_estimate
from app.deck_service import compute_deck_combos, resolved_deck_rows
from app.models import Deck, DeckCombo
from app.timeutil import utc_now


def deck_combo_fingerprint(rows: list) -> str:
    """Hash of exactly what feeds the Spellbook POST: the deck's card names,
    split by commander role. Order-insensitive; quantity-insensitive (Spellbook
    takes a name list, so a 2nd copy can't change the result)."""
    commanders = sorted({r.card.name for r in rows if r.card and r.role == "commander"})
    main = sorted({r.card.name for r in rows if r.card and r.role != "commander"})
    blob = "\x00".join(commanders) + "\x01" + "\x00".join(main)
    return hashlib.sha256(blob.encode()).hexdigest()


def refresh_stale_deck_combos(session: Session, limit: int = 3) -> int:
    """One daemon pass: refresh up to ``limit`` decks whose fingerprint changed.

    Returns the number refreshed (0 = everything is fresh). Each refreshed deck
    costs one Spellbook POST + a local bracket recompute; a bracket failure is
    logged but never blocks the combo persist (combos are the primary product).
    """
    existing: dict[int, DeckCombo] = {dc.deck_id: dc for dc in session.query(DeckCombo).all()}
    decks = session.query(Deck).order_by(Deck.id.asc()).all()
    refreshed = 0
    # #123 — the GC-list version participates in staleness: an estimate whose
    # rules_version predates the current list stamp is stale even when the
    # decklist fingerprint is fresh. One query for the current stamp, one for
    # the per-deck estimate stamps (no per-deck queries).
    current_gc_version = gc_list_version(session)
    est_versions: dict[int, str] = {
        r[0]: r[1]
        for r in session.execute(text("SELECT deck_id, rules_version FROM deck_bracket_estimates"))
    }
    for deck in decks:
        if refreshed >= limit:
            break
        if not deck.storage_location_id:
            continue  # location-less deck has no rows to combo
        rows = resolved_deck_rows(session, deck, deck.user_id)
        fp = deck_combo_fingerprint(rows)
        row = existing.get(deck.id)
        if row is not None and row.fingerprint == fp:
            # Decklist fresh. If the GC list moved since this deck's estimate,
            # re-floor from the PERSISTED combos — zero network either way.
            est_version = est_versions.get(deck.id)
            if est_version is not None and est_version != current_gc_version:
                try:
                    combos = json.loads(row.payload)
                except ValueError:
                    combos = None
                try:
                    estimate = estimate_bracket_v2(session, deck, deck.user_id, combos=combos)
                    persist_estimate(session, deck.id, estimate)
                    refreshed += 1
                    print(
                        f"[combo-refresh] re-floored deck={deck.id} "
                        f"(GC list {est_version} -> {current_gc_version})",
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001 — daemon must keep walking
                    session.rollback()
                    print(f"[combo-refresh] GC re-floor failed deck={deck.id}: {exc}", flush=True)
            continue  # fresh — zero network

        combos = compute_deck_combos(rows)
        if combos is None:
            # Spellbook unreachable — persist nothing so this deck retries.
            print(f"[combo-refresh] fetch failed deck={deck.id}; will retry", flush=True)
            continue

        if row is None:
            row = DeckCombo(deck_id=deck.id, fingerprint=fp, payload=json.dumps(combos))
            session.add(row)
        else:
            row.fingerprint = fp
            row.payload = json.dumps(combos)
        row.computed_at = utc_now()
        session.commit()
        refreshed += 1

        # Bracket recompute with the combo signal restored. Best-effort: a
        # failure here never rolls back the combo persist above.
        try:
            estimate = estimate_bracket_v2(session, deck, deck.user_id, combos=combos)
            persist_estimate(session, deck.id, estimate)
        except Exception as exc:  # noqa: BLE001 — daemon must keep walking
            session.rollback()
            print(f"[combo-refresh] bracket recompute failed deck={deck.id}: {exc}", flush=True)
    return refreshed


def load_deck_combos(session: Session, deck_id: int) -> dict | None:
    """Read the persisted combo payload for a deck (None = never computed).
    The Phase B surfaces read this — never compute_deck_combos directly."""
    row = session.query(DeckCombo).filter(DeckCombo.deck_id == deck_id).first()
    if row is None:
        return None
    try:
        return json.loads(row.payload)
    except ValueError:
        return None


def deck_combo_status(session: Session, deck_id: int, rows: list) -> dict:
    """Phase B read seam with the honesty signal: the persisted combos plus
    whether they're STALE (deck's current fingerprint ≠ the persisted one —
    i.e. the deck changed and the daemon hasn't caught up yet). ``rows`` is the
    caller's already-loaded resolved deck rows (no extra row query).

    Returns ``{"combos": dict|None, "computed_at": datetime|None, "stale": bool}``;
    combos None = never computed (surfaces hide rather than show nothing-yet)."""
    row = session.query(DeckCombo).filter(DeckCombo.deck_id == deck_id).first()
    if row is None:
        return {"combos": None, "computed_at": None, "stale": True}
    try:
        payload = json.loads(row.payload)
    except ValueError:
        payload = None
    return {
        "combos": payload,
        "computed_at": row.computed_at,
        "stale": row.fingerprint != deck_combo_fingerprint(rows),
    }
