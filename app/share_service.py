"""Collection sharing service layer (v3.29.1).

Per-domain service file following the established convention
(``playgroup_service.py``, ``watchlist_service.py``, ``deck_service.py``).
Implements the curated-list collection-sharing model settled in the
v3.29.1 spec's decision register.

**Showcase ≠ Share.**

- A :class:`~app.models.Showcase` is a user's CURATED list of items they
  have prepared for sharing. One per user (decision A5). Lives forever
  until the user deletes its items (or the account is removed).
- A :class:`~app.models.Share` is one ACT of exposing a Showcase to one
  playgroup, read-only (decisions B1 + B3). Ephemeral — revoking
  hard-deletes the Share row but never touches the Showcase (decision
  B2).

**Privacy by construction.** :func:`build_share_display_items` produces a
sanitized projection that whitelists only the fields the spec's §8
allows. Fields like ``InventoryRow.notes`` / ``role`` / ``is_pending`` /
``storage_location_id`` / ``drawer`` / ``slot`` / ``from_drawer`` /
``from_slot`` / ``created_at`` / ``updated_at`` / ``user_id`` and the
sharer-private ``ShowcaseItem.notes`` are NEVER in the projection —
absent fields cannot leak. The verification gate's privacy hard-flag
asserts the rendered share-view HTML carries none of these markers.

**Visibility scope (E2).** :func:`get_share_view` and
:func:`list_shares_for_playgroup` check membership via a DIRECT
``PlaygroupMember`` filter on ``Share.playgroup_id`` — NOT
``co_members_of``. The share's audience is the chosen playgroup
specifically, not the sharer's social graph in general.

**Lifecycle integration** (§9 of the spec). When a user leaves a
playgroup, an owner removes them, or a playgroup is deleted, the
related Share rows are hard-deleted by
:mod:`app.playgroup_service`. Admin user-deletion cascades through
``routes/admin.py`` (Share rows then Showcase row; the Showcase
cascades ShowcaseItem via ``cascade="all, delete-orphan"``).
InventoryRow deletion drops the dependent ShowcaseItem rows BEFORE the
row goes, with the defensive read-skip in
:func:`build_share_display_items` as a belt-and-suspenders backstop.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, contains_eager, joinedload

from app.models import (
    Card,
    InventoryRow,
    PlaygroupMember,
    Share,
    Showcase,
    ShowcaseItem,
    User,
)
from app.pricing import effective_price

# ── Showcase management ─────────────────────────────────────────


def list_showcases(session: Session, user_id: int) -> list[Showcase]:
    """All of the user's Showcases, oldest first (creation order).

    v3.31.0 — multi-showcase. Returns an empty list if the user has
    never created one (a Showcase is no longer lazily created just by
    visiting the area; see :func:`get_or_create_showcase`).
    """
    return (
        session.query(Showcase)
        .filter(Showcase.user_id == user_id)
        .order_by(Showcase.created_at.asc(), Showcase.id.asc())
        .all()
    )


def get_showcase(session: Session, user_id: int, showcase_id: int) -> Showcase | None:
    """Return one of the user's Showcases by id, or None if not owned.

    Ownership-scoped: a ``showcase_id`` belonging to another user (or a
    non-existent id) returns None so the route layer can redirect
    non-leakily rather than 403/500.
    """
    return (
        session.query(Showcase)
        .filter(Showcase.id == showcase_id, Showcase.user_id == user_id)
        .first()
    )


def get_or_create_showcase(session: Session, user_id: int) -> Showcase:
    """Return the user's *default* Showcase, lazily creating one if none.

    v3.31.0 — multi-showcase. "Default" is the oldest Showcase (creation
    order); if the user has none, one named "My Showcase" is created.
    This preserves the v3.29.1 lazy-create contract for the legacy
    single-showcase entry points (the ``/showcase`` redirect, the
    add-to-showcase fallback when no explicit showcase is chosen) while
    the unique constraint that used to guarantee singularity is gone.
    """
    showcase = (
        session.query(Showcase)
        .filter(Showcase.user_id == user_id)
        .order_by(Showcase.created_at.asc(), Showcase.id.asc())
        .first()
    )
    if showcase is not None:
        return showcase
    return create_showcase(session, user_id, "My Showcase", None)


def create_showcase(
    session: Session,
    user_id: int,
    name: str | None,
    description: str | None,
) -> Showcase:
    """Create a new Showcase for the user. v3.31.0 — multi-showcase.

    Name normalizes to "My Showcase" when empty/whitespace; description
    normalizes to None on empty. There is no per-user uniqueness on the
    name — two Showcases may share a name (the id disambiguates).
    """
    trimmed = (name or "").strip()
    trimmed_desc = (description or "").strip()
    showcase = Showcase(
        user_id=user_id,
        name=trimmed[:128] if trimmed else "My Showcase",
        description=trimmed_desc or None,
    )
    session.add(showcase)
    session.commit()
    session.refresh(showcase)
    return showcase


def add_rows_to_showcase(
    session: Session,
    user_id: int,
    showcase_id: int,
    location_id: int | None = None,
    row_ids: list[int] | None = None,
) -> dict | None:
    """Bulk-add the user's inventory rows to one of their Showcases.

    v3.31.0 — the "share my whole collection / a whole location" entry
    point. ``location_id=None`` adds every placed row the user owns;
    otherwise only rows in that StorageLocation. Pure DB work — no
    external calls — so it is safe on the request path (the per-row
    Scryfall ban in CLAUDE.md does not apply).

    v3.x — ``row_ids`` is the filter-scoped Collection bulk-action entry
    point: an explicit set of inventory-row ids (resolved by the route
    from the Collection filter). When given it **takes precedence over**
    ``location_id`` and replaces the location filter with
    ``InventoryRow.id.in_(row_ids)``; the ``user_id`` + ``is_pending``
    guards below still apply, so a forged or foreign id can never leak in.
    An empty list matches nothing (adds 0), never raises.

    Scope decisions:
      - ``is_pending`` rows are excluded — they aren't finalised into the
        collection yet, so they don't belong in a curated "haves" list.
      - ``quantity_offered`` is set to the row's full quantity (you're
        showing off what you actually have), not 1 as the single-card
        add-button uses.
      - Idempotent: rows already in the Showcase are skipped (the
        ``UNIQUE(showcase_id, inventory_row_id)`` set semantics), so
        re-running after adding more cards only tops up the difference.

    Returns ``None`` if the Showcase isn't owned by ``user_id``;
    otherwise ``{"added": int, "skipped": int, "total": int}`` where
    ``total`` is the number of eligible rows considered.
    """
    showcase = get_showcase(session, user_id, showcase_id)
    if showcase is None:
        return None
    query = session.query(InventoryRow).filter(
        InventoryRow.user_id == user_id,
        InventoryRow.is_pending == False,  # noqa: E712
    )
    if row_ids is not None:
        query = query.filter(InventoryRow.id.in_(row_ids))
    elif location_id is not None:
        query = query.filter(InventoryRow.storage_location_id == location_id)
    rows = query.all()
    existing_ids = {
        row_id
        for (row_id,) in session.query(ShowcaseItem.inventory_row_id)
        .filter(ShowcaseItem.showcase_id == showcase.id)
        .all()
    }
    added = 0
    for row in rows:
        if row.id in existing_ids:
            continue
        session.add(
            ShowcaseItem(
                showcase_id=showcase.id,
                inventory_row_id=row.id,
                quantity_offered=max(1, row.quantity),
            )
        )
        added += 1
    if added:
        session.commit()
    return {"added": added, "skipped": len(rows) - added, "total": len(rows)}


def delete_showcase(session: Session, user_id: int, showcase_id: int) -> bool:
    """Delete one of the user's Showcases. Per-user scoped.

    v3.31.0 — multi-showcase. Mirrors the admin user-deletion ordering
    (``routes/admin.py``) so the outcome holds regardless of SQLite's
    PRAGMA foreign_keys posture:

      1. NULL any ``TradeItem.showcase_item_id`` referencing this
         Showcase's items (the trade stays live against its
         ``inventory_row_id`` — decision C1, §10).
      2. Hard-delete the ``Share`` rows pointing at this Showcase (their
         audience is going away; the Showcase being deleted means the
         shares can't outlive it).
      3. ``session.delete`` the Showcase via ORM so ``cascade="all,
         delete-orphan"`` on ``Showcase.items`` takes the ShowcaseItem
         rows with it.

    Returns False if the Showcase isn't owned by ``user_id`` (silently,
    non-leaky).
    """
    showcase = get_showcase(session, user_id, showcase_id)
    if showcase is None:
        return False
    item_ids = [
        row_id
        for (row_id,) in session.query(ShowcaseItem.id)
        .filter(ShowcaseItem.showcase_id == showcase.id)
        .all()
    ]
    if item_ids:
        # Function-level import — trade_service imports share_service at
        # module level (see remove_showcase_item's note).
        from app import trade_service

        trade_service.null_trade_item_showcase_links(session, item_ids)
    session.query(Share).filter(Share.showcase_id == showcase.id).delete(synchronize_session=False)
    session.delete(showcase)
    session.commit()
    return True


def _query_showcase_items(session: Session, showcase_id: int, search: str = ""):
    """Fetch a Showcase's ShowcaseItems, oldest-display-first, optionally
    filtered by the app's boolean/Scryfall-style search language (v3.32.3).

    Joins through InventoryRow → Card (INNER, via the relationships) so the
    shared boolean parser (`apply_collection_search_filters`, the same one
    that backs the Collection search bar — `t:`, `id:`, `c:`, `is:`, `qty:`,
    `price:`, `cmc:`, `lang:`, OR/AND/NOT/parentheses) can filter on Card /
    InventoryRow columns. ``contains_eager`` reuses that join for eager
    loading so there's no second query per item. The INNER join also drops
    any dangling-FK ShowcaseItem at the query level (the per-item None guard
    in the callers stays as defense in depth). The import of
    `apply_collection_search_filters` is function-local to avoid a module
    import cycle (inventory_service ↔ share_service).
    """
    q = (
        session.query(ShowcaseItem)
        .filter(ShowcaseItem.showcase_id == showcase_id)
        .join(ShowcaseItem.inventory_row)
        .join(InventoryRow.card)
        .options(contains_eager(ShowcaseItem.inventory_row).contains_eager(InventoryRow.card))
        .order_by(ShowcaseItem.added_at.desc())
    )
    if search and search.strip():
        from app.inventory_service import apply_collection_search_filters

        q = apply_collection_search_filters(q, search)
    return q.all()


def get_showcase_with_items(
    session: Session,
    user_id: int,
    showcase_id: int,
    search: str = "",
) -> dict | None:
    """Management-page payload — one Showcase + items + computed totals.

    v3.31.0 — multi-showcase. Scoped to a specific ``showcase_id`` and
    ownership-checked: returns ``None`` if the Showcase isn't owned by
    ``user_id`` (route redirects non-leakily). Returns
    ``{"showcase": Showcase, "items": [{...}], "total_value": float}``
    where each item dict carries the live InventoryRow + Card join, the
    computed ``available = min(quantity_offered, InventoryRow.quantity)``,
    and a finish-aware ``effective_price`` / ``value`` for the sharer's
    own view. ``total_value`` is the sum of ``value`` across items.

    Defensive: items whose ``inventory_row`` is None (dangling FK — the
    row was deleted somehow, despite the §9 cleanup; defense in depth)
    are silently skipped from the returned list. The DB row stays put
    until removed via the management UI or admin cascade.
    """
    showcase = get_showcase(session, user_id, showcase_id)
    if showcase is None:
        return None
    items_q = _query_showcase_items(session, showcase.id, search)
    items: list[dict] = []
    total_value = 0.0
    for it in items_q:
        inv = it.inventory_row
        if inv is None or inv.card is None:
            # Dangling FK — defense in depth; §9 cleanup should have caught it.
            continue
        available = min(it.quantity_offered, inv.quantity)
        price = effective_price(inv.card, inv.finish) or 0.0
        value = price * available
        total_value += value
        items.append(
            {
                "id": it.id,
                "showcase_id": it.showcase_id,
                "inventory_row_id": inv.id,
                "quantity_offered": it.quantity_offered,
                "available": available,
                "effective_price": price,
                "value": value,
                "notes": it.notes,  # sharer-private; only on the OWN management view
                "card": inv.card,
                "finish": inv.finish,
                "language": inv.language or "en",
                "is_proxy": bool(inv.is_proxy),
                "added_at": it.added_at,
            }
        )
    return {"showcase": showcase, "items": items, "total_value": total_value}


def update_showcase(
    session: Session,
    user_id: int,
    showcase_id: int,
    name: str | None,
    description: str | None,
) -> Showcase | None:
    """Update a Showcase's name and/or description. Per-user scoped.

    v3.31.0 — multi-showcase: scoped to a specific ``showcase_id`` and
    ownership-checked (returns None if not owned). Empty/whitespace name
    leaves the name unchanged (defensive — a Showcase always carries a
    non-empty name; the DB default is "My Showcase"). Description
    normalizes to None on empty.
    """
    showcase = get_showcase(session, user_id, showcase_id)
    if showcase is None:
        return None
    if name is not None:
        trimmed = name.strip()
        if trimmed:
            showcase.name = trimmed[:128]
    if description is not None:
        trimmed_desc = description.strip()
        showcase.description = trimmed_desc or None
    session.commit()
    session.refresh(showcase)
    return showcase


def add_showcase_item(
    session: Session,
    user_id: int,
    inventory_row_id: int,
    showcase_id: int | None = None,
    quantity_offered: int = 1,
) -> ShowcaseItem | None:
    """Add an InventoryRow to a Showcase. Idempotent on duplicates.

    v3.31.0 — multi-showcase. ``showcase_id`` selects which Showcase to
    add to; it must be owned by ``user_id``. When ``None`` (or 0 — the
    legacy single-showcase add-button path, or a card added before any
    Showcase exists), the user's default Showcase is used / lazily
    created via :func:`get_or_create_showcase`.

    The InventoryRow must also belong to ``user_id`` — service-layer
    ownership check (a tampered POST with another user's row id is
    silently rejected, returning None). Already-added rows return the
    existing ShowcaseItem without raising or modifying state (the
    ``UNIQUE(showcase_id, inventory_row_id)`` index makes a duplicate
    INSERT IntegrityError; the service catches it).

    ``quantity_offered`` clamps to >= 1 (negative/zero coerced to 1).
    """
    row = (
        session.query(InventoryRow.id)
        .filter(InventoryRow.id == inventory_row_id, InventoryRow.user_id == user_id)
        .first()
    )
    if row is None:
        return None
    qty = max(1, int(quantity_offered or 1))
    if showcase_id:
        showcase = get_showcase(session, user_id, showcase_id)
        if showcase is None:
            # Showcase id supplied but not owned by this user — reject.
            return None
    else:
        showcase = get_or_create_showcase(session, user_id)
    existing = (
        session.query(ShowcaseItem)
        .filter(
            ShowcaseItem.showcase_id == showcase.id,
            ShowcaseItem.inventory_row_id == inventory_row_id,
        )
        .first()
    )
    if existing is not None:
        return existing
    item = ShowcaseItem(
        showcase_id=showcase.id,
        inventory_row_id=inventory_row_id,
        quantity_offered=qty,
    )
    session.add(item)
    try:
        session.commit()
    except IntegrityError:
        # Concurrent insert won the race; fetch and return.
        session.rollback()
        item = (
            session.query(ShowcaseItem)
            .filter(
                ShowcaseItem.showcase_id == showcase.id,
                ShowcaseItem.inventory_row_id == inventory_row_id,
            )
            .first()
        )
    session.refresh(item)
    return item


def _get_owned_showcase_item(
    session: Session,
    user_id: int,
    showcase_item_id: int,
) -> ShowcaseItem | None:
    """Return a ShowcaseItem iff its Showcase is owned by ``user_id``.

    v3.31.0 — multi-showcase. Joins ShowcaseItem → Showcase and filters
    on ``Showcase.user_id`` so an item-id is only actionable by the user
    who owns the Showcase it belongs to, regardless of how many
    Showcases that user has.
    """
    return (
        session.query(ShowcaseItem)
        .join(Showcase, Showcase.id == ShowcaseItem.showcase_id)
        .filter(ShowcaseItem.id == showcase_item_id, Showcase.user_id == user_id)
        .first()
    )


def update_quantity_offered(
    session: Session,
    user_id: int,
    showcase_item_id: int,
    quantity_offered: int,
) -> ShowcaseItem | None:
    """Adjust the quantity_offered on one ShowcaseItem.

    Per-user scoped: an item whose Showcase isn't owned by ``user_id``
    is rejected (returns None). v3.31.0 — multi-showcase: ownership is
    resolved by joining the item's Showcase rather than assuming a
    single per-user Showcase. Clamps to >= 1.
    """
    item = _get_owned_showcase_item(session, user_id, showcase_item_id)
    if item is None:
        return None
    item.quantity_offered = max(1, int(quantity_offered or 1))
    session.commit()
    session.refresh(item)
    return item


def remove_showcase_item(
    session: Session,
    user_id: int,
    showcase_item_id: int,
) -> bool:
    """Remove a curated card from a Showcase. Per-user scoped.

    v3.29.2 — NULLs any ``TradeItem.showcase_item_id`` references to
    the removed item BEFORE the delete (§10). The trade itself stays
    live; the link is navigation metadata (decision C1) and the trade
    runs against its ``inventory_row_id`` regardless.

    v3.31.0 — multi-showcase: ownership is resolved by joining the
    item's Showcase rather than assuming a single per-user Showcase.
    """
    item = _get_owned_showcase_item(session, user_id, showcase_item_id)
    if item is None:
        return False
    # v3.29.2 — NULL TradeItem.showcase_item_id for any trade-item
    # referencing this ShowcaseItem. Function-level import keeps the
    # module-load order honest (trade_service imports share_service's
    # _ReadOnlyCardProjection at module level; we MUST NOT import
    # trade_service at module level here).
    from app import trade_service

    trade_service.null_trade_item_showcase_links(session, [item.id])
    session.delete(item)
    session.commit()
    return True


# ── Share management ────────────────────────────────────────────


def create_share(
    session: Session,
    user_id: int,
    showcase_id: int,
    playgroup_id: int,
) -> Share | None:
    """Share one of the user's Showcases with one playgroup. Idempotent.

    v3.31.0 — multi-showcase: ``showcase_id`` selects which Showcase to
    expose; it must be owned by ``user_id`` (rejected silently → None
    otherwise). The user must also be a member of the playgroup
    (PlaygroupMember check); rejected silently otherwise (returns None).
    The ``UNIQUE(showcase_id, playgroup_id)`` index makes a duplicate
    Share INSERT IntegrityError; the service catches it and returns the
    existing Share row.
    """
    showcase = get_showcase(session, user_id, showcase_id)
    if showcase is None:
        return None
    is_member = (
        session.query(PlaygroupMember.id)
        .filter(
            PlaygroupMember.user_id == user_id,
            PlaygroupMember.playgroup_id == playgroup_id,
        )
        .first()
    )
    if is_member is None:
        return None
    existing = (
        session.query(Share)
        .filter(
            Share.showcase_id == showcase.id,
            Share.playgroup_id == playgroup_id,
        )
        .first()
    )
    if existing is not None:
        return existing
    share = Share(
        user_id=user_id,
        showcase_id=showcase.id,
        playgroup_id=playgroup_id,
    )
    session.add(share)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        share = (
            session.query(Share)
            .filter(
                Share.showcase_id == showcase.id,
                Share.playgroup_id == playgroup_id,
            )
            .first()
        )
    session.refresh(share)
    return share


def revoke_share(session: Session, user_id: int, share_id: int) -> bool:
    """Hard-delete a Share (decision B2 — no soft-revoke).

    Per-user scoped: a Share not owned by ``user_id`` is rejected
    silently (returns False). The Showcase the Share pointed at is
    NEVER touched here — the Share is ephemeral; the Showcase persists.
    """
    share = session.query(Share).filter(Share.id == share_id, Share.user_id == user_id).first()
    if share is None:
        return False
    session.delete(share)
    session.commit()
    return True


def list_my_shares(session: Session, user_id: int) -> list[dict]:
    """Shares the user has created. Includes the joined Playgroup + Showcase.

    v3.31.0 — multi-showcase: the Showcase is joined in so the "my
    shares" table can name WHICH Showcase each share exposes (a user
    may now share several different ones).
    """
    rows = (
        session.query(Share)
        .filter(Share.user_id == user_id)
        .options(joinedload(Share.playgroup), joinedload(Share.showcase))
        .order_by(Share.created_at.desc())
        .all()
    )
    return [
        {
            "share": s,
            "playgroup": s.playgroup,
            "showcase": s.showcase,
        }
        for s in rows
    ]


def list_shares_for_playgroup(
    session: Session,
    viewer_user_id: int,
    playgroup_id: int,
) -> list[dict]:
    """Shares targeting a specific playgroup, visible to that playgroup's members.

    Decision E2 — direct PlaygroupMember filter on
    ``Share.playgroup_id``, NOT ``co_members_of``. The audience of a
    Share is the chosen playgroup specifically.

    Returns empty list if ``viewer_user_id`` is not a member of the
    playgroup — non-members never see shares.
    """
    is_member = (
        session.query(PlaygroupMember.id)
        .filter(
            PlaygroupMember.user_id == viewer_user_id,
            PlaygroupMember.playgroup_id == playgroup_id,
        )
        .first()
    )
    if is_member is None:
        return []
    rows = (
        session.query(Share, User, Showcase)
        .join(User, User.id == Share.user_id)
        .join(Showcase, Showcase.id == Share.showcase_id)
        .filter(Share.playgroup_id == playgroup_id)
        .order_by(Share.created_at.desc())
        .all()
    )
    return [
        {
            "share": s,
            "sharer": u,
            "showcase": sc,
        }
        for s, u, sc in rows
    ]


def get_share_view(
    session: Session,
    viewer_user_id: int,
    share_id: int,
    search: str = "",
) -> dict | None:
    """Resolve a Share for a viewer. Returns sanitized projection or None.

    Returns ``None`` if (a) the share doesn't exist, OR (b) the viewer
    is not a member of the share's playgroup. The route layer renders
    a non-leaky redirect on None; never a 403 (keeps share-id existence
    non-leaky).

    Visibility scope (E2): direct ``PlaygroupMember`` filter on
    ``Share.playgroup_id`` — the share is visible to members of the
    specific playgroup it targets, NOT to everyone the sharer co-belongs
    with elsewhere.
    """
    share = (
        session.query(Share)
        .filter(Share.id == share_id)
        .options(
            joinedload(Share.showcase),
            joinedload(Share.playgroup),
            joinedload(Share.user),
        )
        .first()
    )
    if share is None:
        return None
    # The sharer always sees their own share (e.g. preview from /shares).
    if share.user_id != viewer_user_id:
        is_member = (
            session.query(PlaygroupMember.id)
            .filter(
                PlaygroupMember.user_id == viewer_user_id,
                PlaygroupMember.playgroup_id == share.playgroup_id,
            )
            .first()
        )
        if is_member is None:
            return None
    showcase = share.showcase
    if showcase is None:
        # Defensive — Share should always carry a Showcase.
        return None
    display_items = build_share_display_items(session, showcase, search)
    # v3.31.0 — surface the curated list's headline total value. Sum of
    # the per-item finish-aware ``total_value`` already computed in the
    # sanitized projection (price × displayed-available).
    total_value = sum(it["total_value"] for it in display_items)
    return {
        "share": share,
        "showcase": showcase,
        "sharer": share.user,
        "playgroup": share.playgroup,
        "items": display_items,
        "total_value": total_value,
    }


# ── The sanitized projection — privacy by construction (§8) ────


# The whitelist of fields the rendered share view may surface from
# Card (public Scryfall data, fine to expose) and InventoryRow
# (printing facts only — no location/drawer/slot/notes/role/etc.). A
# field absent from this projection CANNOT leak; the verification
# gate's privacy hard-flag asserts the rendered HTML carries no marker
# derived from any field outside this whitelist.
_SHARE_CARD_FIELDS = (
    "id",
    "name",
    "set_code",
    "set_name",
    "collector_number",
    "rarity",
    "image_url",
    "type_line",
    "oracle_text",
    "mana_cost",
    "cmc",
    "colors",
    "color_identity",
    "price_usd",
    "price_usd_foil",
    "price_usd_etched",
    "legalities",
)


def build_share_display_items(
    session: Session,
    showcase: Showcase,
    search: str = "",
) -> list[dict]:
    """Return the sanitized projection of a Showcase's items for shared view.

    Each item carries ONLY whitelisted fields per §8 of the spec:
    - From :class:`~app.models.Card`: name, set_code, collector_number,
      image_url, rarity, type_line, mana_cost, colors, color_identity,
      cmc, oracle_text, prices, legalities — public Scryfall data.
    - From :class:`~app.models.InventoryRow`: finish, language, is_proxy
      — printing facts.
    - Computed: ``available = min(quantity_offered, InventoryRow.quantity)``
      (decision A4 — the displayed available; no stored quantity to
      drift when the sharer sells); ``effective_price`` finish-aware
      via :func:`app.pricing.effective_price`; ``total_value`` = price
      × available.

    NEVER in projection (privacy hard-flag — see §8): InventoryRow
    ``notes``, ``tags``, ``role``, ``is_pending``,
    ``storage_location_id``, ``drawer``, ``slot``, ``from_drawer``,
    ``from_slot``, ``created_at``, ``updated_at``, ``user_id``;
    ShowcaseItem ``notes``.

    Reuses ``inventory_card`` in the rendered template with all action
    flags off; the projection's shape mirrors ``inventory_card``'s
    expected interface (``item.card``, ``item.finish``,
    ``item.language``, ``item.is_proxy``, ``item.quantity`` set to the
    computed available so the macro renders the right number,
    ``item.effective_price``, ``item.total_value``). The macro's
    ``{% elif item.slot %}`` branch is skipped because the projection
    does not populate ``slot``; ``show_collection_actions=False`` and
    ``show_deck_actions=False`` keep every action-form branch out of
    the rendered output.

    Defensive: items whose ``inventory_row`` resolves to None at
    render time (dangling FK — should be impossible after §9
    cleanup, defense in depth) are silently skipped from the list.
    """
    items_q = _query_showcase_items(session, showcase.id, search)
    display: list[dict] = []
    for it in items_q:
        inv = it.inventory_row
        if inv is None:
            continue
        card = inv.card
        if card is None:
            continue
        # The card projection is a plain dict with only the
        # whitelisted attributes. We deliberately do NOT pass the
        # ORM ``Card`` object through — that would let a future
        # template change accidentally reach a non-whitelisted
        # attribute. The macro reads ``item.card.<field>`` so the
        # dict-form needs attribute-style access; use a thin namespace
        # wrapper.
        card_proj = _ReadOnlyCardProjection(card)
        # Available — the only InventoryRow quantity surfaced.
        available = max(0, min(it.quantity_offered, inv.quantity))
        price = effective_price(card, inv.finish) or 0.0
        total = price * available
        display.append(
            {
                "id": it.id,  # ShowcaseItem id — surface-internal only
                "card": card_proj,
                "finish": inv.finish,
                "language": inv.language or "en",
                "is_proxy": bool(inv.is_proxy),
                # ``quantity`` is the displayed-available — the macro
                # reads ``item.quantity`` to render the number; we
                # NEVER expose the raw InventoryRow.quantity here.
                "quantity": available,
                "effective_price": price,
                "total_value": total,
                # Deliberately NOT set: drawer_label, slot, role,
                # is_pending, notes, tags, storage_location_id,
                # from_drawer, from_slot, created_at, updated_at,
                # user_id, ShowcaseItem.notes. Absent = unleakable.
            }
        )
    return display


class _ReadOnlyCardProjection:
    """A whitelisting attribute proxy around :class:`~app.models.Card`.

    The macro accesses ``item.card.<field>`` directly. To prevent any
    non-whitelisted Card column from accidentally being surfaced (e.g.
    in a future template change that reads ``item.card.notes``), this
    wrapper exposes ONLY the fields listed in :data:`_SHARE_CARD_FIELDS`.
    Accessing any other attribute raises ``AttributeError`` — fails
    loudly rather than silently leaking.
    """

    __slots__ = ("_data",)

    def __init__(self, card: Card) -> None:
        self._data = {field: getattr(card, field, None) for field in _SHARE_CARD_FIELDS}

    def __getattr__(self, name: str):
        try:
            return self._data[name]
        except KeyError as err:
            raise AttributeError(
                f"Field {name!r} is not in the sanitized share projection; "
                "see app/share_service.py:_SHARE_CARD_FIELDS for the whitelist."
            ) from err


__all__ = [
    "add_rows_to_showcase",
    "add_showcase_item",
    "build_share_display_items",
    "create_share",
    "create_showcase",
    "delete_showcase",
    "get_or_create_showcase",
    "get_share_view",
    "get_showcase",
    "get_showcase_with_items",
    "list_my_shares",
    "list_shares_for_playgroup",
    "list_showcases",
    "remove_showcase_item",
    "revoke_share",
    "update_quantity_offered",
    "update_showcase",
]
