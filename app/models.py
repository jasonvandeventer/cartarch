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

    inventory_rows: Mapped[list[InventoryRow]] = relationship(back_populates="user")
    decks: Mapped[list[Deck]] = relationship(back_populates="user")
    import_batches: Mapped[list[ImportBatch]] = relationship(back_populates="user")
    transaction_logs: Mapped[list[TransactionLog]] = relationship(back_populates="user")
    storage_locations: Mapped[list[StorageLocation]] = relationship(back_populates="user")


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
