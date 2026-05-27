"""Service layer for the lightweight Token Inventory feature.

Tokens are gameplay accessories, not first-class collection items. Storage
location reuse is intentional; resort_collection / drawer-sorter logic does
NOT operate on token_inventory rows because the table is separate from
inventory_rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import DeckTokenRequirement, TokenInventory


def resolve_token_inventory_id_by_name(
    session: Session, user_id: int, token_name: str
) -> int | None:
    """Case-insensitive lookup of the user's TokenInventory by name.

    Returns the matching ``TokenInventory.id`` when the user owns a token
    with the given name (case-insensitive), else ``None``. Used by the
    v3.30.9 suggestion-side "+ Track" path so an auto-detected token that
    the user already catalogued on /tokens links its DeckTokenRequirement
    to that inventory row automatically; an auto-detected token the user
    doesn't yet own becomes a loose name-only requirement (null link), a
    first-class case the v3.30.8 docs already record.
    """
    name = (token_name or "").strip()
    if not name:
        return None
    row = (
        session.query(TokenInventory.id)
        .filter(
            TokenInventory.user_id == user_id,
            func.lower(TokenInventory.name) == name.lower(),
        )
        .first()
    )
    return row[0] if row else None


def deck_requirement_exists_for_name(session: Session, deck_id: int, token_name: str) -> bool:
    """Server-side idempotency guard for the v3.30.9 auto-add route.

    The suggestion list already excludes tracked tokens at render time, but
    a stale tab or double-click must NOT produce a duplicate row.
    Case-insensitive name compare against this deck's existing
    DeckTokenRequirement rows.
    """
    name = (token_name or "").strip()
    if not name:
        return False
    return (
        session.query(DeckTokenRequirement.id)
        .filter(
            DeckTokenRequirement.deck_id == deck_id,
            func.lower(DeckTokenRequirement.token_name) == name.lower(),
        )
        .first()
        is not None
    )


def list_tokens(
    session: Session,
    user_id: int,
    *,
    name_filter: str = "",
    subtype_filter: str = "",
    storage_location_id: int | None = None,
    double_sided_only: bool = False,
) -> list[TokenInventory]:
    q = (
        session.query(TokenInventory)
        .options(joinedload(TokenInventory.storage_location))
        .filter(TokenInventory.user_id == user_id)
    )
    if name_filter.strip():
        q = q.filter(TokenInventory.name.ilike(f"%{name_filter.strip()}%"))
    if subtype_filter.strip():
        q = q.filter(TokenInventory.subtype.ilike(f"%{subtype_filter.strip()}%"))
    if storage_location_id is not None:
        q = q.filter(TokenInventory.storage_location_id == storage_location_id)
    if double_sided_only:
        q = q.filter(TokenInventory.is_double_sided.is_(True))
    return q.order_by(TokenInventory.name.asc(), TokenInventory.subtype.asc()).all()


def get_token(session: Session, token_id: int, user_id: int) -> TokenInventory | None:
    return (
        session.query(TokenInventory)
        .filter(TokenInventory.id == token_id, TokenInventory.user_id == user_id)
        .first()
    )


def _infer_double_sided(
    is_double_sided: bool,
    back_set_code: str | None,
    back_collector_number: str | None,
) -> bool:
    """Coerce ``is_double_sided`` to True when back fields are populated.

    The new-token form has a "Double-sided" checkbox that users can forget to
    tick after filling in the back-face set + collector inputs. Without this
    coercion, ``_get_owned_token_map`` skips the back side (filter requires
    ``is_double_sided.is_(True)``) and the Sets page shows only the front
    face as owned. Inferring True from non-empty back fields closes that gap
    while leaving truly single-sided tokens (no back fields) unaffected.
    """
    if is_double_sided:
        return True
    return bool((back_set_code or "").strip() and (back_collector_number or "").strip())


def create_token(
    session: Session,
    *,
    user_id: int,
    name: str,
    quantity: int = 1,
    subtype: str | None = None,
    type_line: str | None = None,
    storage_location_id: int | None = None,
    image_url: str | None = None,
    is_double_sided: bool = False,
    back_name: str | None = None,
    back_image_url: str | None = None,
    back_set_code: str | None = None,
    back_collector_number: str | None = None,
    set_code: str | None = None,
    collector_number: str | None = None,
    scryfall_id: str | None = None,
    notes: str | None = None,
) -> TokenInventory:
    if not name.strip():
        raise ValueError("Token name is required")
    if quantity < 0:
        raise ValueError("Quantity cannot be negative")

    is_double_sided = _infer_double_sided(is_double_sided, back_set_code, back_collector_number)

    token = TokenInventory(
        user_id=user_id,
        name=name.strip(),
        type_line=type_line.strip() if type_line else None,
        subtype=subtype.strip() if subtype else None,
        quantity=quantity,
        set_code=set_code.strip() if set_code else None,
        collector_number=collector_number.strip() if collector_number else None,
        scryfall_id=scryfall_id.strip() if scryfall_id else None,
        image_url=image_url.strip() if image_url else None,
        is_double_sided=is_double_sided,
        back_name=back_name.strip() if back_name else None,
        back_image_url=back_image_url.strip() if back_image_url else None,
        back_set_code=back_set_code.strip() if back_set_code else None,
        back_collector_number=back_collector_number.strip() if back_collector_number else None,
        storage_location_id=storage_location_id,
        notes=notes.strip() if notes else None,
    )
    session.add(token)
    session.commit()
    return token


def update_token(
    session: Session,
    *,
    token_id: int,
    user_id: int,
    **fields,
) -> TokenInventory | None:
    token = get_token(session, token_id, user_id)
    if not token:
        return None
    for k, v in fields.items():
        if not hasattr(token, k):
            continue
        if isinstance(v, str):
            v = v.strip() or None
        setattr(token, k, v)
    # Apply the same DFC inference as create_token. Whether the caller passed
    # is_double_sided, back_set_code, and back_collector_number explicitly or
    # left them untouched, the row's final state should satisfy the invariant
    # "back fields populated → is_double_sided=True".
    token.is_double_sided = _infer_double_sided(
        bool(token.is_double_sided),
        token.back_set_code,
        token.back_collector_number,
    )
    token.updated_at = datetime.utcnow()
    session.commit()
    return token


def delete_token(session: Session, token_id: int, user_id: int) -> bool:
    token = get_token(session, token_id, user_id)
    if not token:
        return False
    session.delete(token)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# Deck token requirements
# ---------------------------------------------------------------------------


def list_deck_token_requirements(session: Session, deck_id: int) -> list[DeckTokenRequirement]:
    return (
        session.query(DeckTokenRequirement)
        .options(
            joinedload(DeckTokenRequirement.token_inventory).joinedload(
                TokenInventory.storage_location
            )
        )
        .filter(DeckTokenRequirement.deck_id == deck_id)
        .order_by(DeckTokenRequirement.token_name.asc())
        .all()
    )


def add_deck_token_requirement(
    session: Session,
    *,
    deck_id: int,
    token_name: str,
    quantity_needed: int = 1,
    token_inventory_id: int | None = None,
    notes: str | None = None,
) -> DeckTokenRequirement:
    if not token_name.strip():
        raise ValueError("Token name is required")
    if quantity_needed < 1:
        raise ValueError("Quantity needed must be at least 1")
    req = DeckTokenRequirement(
        deck_id=deck_id,
        token_name=token_name.strip(),
        quantity_needed=quantity_needed,
        token_inventory_id=token_inventory_id,
        notes=notes.strip() if notes else None,
    )
    session.add(req)
    session.commit()
    return req


def delete_deck_token_requirement(session: Session, req_id: int, deck_id: int) -> bool:
    req = (
        session.query(DeckTokenRequirement)
        .filter(
            DeckTokenRequirement.id == req_id,
            DeckTokenRequirement.deck_id == deck_id,
        )
        .first()
    )
    if not req:
        return False
    session.delete(req)
    session.commit()
    return True


def deck_token_status(session: Session, deck_id: int, user_id: int) -> list[dict]:
    """Return enriched requirement rows: needed/owned/location/status.

    For each DeckTokenRequirement, compute owned count by:
      - If token_inventory_id is set: that row's quantity (and its location).
      - Else: sum of TokenInventory.quantity across all rows for this user
        whose name (case-insensitive) matches token_name. Location is shown
        only if a single storage location holds them all.
    """
    reqs = list_deck_token_requirements(session, deck_id)
    out: list[dict] = []
    for r in reqs:
        owned = 0
        location_label: str | None = None

        if r.token_inventory_id and r.token_inventory:
            owned = r.token_inventory.quantity
            if r.token_inventory.storage_location:
                location_label = r.token_inventory.storage_location.name
        else:
            rows = (
                session.query(TokenInventory)
                .options(joinedload(TokenInventory.storage_location))
                .filter(
                    TokenInventory.user_id == user_id,
                    TokenInventory.name.ilike(r.token_name),
                )
                .all()
            )
            owned = sum(row.quantity for row in rows)
            locs = {row.storage_location.name for row in rows if row.storage_location is not None}
            if len(locs) == 1:
                location_label = next(iter(locs))
            elif len(locs) > 1:
                location_label = f"{len(locs)} locations"

        deficit = max(0, r.quantity_needed - owned)
        out.append(
            {
                "id": r.id,
                "token_name": r.token_name,
                "quantity_needed": r.quantity_needed,
                "owned": owned,
                "deficit": deficit,
                "location": location_label,
                "status": "ok" if deficit == 0 else f"missing {deficit}",
                "notes": r.notes,
                "linked_token_id": r.token_inventory_id,
            }
        )
    return out


def list_token_subtypes(session: Session, user_id: int) -> list[str]:
    """Return distinct non-empty subtype values for filter dropdowns."""
    rows = (
        session.query(TokenInventory.subtype)
        .filter(
            TokenInventory.user_id == user_id,
            TokenInventory.subtype.is_not(None),
            TokenInventory.subtype != "",
        )
        .distinct()
        .order_by(TokenInventory.subtype.asc())
        .all()
    )
    return [r[0] for r in rows]


def parse_bulk_token_lines(raw: str) -> list[dict]:
    """Parse a paste-list of tokens (single-sided OR DFC).

    Per line, whitespace-separated:
      2 fields:  set collector                                   (single, qty=default)
      3 fields:  set collector quantity                          (single)
      4 fields:  front_set front_# back_set back_#               (DFC, qty=default)
      5 fields:  front_set front_# back_set back_# quantity      (DFC)

    Blank lines and lines starting with `#` are ignored.

    Each result dict has either:
      - 'ok': True with parsed fields including 'is_dfc' bool
      - 'ok': False with 'error' message (raw line preserved for user feedback)
    """
    out: list[dict] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        n = len(parts)

        if n == 2:
            out.append(
                {
                    "ok": True,
                    "raw": raw_line,
                    "is_dfc": False,
                    "front_set": parts[0],
                    "front_collector": parts[1],
                    "quantity": None,
                }
            )
            continue
        if n == 3:
            try:
                qty = int(parts[2])
                if qty < 1:
                    raise ValueError
            except ValueError:
                out.append(
                    {
                        "ok": False,
                        "raw": raw_line,
                        "error": (
                            f"invalid quantity {parts[2]!r} (single-sided line is "
                            "set collector [qty])"
                        ),
                    }
                )
                continue
            out.append(
                {
                    "ok": True,
                    "raw": raw_line,
                    "is_dfc": False,
                    "front_set": parts[0],
                    "front_collector": parts[1],
                    "quantity": qty,
                }
            )
            continue
        if n == 4:
            out.append(
                {
                    "ok": True,
                    "raw": raw_line,
                    "is_dfc": True,
                    "front_set": parts[0],
                    "front_collector": parts[1],
                    "back_set": parts[2],
                    "back_collector": parts[3],
                    "quantity": None,
                }
            )
            continue
        if n == 5:
            try:
                qty = int(parts[4])
                if qty < 1:
                    raise ValueError
            except ValueError:
                out.append(
                    {
                        "ok": False,
                        "raw": raw_line,
                        "error": f"invalid quantity {parts[4]!r}",
                    }
                )
                continue
            out.append(
                {
                    "ok": True,
                    "raw": raw_line,
                    "is_dfc": True,
                    "front_set": parts[0],
                    "front_collector": parts[1],
                    "back_set": parts[2],
                    "back_collector": parts[3],
                    "quantity": qty,
                }
            )
            continue

        out.append(
            {
                "ok": False,
                "raw": raw_line,
                "error": f"expected 2/3/4/5 fields per line, got {n}",
            }
        )
    return out


# Back-compat alias — old name used while the parser was DFC-only.
parse_bulk_dfc_lines = parse_bulk_token_lines


def total_token_count(session: Session, user_id: int) -> int:
    return (
        session.query(func.coalesce(func.sum(TokenInventory.quantity), 0))
        .filter(TokenInventory.user_id == user_id)
        .scalar()
        or 0
    )
