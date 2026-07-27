from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import Deck, InventoryRow, StorageLocation
from app.pricing import effective_price

VALID_LOCATION_TYPES = {"root", "drawer", "binder", "box", "deck", "considering", "other"}

# v3.26.2 — per-location sorter modes. Validated at this layer (matches the
# existing VALID_LOCATION_TYPES pattern; no DB-level CHECK constraint).
VALID_LOCATION_MODES = {"managed", "manual", "sink", "ignored"}

# Modes the drawer sorter will PLACE INTO. Single source of truth for
# is_sortable_target (Python predicate) AND the equivalent SQL filter in
# resort_collection() (inventory_service.py). Currently only ``managed``.
SORTABLE_TARGET_MODES = frozenset({"managed"})

# Modes the drawer sorter will MOVE OUT OF. Single source of truth for
# is_sortable_source (Python predicate) AND the equivalent SQL filter in
# resort_collection() (inventory_service.py). ``managed`` locations can be
# rebalanced; ``sink`` locations can be drained during rebalancing.
SORTABLE_SOURCE_MODES = frozenset({"managed", "sink"})

# #159 — user-facing mode labels. DISPLAY ONLY: the stored values above are
# unchanged, and every comparison site still reads them. This exists because the
# stored names describe the sorter's ROLE for a location ("managed", "sink") and
# not the CONSEQUENCE to the user's cards, which is the thing they need to choose
# on. Nothing in the picker told a user that "managed" means the sorter may empty
# this box — and since ``mode`` defaulted to managed, four accounts ended up with
# sortable storage they never chose (see #159, and the first-rule sweep warning
# in sorter_rule_service.first_sweep_preview).
#
# Ordered SAFEST FIRST: the first entry is the create-form default, so the
# do-nothing option is what a user gets when they don't engage with this choice.
# Each label states what the sorter does TO THE CARDS, derived from the two
# predicates above — keep them in sync if the predicates ever change:
#
#     mode      sorter files cards IN   sorter takes cards OUT
#     manual            no                      no
#     ignored           no                      no      (+ ranked last in trade suggestions)
#     managed           yes                     yes
#     sink              no                      yes
# Each entry is (stored_value, picker_label, description, badge). The badge is the
# at-a-glance table form — a status, not an instruction — because scanning which
# boxes are at risk is the whole point; the picker label is the choice the user makes.
LOCATION_MODE_CHOICES: list[tuple[str, str, str, str]] = [
    (
        "manual",
        "Leave this alone",
        "The sorter never adds or removes cards. Your filing stays exactly as you put it.",
        "Left alone",
    ),
    (
        "managed",
        "Let the sorter organize this",
        "The sorter may file cards here, and may take cards away when it rebalances.",
        "Sorter organizes",
    ),
    (
        "sink",
        "Overflow — the sorter may empty this",
        "The sorter never files cards here, but may pull cards out of it when rebalancing.",
        "Sorter may empty",
    ),
    (
        "ignored",
        "Leave alone, and skip in trade suggestions",
        "Same sorter behaviour as \u201cLeave this alone\u201d. Additionally ranked last when "
        "suggesting cards you could trade.",
        "Left alone \u00b7 no trades",
    ),
]

DEFAULT_LOCATION_MODE = LOCATION_MODE_CHOICES[0][0]


def location_mode_label(mode: str) -> str:
    """Picker label for a stored mode value; unknown → the raw value."""
    for value, label, _desc, _badge in LOCATION_MODE_CHOICES:
        if value == mode:
            return label
    return mode


def location_mode_badge(mode: str) -> str:
    """Short at-a-glance table form; unknown → the raw value."""
    for value, _label, _desc, badge in LOCATION_MODE_CHOICES:
        if value == mode:
            return badge
    return mode


def is_sortable_target(location: StorageLocation) -> bool:
    """Return True if the drawer sorter may PLACE cards INTO this location.

    Per v3.26.2 mode semantics: only ``managed`` locations are sorter targets.
    ``manual`` keeps existing contents in place but accepts no new placement;
    ``sink`` is a source-only catch-all; ``ignored`` is invisible to the sorter.

    Consults ``SORTABLE_TARGET_MODES``; the DB-level filter in
    ``resort_collection()`` consults the same constant so the Python predicate
    and the SQL query stay aligned.
    """
    return location.mode in SORTABLE_TARGET_MODES


