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
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="inventory_rows")
    card: Mapped[Card] = relationship(back_populates="inventory_rows")
    storage_location: Mapped[StorageLocation | None] = relationship(back_populates="inventory_rows")


class Deck(Base):
    __tablename__ = "decks"
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    storage_location: Mapped[StorageLocation | None] = relationship()
    user: Mapped[User] = relationship(back_populates="decks")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="transaction_logs")
    card: Mapped[Card | None] = relationship(back_populates="transaction_logs")
    batch: Mapped[ImportBatch | None] = relationship(back_populates="transaction_logs")


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    played_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    seats: Mapped[list[GameSeat]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        order_by="GameSeat.seat_number",
    )
    user: Mapped[User] = relationship()


class GameSeat(Base):
    __tablename__ = "game_seats"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), nullable=False, index=True)
    seat_number: Mapped[int] = mapped_column(Integer, nullable=False)
    player_name: Mapped[str] = mapped_column(String(128), nullable=False)
    deck_id: Mapped[int | None] = mapped_column(ForeignKey("decks.id"), nullable=True)
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
    # Stored as INTEGER 0/1 (SQLite's idiomatic boolean shape); SQLAlchemy
    # exposes it as bool via the Boolean type. server_default="0" matches the
    # ALTER TABLE DEFAULT 0 the migration applied.
    art_background_hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
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
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
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
    storage_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id"), nullable=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    storage_location: Mapped[StorageLocation | None] = relationship()


class DeckTokenRequirement(Base):
    """A deck's declared need for a token type (Pest x10, Food x8, etc.).

    May reference an exact TokenInventory row via token_inventory_id, or be
    a loose name-only requirement when the user doesn't yet own the token.
    """

    __tablename__ = "deck_token_requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    deck_id: Mapped[int] = mapped_column(ForeignKey("decks.id"), nullable=False, index=True)
    token_inventory_id: Mapped[int | None] = mapped_column(
        ForeignKey("token_inventory.id"), nullable=True
    )
    token_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity_needed: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id"), nullable=True)
    card_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
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
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

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

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Immutable audit — "who made it". NOT the authority check; see role.
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    join_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    members: Mapped[list[PlaygroupMember]] = relationship(
        back_populates="playgroup", cascade="all, delete-orphan"
    )
    creator: Mapped[User] = relationship(foreign_keys=[created_by])


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
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    playgroup: Mapped[Playgroup] = relationship(back_populates="members")
    user: Mapped[User] = relationship(foreign_keys=[user_id], overlaps="playgroup_memberships")
