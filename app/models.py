"""SQLAlchemy models for Mana Archive.

Cards are global reference data. Inventory, decks, imports, audit logs, and
storage locations are user-owned and must be queried through user_id.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.timeutil import utc_now


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    deck_view_mode: Mapped[str] = mapped_column(String(16), default="grid", nullable=False)
    deck_group_by: Mapped[str] = mapped_column(String(16), default="type", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    # v3.27.4 — replaces the misleading "last activity" proxy on the Admin
    # page (which was `func.max(TransactionLog.created_at)`, i.e. last
    # inventory event — users who only play games / edit decks / log in
    # showed stale dates). Set by POST /login on every successful auth.
    # NULL until next login for existing users (no backfill: the proxy
    # data is semantically different and copying it under the new name
    # would import the same misleading signal).
    last_signed_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    inventory_rows: Mapped[list[InventoryRow]] = relationship(back_populates="user")
    decks: Mapped[list[Deck]] = relationship(back_populates="user")
    import_batches: Mapped[list[ImportBatch]] = relationship(back_populates="user")
    transaction_logs: Mapped[list[TransactionLog]] = relationship(back_populates="user")
    storage_locations: Mapped[list[StorageLocation]] = relationship(back_populates="user")
    watchlist_items: Mapped[list[WatchlistItem]] = relationship(back_populates="user")
    password_reset_tokens: Mapped[list[PasswordResetToken]] = relationship(back_populates="user")
    # v3.29.0 — plain relationship; no cascade from User. The admin
    # user-deletion path handles cleanup explicitly via
    # ``playgroup_service.handle_user_deletion`` (transfers owned playgroups
    # to the longest-tenured remaining member; auto-deletes sole-member
    # playgroups) followed by a plain DELETE of the membership rows.
    playgroup_memberships: Mapped[list[PlaygroupMember]] = relationship(
        foreign_keys="PlaygroupMember.user_id"
    )
    # v3.29.1 — a user's curated Showcases. No cascade from User; the
    # admin user-deletion path explicitly DELETEs Share, Showcase, and
    # (via cascade="all, delete-orphan" on Showcase.items) ShowcaseItem
    # rows in ``app/routes/admin.py:delete_user`` to guarantee the
    # outcome regardless of SQLite's PRAGMA foreign_keys posture.
    # v3.30.12 — back_populates pairs this with Showcase.user so
    # SQLAlchemy knows the two relationships address the same FK
    # (showcases.user_id) and won't issue the "writing the same FK from
    # two relationships" SAWarning at mapper-configure time.
    # v3.31.0 — multi-showcase: the UNIQUE(user_id) constraint is
    # dropped, so this is now a one-to-many collection (was uselist=False
    # under the v3.29.1 decision A5 one-per-user cap).
    showcases: Mapped[list[Showcase]] = relationship(back_populates="user")


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    scryfall_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    set_code: Mapped[str] = mapped_column(String(32), index=True)
    set_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    collector_number: Mapped[str] = mapped_column(String(32), index=True)
    rarity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    type_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    oracle_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_usd: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_usd_foil: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_usd_etched: Mapped[str | None] = mapped_column(String(32), nullable=True)
    colors: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mana_cost: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cmc: Mapped[float | None] = mapped_column(Float, nullable=True)
    legalities: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Scryfall-only printing traits the drawer sorter needs. NULL = not yet
    # fetched (live-fetch fallback); populated by every card-write path so
    # the sorter needs zero network calls once backfilled.
    full_art: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    frame_effects: Mapped[str | None] = mapped_column(Text, nullable=True)
    set_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    layout: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # v3.36.1 — planeswalker starting loyalty / Battle defense. Faithful
    # raw Scryfall strings (can be non-numeric, e.g. loyalty "X"); NULL on
    # cards that have neither. Dormant payload data for the goldfish
    # loyalty/defense auto-init (Step 4). Part of the scryfall_cards seam.
    loyalty: Mapped[str | None] = mapped_column(String(16), nullable=True)
    defense: Mapped[str | None] = mapped_column(String(16), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    inventory_rows: Mapped[list[InventoryRow]] = relationship(back_populates="card")
    transaction_logs: Mapped[list[TransactionLog]] = relationship(back_populates="card")


class StorageLocation(Base):
    __tablename__ = "storage_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), index=True)
    type: Mapped[str] = mapped_column(String(64), default="other", index=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id"), nullable=True, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    mode: Mapped[str] = mapped_column(String(16), default="managed", nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    user: Mapped[User] = relationship(back_populates="storage_locations")
    parent: Mapped[StorageLocation | None] = relationship(
        remote_side="StorageLocation.id",
        back_populates="children",
    )
    children: Mapped[list[StorageLocation]] = relationship(back_populates="parent")
    inventory_rows: Mapped[list[InventoryRow]] = relationship(back_populates="storage_location")


class InventoryRow(Base):
    __tablename__ = "inventory_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    storage_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id"), nullable=True, index=True
    )
    finish: Mapped[str] = mapped_column(String(32), default="normal", index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    drawer: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    slot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_pending: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True, default="en", index=True)
    is_proxy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    from_drawer: Mapped[str | None] = mapped_column(String(32), nullable=True)
    from_slot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    user: Mapped[User] = relationship(back_populates="inventory_rows")
    card: Mapped[Card] = relationship(back_populates="inventory_rows")
    storage_location: Mapped[StorageLocation | None] = relationship(back_populates="inventory_rows")


class Deck(Base):
    __tablename__ = "decks"
    # DELIBERATE DOCUMENTED DELTA (Gate #4): prod's schema carries a legacy
    # ``CREATE UNIQUE INDEX ix_decks_name ON decks(name)`` — global-unique deck
    # name, a single-user-era artifact. The correct multi-user scope is per-user
    # unique, which this constraint encodes; prod's globally-unique data trivially
    # satisfies the looser predicate (zero migration risk). This is the roadmap's
    # "v4 table rebuild drops the legacy decks.name auto-index" cleanup landing —
    # post-cutover it lets the v3.30.18/v3.30.20 cross_user_deck_conflict
    # workarounds be removed.
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_decks_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    storage_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), index=True)
    format: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent_pod: Mapped[str | None] = mapped_column(String(16), nullable=True)
    intent_speed: Mapped[str | None] = mapped_column(String(16), nullable=True)
    intent_combo: Mapped[str | None] = mapped_column(String(16), nullable=True)
    intent_winning: Mapped[str | None] = mapped_column(String(16), nullable=True)
    intent_played: Mapped[str | None] = mapped_column(String(16), nullable=True)
    blurb: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    # v3.33.0 — optional link into a "variant group": a family of builds of the
    # same deck (e.g. Atraxa v1 / v2) that SHARE one physical copy of many cards.
    # Accounting-only overlay — one physical card still lives in exactly ONE
    # deck's location; this never duplicates rows or spans locations. It only
    # lets deck-import reconciliation treat a card held by a sibling variant
    # deck as "covered" (no new copy needed). NULL = standalone deck (legacy +
    # default). ``ondelete="SET NULL"`` documents v4 Postgres intent; SQLite
    # doesn't enforce it (PRAGMA foreign_keys OFF), so delete_variant_group +
    # the admin user-deletion cascade null/remove referencing rows explicitly.
    variant_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("variant_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # v3.37.0 — Brew Mode. Marks a deck as a "brew" (a deck built from cards the
    # user may not own, for planning/testing). When set, the add-card path flags
    # an unowned add as a proxy row so it never pollutes owned totals, and the
    # deck detail shows an owned/missing buy-list. Declared BOOLEAN and queried
    # ONLY through the ORM (``Deck.is_brew`` / ``.is_(True)``) — zero raw SQL
    # against this column, so pgloader's default BOOLEAN→boolean map is correct
    # at v4 with no cast-file entry (the v7/v8 blueprint boolean lesson).
    is_brew: Mapped[bool] = mapped_column(default=False)

    storage_location: Mapped[StorageLocation | None] = relationship()
    user: Mapped[User] = relationship(back_populates="decks")
    variant_group: Mapped[VariantGroup | None] = relationship(back_populates="decks")


class VariantGroup(Base):
    __tablename__ = "variant_groups"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_variant_groups_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    user: Mapped[User] = relationship()
    decks: Mapped[list[Deck]] = relationship(back_populates="variant_group")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="import_batches")
    transaction_logs: Mapped[list[TransactionLog]] = relationship(back_populates="batch")


class TransactionLog(Base):
    __tablename__ = "transaction_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id"), nullable=True)
    finish: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quantity_delta: Mapped[int] = mapped_column(Integer, default=0)
    source_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    destination_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id"), nullable=True, index=True
    )
    inventory_row_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="transaction_logs")
    card: Mapped[Card | None] = relationship(back_populates="transaction_logs")
    batch: Mapped[ImportBatch | None] = relationship(back_populates="transaction_logs")


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    # v3.39.x (gate #5) — was NO ACTION + NOT NULL. The gate-#5 parent-delete
    # harness proved a NO-ACTION ``user_id`` BLOCKS ``DELETE FROM users`` under FK
    # enforcement: deleting any user who recorded a game crashed the whole
    # deletion (v4 cutover) and orphaned ``games.user_id`` under SQLite (prod
    # today). Now ``ondelete="SET NULL"`` (column made nullable) — consistent with
    # ``GameSeat.user_id``: the game survives as shared history, its recorder ref
    # nulled. ``user_name_at_game`` (below) snapshots the recorder's display name
    # so the read-only game banner stays attributed instead of degrading to
    # "another player" (mirrors ``GameSeat.user_name_at_game``). ``delete_user``
    # re-snapshots then nulls explicitly (SQLite enforces nothing — the clause is
    # v4 defense-in-depth). gate-#5 verified (parent-delete harness, 2026-06-19).
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # v3.39.x (gate #5) — durable snapshot of the recording user's display name,
    # populated by ``delete_user`` right before it nulls ``user_id`` (and re-snapshot
    # safe to set at create time too). NULL = recorder still live (read through the
    # ``game.user`` relationship) OR a legacy game predating this column. Mirrors
    # ``GameSeat.user_name_at_game`` exactly.
    user_name_at_game: Mapped[str | None] = mapped_column(Text, nullable=True)
    played_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    # v3.27.2 — service-layer enum (CANONICAL_GAME_FORMATS in game_service.py).
    # Column stays nullable=True at the DB level because SQLite can't alter
    # NULL→NOT NULL on an existing column without a table rebuild (reserved
    # for v4). The Python-side default + game_create's normalize_game_format
    # validation ensure new rows always carry a canonical value; the v3.27.2
    # migration backfills existing rows to canonical values too. NULL is
    # effectively unreachable after migration but the column type permits it.
    format: Mapped[str | None] = mapped_column(String(64), nullable=True, default="Commander")
    # v3.27.3 — service-layer enum (CANONICAL_GAME_STATUSES in game_service.py).
    # Replaces the brittle "any seat has placement → game is finalized"
    # derivation that lived in game_detail.html line 3. Column nullable=True
    # at the DB level (additive ALTER under SQLite-until-v4 can't tighten
    # nullability without table rebuild); Python-side default + service-
    # layer setters (create_game → "created"; end_game → "finalized")
    # ensure new rows always carry a canonical value, and the v3.27.3
    # migration backfills existing rows.
    status: Mapped[str | None] = mapped_column(String(32), nullable=True, default="created")
    turn_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seat_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # v3.27.0 — collision-proof localStorage key for the game tracker.
    # Server-generated once at create time (secrets.token_urlsafe(8)); never
    # regenerated; NEVER added to the localStorage-saved state blob (key-only,
    # so gameFingerprint() stays unchanged — same rationale as
    # first_seat_number above). NULL = legacy game predating this fix; client
    # falls back to the bare ``mana-game-${gameId}`` key.
    client_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    # v3.33.2 — wall-clock end timestamp, stamped once by end_game when the
    # game is finalized. NULL = never finalized OR a legacy game predating this
    # column (the game-summary view shows "—" for elapsed in that case; no
    # backfill — past durations are unrecoverable). Elapsed playtime is
    # rendered as ``ended_at − played_at`` (played_at ≈ when live play started).
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # v3.32.0 — optional playgroup link for shared game visibility. A game
    # is viewable by its owner (user_id), by any user attributed to one of
    # its seats (GameSeat.user_id), AND — when this is set — by every member
    # of the linked playgroup. NULL = private to owner + seat-attributed
    # players only (legacy games and games created without a playgroup pick).
    # ``ondelete="SET NULL"`` documents v4 Postgres intent; SQLite doesn't
    # enforce it (PRAGMA foreign_keys OFF), so playgroup_service.delete_playgroup
    # nulls these explicitly. A dangling id is access-safe regardless: the
    # membership check returns nobody once the playgroup's member rows are gone.
    playgroup_id: Mapped[int | None] = mapped_column(
        ForeignKey("playgroups.id", ondelete="SET NULL"), nullable=True, index=True
    )

    seats: Mapped[list[GameSeat]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        order_by="GameSeat.seat_number",
    )
    user: Mapped[User] = relationship()
    playgroup: Mapped[Playgroup | None] = relationship()


class GameSeat(Base):
    __tablename__ = "game_seats"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), nullable=False, index=True)
    seat_number: Mapped[int] = mapped_column(Integer, nullable=False)
    player_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # ``ondelete="SET NULL"`` documents v4 Postgres intent: a seat (game history)
    # outlives a later-deleted deck — the deck ref nulls, the seat persists. SQLite
    # doesn't enforce it (PRAGMA foreign_keys OFF). Gate #4 orphan audit (id 40).
    deck_id: Mapped[int | None] = mapped_column(
        ForeignKey("decks.id", ondelete="SET NULL"), nullable=True
    )
    placement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    starting_life: Mapped[int] = mapped_column(Integer, default=40, nullable=False)
    final_life: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grid_position: Mapped[str | None] = mapped_column(String(4), nullable=True)
    # v3.27.5 — seat→user attribution. Two-column design mirrors v3.27.1's
    # deck-identity snapshot (live FK + analytics-stable snapshot).
    # ``user_id`` is the live navigational link; ``ondelete="SET NULL"`` is
    # declared for documentation + v4 Postgres forward-compat, but SQLite
    # doesn't enforce it (PRAGMA foreign_keys is OFF project-wide). The
    # cascade is enforced explicitly in the admin user-deletion path —
    # see ``delete_user`` in ``app/routes/admin.py``. ``user_name_at_game``
    # is captured at game creation and SURVIVES account deletion (the
    # whole point of the snapshot).
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_name_at_game: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v3.27.0b-1 — deck identity captured at game creation. Analytics read
    # these instead of joining through the live ``deck_id`` FK (which mutates
    # whenever a deck is edited or deleted). The FK stays in place for "what
    # deck was this?" navigation; the snapshots are the analytics truth.
    # NULL = no deck assigned at seat creation, or legacy seat predating this
    # column. commander_name_at_game joins multi-commander pairs with " + "
    # (Partner / Background / Friends Forever, capped at 2 — mirrors
    # get_seat_commander_image_urls' two-URL cap).
    deck_name_at_game: Mapped[str | None] = mapped_column(Text, nullable=True)
    commander_name_at_game: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v3.26.6 — per-seat opt-out for the v3.26.1 commander art panel background.
    # ``server_default=false()`` is portable: renders ``DEFAULT 0`` on SQLite (matching
    # the ALTER TABLE DEFAULT 0 the migration applied) and ``DEFAULT false`` on Postgres.
    # A literal ``text("0")`` breaks ``CREATE TABLE`` on PG (boolean column can't default
    # to integer 0) — caught by the Phase-E dual-backend suite run, 2026-06-18.
    art_background_hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    game: Mapped[Game] = relationship(back_populates="seats")
    deck: Mapped[Deck | None] = relationship()


class TokenInventory(Base):
    """Per-user physical token holdings (Pest x12, Treasure x30, etc.).

    Separate from InventoryRow so resort_collection / drawer-sorter logic
    doesn't try to organize tokens.
    """

    __tablename__ = "token_inventory"

    id: Mapped[int] = mapped_column(primary_key=True)
    # ``ondelete="CASCADE"`` recovers a prod raw-SQL invariant the ORM omitted
    # (the v3.x token_inventory migration created this FK with ON DELETE CASCADE).
    # Matches the explicit admin user-deletion cleanup. SQLite doesn't enforce it
    # (foreign_keys OFF). gate-#5 verified (parent-delete harness, 2026-06-19).
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), index=True)
    type_line: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    set_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    collector_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scryfall_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_double_sided: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    back_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    back_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    back_set_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    back_collector_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # ``ondelete="SET NULL"`` recovers a prod raw-SQL invariant the ORM omitted
    # (the migration created this FK with ON DELETE SET NULL — a deleted location
    # nulls the token's placement, keeps the token). SQLite doesn't enforce it
    # (foreign_keys OFF). gate-#5 verified — no parent-delete entrypoint exercises this FK (harness coverage-gate allow-list); the clause is v4 defense-in-depth.
    storage_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    storage_location: Mapped[StorageLocation | None] = relationship()


class DeckTokenRequirement(Base):
    """A deck's declared need for a token type (Pest x10, Food x8, etc.).

    May reference an exact TokenInventory row via token_inventory_id, or be
    a loose name-only requirement when the user doesn't yet own the token.
    """

    __tablename__ = "deck_token_requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    # ``ondelete="CASCADE"`` documents v4 Postgres intent: a token requirement is
    # meaningless without its deck and dies with it. nullable=False rules out SET
    # NULL; CASCADE also fixes the latent delete_deck bug (deck_service.py) where
    # these rows are not cleaned up. SQLite doesn't enforce it (foreign_keys OFF).
    deck_id: Mapped[int] = mapped_column(
        ForeignKey("decks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # ``ondelete="SET NULL"`` recovers a prod raw-SQL invariant the ORM omitted
    # (the migration created this FK with ON DELETE SET NULL — deleting the owned
    # token leaves the requirement as a loose name-only need). SQLite doesn't
    # enforce it (foreign_keys OFF). gate-#5 verified — delete_token nulls this ref explicitly; no parent-delete harness cell (token_inventory has no app delete entrypoint). v4 defense-in-depth.
    token_inventory_id: Mapped[int | None] = mapped_column(
        ForeignKey("token_inventory.id", ondelete="SET NULL"), nullable=True
    )
    token_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity_needed: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    token_inventory: Mapped[TokenInventory | None] = relationship()


class WatchlistItem(Base):
    """A user's watchlist entry — a card they want to track.

    v3.27.12. Two identity modes, XOR-shaped:

    - ``card_id`` set, ``card_name`` NULL: a printing-specific watch.
      References a single Scryfall printing via ``cards.id``. Useful
      for collectors after a specific border / promo / set version.
    - ``card_id`` NULL, ``card_name`` set: a printing-agnostic watch.
      Matches any printing whose ``Card.name`` equals the stored
      canonical name. Useful for the more common "I want a Sol Ring"
      mental model.

    Exactly one of ``card_id`` / ``card_name`` is populated per row —
    enforced at the service layer in ``app/watchlist_service.py`` (the
    project convention from v3.10.6 / v3.27.2 for free-text validation;
    SQLite ``CHECK`` constraints stay out of the schema to preserve the
    SQLite-until-v4 no-rebuild constraint). Two partial-unique indexes
    in the v3.27.12 migration enforce one-row-per-identity per user.

    ``card_id`` is a nominal FK to ``cards.id``; SQLite's
    ``PRAGMA foreign_keys`` defaults OFF and the project doesn't turn
    it on, so the FK declaration is documentary + v4-Postgres
    forward-compat (same pattern as the v3.27.5 ``GameSeat.user_id``
    FK). Card deletion is essentially never observed in production
    (the ``cards`` table is shared and append-only in practice), so
    the dangling-FK risk is theoretical. User deletion is handled
    explicitly by the cascade in ``routes/admin.py``.
    """

    __tablename__ = "watchlist"

    # Two PARTIAL unique indexes recovered from the prod schema (the v3.27.12
    # migration's ``uq_watchlist_user_card_*`` indexes the ORM never declared):
    # one-row-per-identity per user, enforced only on the populated side of the
    # card_id/card_name XOR (WHERE … IS NOT NULL). Both sqlite_where +
    # postgresql_where so the partial predicate emits on BOTH dialects. These are
    # correctness invariants (block duplicate watch entries), not niceties.
    # gate-#5 verified — encoding diffs-empty on both dialects (not a parent-delete FK).
    __table_args__ = (
        Index(
            "uq_watchlist_user_card_id",
            "user_id",
            "card_id",
            unique=True,
            sqlite_where=text("card_id IS NOT NULL"),
            postgresql_where=text("card_id IS NOT NULL"),
        ),
        Index(
            "uq_watchlist_user_card_name",
            "user_id",
            "card_name",
            unique=True,
            sqlite_where=text("card_name IS NOT NULL"),
            postgresql_where=text("card_name IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # ``ondelete=CASCADE`` on both FKs recovers prod raw-SQL invariants the ORM
    # omitted (delete user / card → drop their watch rows). Matches the explicit
    # admin user-deletion cleanup. SQLite doesn't enforce it. gate-#5 verified — user_id by the parent-delete harness (delete_user); card_id is defense-in-depth (cards are catalog; no app entrypoint deletes a Card).
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    card_id: Mapped[int | None] = mapped_column(
        ForeignKey("cards.id", ondelete="CASCADE"), nullable=True
    )
    card_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v3.28.11 — optional buy-target. When the watched card's current
    # price drops to or below target_price, the watchlist row gets a
    # "target met" highlight on /watchlist. Independent of the
    # card_id / card_name XOR — allowed on either identity mode; the
    # comparison basis differs (printing-specific finish min vs name's
    # lowest-across-printings). Stored as REAL (SQLite float) because
    # this is user-entered numeric input, not a Scryfall wire-format
    # round-trip the way Card.price_usd is.
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    user: Mapped[User] = relationship(back_populates="watchlist_items")
    card: Mapped[Card | None] = relationship()


class PasswordResetToken(Base):
    """A self-service password reset token (v3.27.14).

    The raw token (a ``secrets.token_urlsafe(32)`` value) is NEVER
    stored — only ``hashlib.sha256(token).hexdigest()`` lives in
    ``token_hash``. The raw token exists only in the emailed link.
    Validation hashes the incoming token and looks up by hash.

    SHA-256 is the correct choice here (NOT a slow password hasher
    like the one in ``app/auth.py:hash_password``) because the token
    is high-entropy random data, not a low-entropy user secret. A
    slow hash would just make every verification slower for no
    security gain.

    Lifecycle is enforced at the service layer in
    ``app/password_reset_service.py``:

    - 30-minute lifetime: ``expires_at = created_at + 30min`` at
      insert time; validation checks ``expires_at > now()``.
    - Single-use: ``used_at`` is set on successful reset; rows with
      ``used_at IS NOT NULL`` never validate again.
    - Invalidate-on-new-request: a new reset request DELETEs the
      user's existing unused tokens before inserting the new one,
      so there's at most one outstanding token per user at any
      moment.

    ``user_id`` is a documentary FK only (project doesn't enable
    ``PRAGMA foreign_keys``). User deletion is handled explicitly by
    the cascade in ``app/routes/admin.py:delete_user`` — plain DELETE,
    no historical retention value (no "X reset Y's password"
    snapshot to preserve).
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    # ``ondelete="CASCADE"`` recovers a prod raw-SQL invariant the ORM omitted
    # (the v3.27.14 migration created this FK with ON DELETE CASCADE — a deleted
    # user's reset tokens die with them, no retention value). Matches the explicit
    # admin user-deletion cleanup. SQLite doesn't enforce it. gate-#5 verified (parent-delete harness, 2026-06-19).
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    user: Mapped[User] = relationship(back_populates="password_reset_tokens")