def is_sortable_source(location: StorageLocation) -> bool:
    """Return True if the drawer sorter may MOVE cards OUT OF this location.

    Per v3.26.2 mode semantics: ``managed`` locations can be rebalanced out
    of, and ``sink`` locations can be drained during rebalancing. ``manual``
    locks contents in place; ``ignored`` is invisible.

    Consults ``SORTABLE_SOURCE_MODES``; the DB-level filter in
    ``resort_collection()`` consults the same constant so the Python predicate
    and the SQL query stay aligned.
    """
    return location.mode in SORTABLE_SOURCE_MODES


def list_locations(session: Session, user_id: int) -> list[StorageLocation]:
    # #148 — "considering" locations are per-deck internal holding areas, managed
    # only via the deck's Considering section (not the Locations page or any
    # move-destination dropdown). Excluding them here keeps them out of every
    # list_locations consumer in one place (they never appear as a move target,
    # so a card can't be manually moved into/out of Considering, bypassing the
    # promote/demote flow and the #140 placeholder guard).
    return (
        session.query(StorageLocation)
        .options(joinedload(StorageLocation.parent))
        .filter(StorageLocation.user_id == user_id, StorageLocation.type != "considering")
        .order_by(
            StorageLocation.parent_id.nullsfirst(), StorageLocation.sort_order, StorageLocation.name
        )
        .all()
    )


def get_location(session: Session, location_id: int, user_id: int) -> StorageLocation | None:
    return (
        session.query(StorageLocation)
        .filter(
            StorageLocation.id == location_id,
            StorageLocation.user_id == user_id,
        )
        .first()
    )


def create_location(
    session: Session,
    user_id: int,
    name: str,
    type: str,
    parent_id: int | None = None,
    sort_order: int = 0,
    mode: str = DEFAULT_LOCATION_MODE,  # #159 — safe default; see LOCATION_MODE_CHOICES
    note: str | None = None,
    capacity: int | None = None,
) -> StorageLocation:
    name = name.strip()
    type = type.strip().lower() or "other"
    mode = (mode or DEFAULT_LOCATION_MODE).strip().lower()

    if not name:
        raise ValueError("Location name is required.")

    if type not in VALID_LOCATION_TYPES:
        raise ValueError(f"Invalid location type: {type}")

    # v3.30.17 — deck-type StorageLocations MUST be created through
    # deck_service.create_deck so the paired Deck row lands atomically.
    # v3.30.15's auto_create_locations bypassed the v3.8.1 defense (at
    # /locations/create-inline) by reaching create_location directly with
    # type="deck", producing orphan deck-locations on confirm. This guard
    # restores the v3.3 architectural invariant: every type="deck"
    # StorageLocation has a paired Deck row. The canonical create_deck
    # path uses raw StorageLocation(...) + session.add(), NOT this
    # function, so adding the guard here does not break the canonical path.
    if type == "deck":
        raise ValueError(
            "create_location refuses type='deck'; use deck_service.create_deck "
            "so the paired Deck row is created atomically."
        )

    if mode not in VALID_LOCATION_MODES:
        raise ValueError(f"Invalid location mode: {mode}")

    if note is not None:
        note = note.strip() or None

    if capacity is not None and capacity <= 0:
        capacity = None

    existing = (
        session.query(StorageLocation)
        .filter(
            StorageLocation.user_id == user_id,
            StorageLocation.name == name,
        )
        .first()
    )
    if existing:
        raise ValueError(f"A location named '{name}' already exists.")

    if parent_id is not None:
        parent = get_location(session, parent_id, user_id)
        if parent is None:
            raise ValueError("Parent location does not exist.")

    location = StorageLocation(
        user_id=user_id,
        name=name,
        type=type,
        parent_id=parent_id,
        sort_order=sort_order,
        mode=mode,
        note=note,
        capacity=capacity,
    )
    session.add(location)
    session.commit()
    session.refresh(location)
    return location


