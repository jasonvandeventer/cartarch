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
    set_code: str | None = None,
    collector_number: str | None = None,
    scryfall_id: str | None = None,
    notes: str | None = None,
) -> TokenInventory:
    if not name.strip():
        raise ValueError("Token name is required")
    if quantity < 0:
        raise ValueError("Quantity cannot be negative")

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
        is_double_sided=bool(is_double_sided),
        back_name=back_name.strip() if back_name else None,
        back_image_url=back_image_url.strip() if back_image_url else None,
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


def total_token_count(session: Session, user_id: int) -> int:
    return (
        session.query(func.coalesce(func.sum(TokenInventory.quantity), 0))
        .filter(TokenInventory.user_id == user_id)
        .scalar()
        or 0
    )