class Playgroup(Base):
    """A membership-based grouping of users — the substrate for the
    v3.29.x social features (v3.29.1 sharing, v3.29.2 trading). Opens
    the v3.29.x minor.

    NOT the planned v4 multi-tenancy ``playgroup_id`` / ``org_id``
    scope. A v3.29.0 ``Playgroup`` is a *social grouping* (who you
    play / share / trade with); a user belongs to many, membership is
    fluid, it owns no data. The v4 tenancy scope is a *data-isolation
    boundary*. The names collide; the entities are distinct. Recorded
    as a v4-schema-design input in ``roadmap.md`` — v4 design settles
    whether to reuse the entity or introduce a separate ``Tenant``
    above it. Do not pre-decide here.

    **Authority rule.** ``Playgroup.created_by`` is immutable audit
    (who originally made it) and **never** the live authority check.
    Live authority is ``PlaygroupMember.role == "owner"``. After an
    ownership transfer the two diverge. Every permission check reads
    ``role``, never ``created_by``.

    Join-code-only invite model (v3.29.0). The opaque ``join_code``
    is generated server-side at creation via ``secrets.token_urlsafe``;
    NULL = disabled (the owner toggled the code off). Any member can
    view and share the code; only the owner may regenerate or
    disable it. Email invites are deferred — when taken up, they
    will carry the v3.27.14 / v3.27.17 enumeration-oracle defense as
    a hard-flag requirement.
    """

    __tablename__ = "playgroups"

    # PARTIAL unique index recovered from the prod schema (the v3.29.0
    # ``uq_playgroups_join_code`` index the ORM never declared): join codes are
    # globally unique among ENABLED codes (WHERE join_code IS NOT NULL); NULL =
    # disabled and may repeat. Both sqlite_where + postgresql_where so the partial
    # predicate emits on BOTH dialects. Correctness invariant — without it, two
    # playgroups could share a code and a join would be ambiguous. gate-#5 verified — encoding diffs-empty on both dialects (not a parent-delete FK).
    __table_args__ = (
        Index(
            "uq_playgroups_join_code",
            "join_code",
            unique=True,
            sqlite_where=text("join_code IS NOT NULL"),
            postgresql_where=text("join_code IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Immutable audit — "who made it". NOT the authority check; see role.
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    join_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    members: Mapped[list[PlaygroupMember]] = relationship(
        back_populates="playgroup", cascade="all, delete-orphan"
    )
    creator: Mapped[User] = relationship(foreign_keys=[created_by])


class Showcase(Base):
    """A user's curated subset of their own inventory, prepared for sharing.

    v3.29.1. Originally one per user (``UniqueConstraint(user_id)``); the
    model was deliberately general so a future multi-showcase release
    could drop the constraint with no other change, and so v3.29.2
    trading may reuse it as a "haves" list. v3.31.0 dropped that
    constraint — a user may now keep several Showcases for different
    purposes. A Showcase is NOT a ``StorageLocation type="binder"``
    (a physical container). It is a logical curated list — cards can be
    in it without being physically moved.

    **Showcase ≠ Share.** The Showcase is the prepared curation; a
    :class:`Share` is one act of exposing this Showcase to one playgroup,
    read-only. Revoking a Share hard-deletes the Share row; the Showcase
    it pointed at is untouched. This separation is the whole point of the
    two-table split.

    Items live in :class:`ShowcaseItem`, cascade-deleted with the
    Showcase.
    """

    __tablename__ = "showcases"
    # v3.31.0 — multi-showcase: the v3.29.1 UNIQUE(user_id) constraint
    # is gone (dropped in migrate_v3_31_0_multi_showcase). A user may
    # have any number of Showcases.

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="My Showcase")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    items: Mapped[list[ShowcaseItem]] = relationship(
        back_populates="showcase", cascade="all, delete-orphan"
    )
    # v3.30.12 — back_populates pairs this with User.showcases. Closes
    # the v3.29.1 ORM-config gap that surfaced as the "Showcase.user
    # will copy column users.id to column showcases.user_id, which
    # conflicts with relationship(s)" SAWarning at mapper-configure.
    user: Mapped[User] = relationship(back_populates="showcases")


class ShowcaseItem(Base):
    """One curated card in a :class:`Showcase`. References an InventoryRow.

    v3.29.1. ``inventory_row_id`` is the identity key (decision A3 — the
    Showcase NEVER forks or copies inventory; InventoryRow stays the
    single source of truth). ``quantity_offered`` is the sharer's intent;
    the displayed available quantity in the shared view is computed at
    render time as ``min(quantity_offered, InventoryRow.quantity)`` — no
    stored quantity to drift when the sharer sells from inventory.

    ``notes`` is **sharer-private**: it is the one field on this table
    that MUST NEVER appear in the sanitized share projection (§8 of the
    v3.29.1 spec). The privacy hard-flag verification in the test suite
    asserts that no rendered share-view HTML contains a marker derived
    from this column.

    ``UniqueConstraint(showcase_id, inventory_row_id)`` keeps the
    curated set a true set; ``add_showcase_item`` in
    ``app/share_service.py`` treats IntegrityError on this pair as a
    no-op (the v3.29.0 ``join_by_code`` idempotency pattern).

    ``inventory_row_id`` is a documentary FK only (project runs with
    ``PRAGMA foreign_keys`` OFF). InventoryRow-delete cleanup runs
    explicitly: ``inventory_service`` deletes the ShowcaseItem rows
    referencing the row BEFORE the row is deleted (§9 of the spec). A
    defensive read-skip in ``build_share_display_items`` handles the
    theoretical case where the link is dangling at render time.
    """

    __tablename__ = "showcase_items"
    __table_args__ = (
        UniqueConstraint("showcase_id", "inventory_row_id", name="uq_showcase_items_showcase_inv"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    showcase_id: Mapped[int] = mapped_column(ForeignKey("showcases.id"), nullable=False, index=True)
    # ``ondelete="CASCADE"`` documents v4 Postgres intent: matches v3.39.6
    # clean_inventory_row_references (deletes the showcase_item when its row goes).
    # nullable=False rules out SET NULL. SQLite doesn't enforce it (foreign_keys
    # OFF). gate-#5 verified (parent-delete harness, 2026-06-19).
    inventory_row_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_rows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quantity_offered: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    showcase: Mapped[Showcase] = relationship(back_populates="items")
    inventory_row: Mapped[InventoryRow | None] = relationship()


class Share(Base):
    """One act of exposing a :class:`Showcase` to one playgroup, read-only.

    v3.29.1. Ephemeral: revoking hard-deletes this row (decision B2).
    Public links are out of scope at v3.29.1 (decision B1 — playgroup-
    scoped only; the public-link path is deferred entirely until its own
    privacy review). One playgroup per Share (decision B3); a Showcase
    shared to N playgroups is N Share rows.

    Visibility is a direct ``PlaygroupMember`` filter on
    ``Share.playgroup_id`` (decision E2 — NOT ``co_members_of``, which
    would return everyone the sharer co-belongs with across other
    playgroups too; the visibility scope of a Share is the chosen
    playgroup specifically, not the sharer's social graph in general).

    ``user_id`` is denormalized for the "my shares" query and the admin
    user-deletion cascade. ``UniqueConstraint(showcase_id, playgroup_id)``
    prevents double-sharing the same Showcase to the same playgroup;
    ``create_share`` in ``app/share_service.py`` returns the existing
    Share when the constraint trips.

    Playgroup-lifecycle cleanup is wired in ``app/playgroup_service.py``
    (§9 of the spec): ``delete_playgroup`` deletes all shares targeting
    that playgroup; ``leave_playgroup`` and ``remove_member`` delete the
    departing user's shares targeting that playgroup. The
    ``Showcase`` itself is not touched by playgroup deletion.
    """

    __tablename__ = "shares"
    __table_args__ = (
        UniqueConstraint("showcase_id", "playgroup_id", name="uq_shares_showcase_playgroup"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    showcase_id: Mapped[int] = mapped_column(ForeignKey("showcases.id"), nullable=False, index=True)
    playgroup_id: Mapped[int] = mapped_column(
        ForeignKey("playgroups.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    user: Mapped[User] = relationship()
    showcase: Mapped[Showcase] = relationship()
    playgroup: Mapped[Playgroup] = relationship()


class PlaygroupMember(Base):
    """user ↔ playgroup membership. The codebase's first explicit M2M.

    Surrogate primary key + ``UniqueConstraint(playgroup_id, user_id)``
    rather than a composite PK — keeps SQLAlchemy ergonomics simple
    and matches every other join-bearing table in the schema (no
    model in this file uses a composite PK today; ``GameSeat``,
    ``DeckTokenRequirement`` etc. all use surrogate + uniqueness on
    the FK pair when needed).

    ``role`` is a service-layer canonical enum
    (``CANONICAL_PLAYGROUP_ROLES`` in ``app/playgroup_service.py``) —
    the v3.27.2 / v3.27.3 pattern, no DB ``CHECK`` constraint (would
    require table rebuild, reserved for v4 Postgres). v3.29.0 ships
    two roles, ``owner`` and ``member``; the enum can widen
    additively later (e.g. to add ``admin``) with no schema change.

    No ``invited_by`` column at v3.29.0 — under join-code-only it
    would be uniformly NULL. Returns if/when email invites ship and
    a real invite-audit trail becomes meaningful.
    """

    __tablename__ = "playgroup_members"
    __table_args__ = (
        UniqueConstraint("playgroup_id", "user_id", name="uq_playgroup_members_pg_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    playgroup_id: Mapped[int] = mapped_column(
        ForeignKey("playgroups.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), default="member", nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    playgroup: Mapped[Playgroup] = relationship(back_populates="members")
    user: Mapped[User] = relationship(foreign_keys=[user_id], overlaps="playgroup_memberships")


class Trade(Base):
    """A pairwise card trade between two playgroup co-members.

    v3.29.2 — the third and final release of the v3.29.x social-features
    minor. Recording-only: the Trade records the agreement; it never
    moves InventoryRow. Inventory execution is deferred (v4-gated;
    v4-schema-design input).

    **Lifecycle.** One non-terminal status (``proposed``) and four
    terminal statuses (``accepted``, ``declined``, ``cancelled``,
    ``abandoned``). Transitions are gated by the actor: the recipient
    accepts / declines; the proposer cancels; ``abandoned`` is system-
    only (the §10 cleanup hooks). The state machine is enforced in
    ``app.trade_service.transition_trade`` — there is no other code path
    that mutates ``status`` from a user action.

    **Hybrid identity reference.** ``TradeItem`` carries both live FKs
    (``inventory_row_id``, ``card_id``) and snapshot fields
    (``*_at_trade``). The live FKs let the construction / detail pages
    navigate to current InventoryRow + Card data during negotiation.
    The snapshots are written on every transition into a terminal
    status (decision A4) so the historical record survives later card-
    or inventory-row changes. The live FKs stay populated after
    terminal — they are nulled only when the underlying row is deleted
    (§10 cleanup).

    **Identity FKs are nullable for the SET-NULL pattern.**
    ``proposer_user_id`` / ``recipient_user_id`` / ``playgroup_id`` are
    all nullable at the DB level so the admin-cascade and playgroup-
    delete cleanup hooks can SET-NULL on terminal trades (preserving
    the historical record via the snapshot columns) and ORM-delete
    pending trades. At app level both user FKs are required at
    proposal time; ``playgroup_id`` is required at proposal time
    (decision D1).

    **Status / side enums are service-layer canonical** (no DB CHECK,
    matching the v3.27.2 / v3.27.3 / v3.29.0 pattern). The valid sets
    live in ``CANONICAL_TRADE_STATUSES`` / ``CANONICAL_TRADE_ITEM_SIDES``
    in ``app/trade_service.py``.
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable for SET-NULL on admin account deletion of terminal trades
    # (preserves the historical record via the *_name_at_trade snapshots).
    # Required at app layer at proposal time.
    proposer_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    recipient_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    # Nullable for SET-NULL on playgroup deletion of terminal trades.
    # Required at app layer at proposal time (decision D1).
    playgroup_id: Mapped[int | None] = mapped_column(
        ForeignKey("playgroups.id"), nullable=True, index=True
    )
    # Service-layer canonical enum (CANONICAL_TRADE_STATUSES). Python-side
    # default lands ``proposed`` on every new row; no DB CHECK (the v3.27.2
    # service-enum pattern, SQLite-until-v4 posture).
    status: Mapped[str] = mapped_column(String(32), default="proposed", nullable=False, index=True)
    proposer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipient_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Identity snapshots. NULL on a still-proposed trade; populated by
    # ``write_trade_terminal_snapshot`` on every terminal transition (and
    # by the cleanup helpers' ``abandon_*`` paths). Survives account
    # deletion of either party.
    proposer_name_at_trade: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipient_name_at_trade: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    # NULL while the trade is still ``proposed``; written on every terminal
    # transition. The single source of truth for "when did this close?".
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list[TradeItem]] = relationship(
        back_populates="trade", cascade="all, delete-orphan"
    )
    proposer: Mapped[User | None] = relationship(foreign_keys=[proposer_user_id])
    recipient: Mapped[User | None] = relationship(foreign_keys=[recipient_user_id])
    playgroup: Mapped[Playgroup | None] = relationship()


class TradeItem(Base):
    """One line item on one side of a :class:`Trade`.

    ``side`` is one of ``offered`` (proposer is giving) or ``requested``
    (proposer is asking for from the recipient). Service-layer canonical
    enum (``CANONICAL_TRADE_ITEM_SIDES`` in ``app/trade_service.py``).

    ``inventory_row_id`` is the live FK to the source InventoryRow —
    set on both sides at proposal time (offered: a row the proposer
    owns; requested: a row the recipient owns surfaced via their
    Showcase). It is nulled by the §10 inventory-row-delete cleanup
    when the underlying InventoryRow is deleted. ``card_id`` is the
    redundant live FK for the card itself (cheaper joins for
    rendering — InventoryRow already has card_id, but this avoids a
    second join hop on hot paths).

    ``showcase_item_id`` (decision C1) is the OPTIONAL link to the
    v3.29.1 ShowcaseItem this trade-item was selected from. v3.29.2
    requires it for every ``side='requested'`` row at proposal time
    (decision C2 — requested items must come from the recipient's
    shared Showcase); ``side='offered'`` rows leave it NULL. It is
    nulled if the underlying ShowcaseItem is removed (§10 — the
    showcase-item-remove hook). The trade continues against its
    ``inventory_row_id`` regardless; the showcase link is navigation
    metadata, not the identity.

    Five ``*_at_trade`` snapshot fields are the durable historical
    record (decision A4). NULL while the trade is still ``proposed``;
    populated on every terminal transition by
    ``write_trade_terminal_snapshot``. After terminal, the rendered
    detail pulls from snapshots so card edits / inventory deletes
    don't rewrite history.
    """

    __tablename__ = "trade_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), nullable=False, index=True)
    # Service-layer canonical enum (CANONICAL_TRADE_ITEM_SIDES). Indexed
    # for the composite (trade_id, side) per-side render query.
    side: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Live FK — nulled by §10 inventory-row-delete cleanup.
    # ``ondelete="SET NULL"`` documents v4 Postgres intent: matches v3.39.6
    # clean_inventory_row_references (NULLs the ref, preserves the trade record —
    # decision A4); nullable=True permits it. SQLite doesn't enforce it
    # (foreign_keys OFF). gate-#5 verified (parent-delete harness, 2026-06-19).
    inventory_row_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_rows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Live FK — redundant with InventoryRow.card_id but saves the join hop
    # on hot render paths. Documentary only (PRAGMA foreign_keys OFF).
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id"), nullable=True, index=True)
    # Decision C1 — nullable FK to the ShowcaseItem the requested item was
    # selected from. App-layer requires it for ``side='requested'`` at
    # proposal time (C2); ``side='offered'`` rows leave it NULL. Nulled by
    # §10 showcase-item-remove cleanup; trade continues against
    # inventory_row_id (the showcase link is navigation only).
    # ``ondelete="SET NULL"`` — sibling of inventory_row_id's SET NULL (decision A4):
    # the link is navigation-only (decision C1), so a deleted showcase_item nulls the
    # provenance ref and KEEPS the trade record. Without it, showcase_items'
    # inventory_row_id CASCADE delete is blocked by this NO-ACTION ref (surfaced in the
    # 2026-06-18 scripted-load rehearsal). SQLite doesn't enforce it. gate-#5 verified (parent-delete harness, 2026-06-19).
    showcase_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("showcase_items.id", ondelete="SET NULL"), nullable=True
    )
    finish: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Five ``*_at_trade`` snapshot fields (decision A4). NULL while trade
    # is still ``proposed``; populated on terminal transition.
    card_name_at_trade: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_set_code_at_trade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    card_collector_number_at_trade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    finish_at_trade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quantity_at_trade: Mapped[int | None] = mapped_column(Integer, nullable=True)

    trade: Mapped[Trade] = relationship(back_populates="items")
    inventory_row: Mapped[InventoryRow | None] = relationship()
    card: Mapped[Card | None] = relationship()
    showcase_item: Mapped[ShowcaseItem | None] = relationship()