def get_location_summary(session: Session, user_id: int) -> list[dict]:
    locations = list_locations(session, user_id=user_id)

    # v3.28.6 — batched last-touched aggregate. Single GROUP BY across all of
    # the user's InventoryRows, keyed by storage_location_id. Looked up O(1)
    # per location, no per-location query. Faithful "last activity at this
    # location" because every inventory mutation (placement, move, quantity
    # adjustment) writes InventoryRow.updated_at via 15+ explicit
    # `updated_at = utc_now()` sites in inventory_service.py /
    # import_service.py / main.py — verified pre-implementation.
    last_touched_map: dict[int, object] = dict(
        session.query(
            InventoryRow.storage_location_id,
            func.max(InventoryRow.updated_at),
        )
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id.isnot(None),
        )
        .group_by(InventoryRow.storage_location_id)
        .all()
    )

    summaries = []
    for location in locations:
        rows = (
            session.query(InventoryRow)
            .options(joinedload(InventoryRow.card))
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.storage_location_id == location.id,
            )
            .all()
        )

        quantity = sum(row.quantity for row in rows)
        total_value = 0.0

        for row in rows:
            price = effective_price(row.card, row.finish) or 0.0
            total_value += price * row.quantity

        is_orphaned_deck = (
            location.type == "deck"
            and not session.query(Deck).filter(Deck.storage_location_id == location.id).first()
        )
        has_children = (
            session.query(StorageLocation).filter(StorageLocation.parent_id == location.id).first()
            is not None
        )
        is_deletable = (
            len(rows) == 0
            and location.type not in ("root",)
            and (location.type != "deck" or is_orphaned_deck)
            and not has_children
        )
        # Non-empty non-root non-deck (and no children) → delete-with-redirect.
        # Deck-typed locations route through the Decks page even when orphaned-
        # with-rows, matching the v3.26.4 spec ("no change to deck-typed
        # location deletion").
        is_redirectable = (
            len(rows) > 0 and location.type not in ("root", "deck") and not has_children
        )

        # v3.28.6 — capacity meter derivations. capacity_pct is the
        # fill-percentage (0-100, clamped) when capacity is set; None when
        # NULL so the template branches on the value rather than a special
        # zero. is_over_capacity flags ≥95% — the design package's `attn`
        # threshold, used to apply the oxblood meter color.
        capacity_pct: float | None = None
        is_over_capacity = False
        if location.capacity and location.capacity > 0:
            capacity_pct = min(100.0, (quantity / location.capacity) * 100.0)
            is_over_capacity = (quantity / location.capacity) >= 0.95

        summaries.append(
            {
                "location": location,
                "row_count": len(rows),
                "quantity": quantity,
                "total_value": total_value,
                "is_deletable": is_deletable,
                "is_redirectable": is_redirectable,
                "last_touched_at": last_touched_map.get(location.id),
                "capacity_pct": capacity_pct,
                "is_over_capacity": is_over_capacity,
            }
        )

    return summaries


def update_location(
    session: Session,
    location_id: int,
    user_id: int,
    name: str,
    type: str,
    parent_id: int | None = None,
    sort_order: int = 0,
    mode: str | None = None,
    note: str | None = None,
    capacity: int | None = None,
    update_note: bool = False,
    update_capacity: bool = False,
) -> StorageLocation:
    """Update an editable location. ``note`` and ``capacity`` are only
    applied when their corresponding ``update_note`` / ``update_capacity``
    flags are True — distinguishes "not in this form submission" from
    "explicitly cleared." The route handler passes the flags True when the
    form carries those fields (the v3.28.6 Locations edit popout always
    includes both); future routes that don't carry them can leave the
    flags False to preserve the stored value.
    """
    location = get_location(session, location_id, user_id)
    if location is None:
        raise ValueError("Location not found.")
    if location.type == "root":
        raise ValueError("Root location cannot be edited.")
    if location.type == "deck":
        raise ValueError("Deck locations are managed through the Decks page.")

    name = name.strip()
    if not name:
        raise ValueError("Location name is required.")

    type = type.strip().lower() or "other"
    if type in ("root", "deck"):
        raise ValueError(f"Cannot set type to '{type}'.")
    if type not in VALID_LOCATION_TYPES:
        raise ValueError(f"Invalid location type: {type}")

    if mode is not None:
        mode = mode.strip().lower()
        if mode not in VALID_LOCATION_MODES:
            raise ValueError(f"Invalid location mode: {mode}")

    existing = (
        session.query(StorageLocation)
        .filter(
            StorageLocation.user_id == user_id,
            StorageLocation.name == name,
            StorageLocation.id != location_id,
        )
        .first()
    )
    if existing:
        raise ValueError(f"A location named '{name}' already exists.")

    if parent_id is not None:
        if parent_id == location_id:
            raise ValueError("A location cannot be its own parent.")
        parent = get_location(session, parent_id, user_id)
        if parent is None:
            raise ValueError("Parent location does not exist.")

    location.name = name
    location.type = type
    location.parent_id = parent_id
    location.sort_order = sort_order
    if mode is not None:
        location.mode = mode
    if update_note:
        # Empty string clears the note; whitespace-only also clears.
        location.note = (note.strip() if note else None) or None
    if update_capacity:
        # Falsy / non-positive clears the capacity.
        location.capacity = capacity if (capacity is not None and capacity > 0) else None
    session.commit()
    return location


def delete_location(
    session: Session,
    location_id: int,
    user_id: int,
    destination_id: int | None = None,
) -> None:
    """Delete a location. When ``destination_id`` is provided, any rows
    currently in the location are moved to that destination first, then
    the now-empty location is deleted.

    Per-row moves go through ``move_inventory_row_to_location``, which
    commits per row (matching the existing bulk-move pattern); the
    final ``session.delete(location)`` + commit happens only after all
    moves succeed. A mid-loop failure leaves the rows that already
    moved at the destination — the source location stays (and still has
    the unmoved tail), so the operation is safely re-runnable.

    Empty-location deletion (the original v3.10.6 path) keeps its
    behavior unchanged when ``destination_id`` is None.
    """
    location = get_location(session, location_id=location_id, user_id=user_id)
    if location is None:
        raise ValueError("Location not found.")
    if location.type == "root":
        raise ValueError("This location cannot be deleted directly.")
    if location.type == "deck":
        linked_deck = session.query(Deck).filter(Deck.storage_location_id == location_id).first()
        if linked_deck:
            raise ValueError("Delete the deck from the Decks page to remove this location.")
    has_children = (
        session.query(StorageLocation).filter(StorageLocation.parent_id == location_id).first()
    )
    if has_children:
        raise ValueError("Cannot delete a location that has child locations.")

    if destination_id is not None:
        if destination_id == location_id:
            raise ValueError("Cannot redirect a location's contents into itself.")
        destination = get_location(session, location_id=destination_id, user_id=user_id)
        if destination is None:
            raise ValueError("Destination location not found.")

        # Local import avoids the inventory_service ↔ location_service
        # circular at module load.
        from app.inventory_service import move_inventory_row_to_location

        row_ids = [
            row_id
            for (row_id,) in session.query(InventoryRow.id)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.storage_location_id == location_id,
            )
            .all()
        ]
        for row_id in row_ids:
            move_inventory_row_to_location(
                session, row_id=row_id, user_id=user_id, location_id=destination_id
            )
    else:
        has_rows = (
            session.query(InventoryRow)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.storage_location_id == location_id,
            )
            .first()
        )
        if has_rows:
            raise ValueError(
                "Cannot delete a location that still contains cards. Move or remove them first."
            )

    session.delete(location)
    session.commit()


def list_rows_for_location(
    session: Session,
    user_id: int,
    location_id: int,
) -> list[InventoryRow]:
    location = get_location(session, location_id=location_id, user_id=user_id)
    if location is None:
        raise ValueError("Location does not exist.")

    return (
        session.query(InventoryRow)
        .options(
            joinedload(InventoryRow.card),
            joinedload(InventoryRow.storage_location),
        )
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id == location_id,
        )
        .order_by(InventoryRow.slot.asc())
        .all()
    )


def user_has_drawers(session: Session, user_id: int) -> bool:
    """True if the user has any numbered-drawer location. Gates the drawer-specific
    /drawers pages + nav (#104 — replaced the old username-frozenset gate)."""
    return (
        session.query(StorageLocation.id)
        .filter(StorageLocation.user_id == user_id, StorageLocation.type == "drawer")
        .first()
        is not None
    )
