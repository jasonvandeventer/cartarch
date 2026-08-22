"""SQLAlchemy models for Cartarch.

Cards are global reference data. Inventory, decks, imports, audit logs, and
storage locations are user-owned and must be queried through user_id.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
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
    true,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime
from app.timeutil import utc_now

# #172 — a GUEST is a real ``users`` row with an unusable password, not a second
# kind of identity. Every attribution surface in the app keys on ``user_id`` —
# the playgroup record (#152), deck game stats, #164's commander→deck resolution,
# #175's finalize capture — and ``decks.user_id`` is NOT NULL, so a seat with
# ``user_id IS NULL`` is a seat that records NOTHING. Minting an account is far
# less machinery than teaching every one of those surfaces a second identity,
# and it makes a guest's game history real rather than collapsing it into the
# single GUESTS_LABEL row.
#
# ``.invalid`` is RFC 2606 reserved: the address can never be registered or
# receive mail, so a guest account can never be taken over through
# forgot-password. NO new column — the domain IS the marker, which is why there
# is no migration here.
GUEST_USERNAME_DOMAIN = "guests.cartarch.invalid"
_GUEST_USERNAME_SUFFIX = "@" + GUEST_USERNAME_DOMAIN


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # v4.13.29 — the person's actual name, for people who play together.
    # SEPARATE from display_name on purpose: display_name is the pseudonymous
    # handle, and it is what the ANONYMOUS wishlist page (/w/{token}) shows.
    # Writing real names into it would turn a public pseudonymous surface into
    # a first-name one for anyone holding the link. This field is member-facing
    # ONLY — see `player_label`, and never render it on a public projection.
    real_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    deck_view_mode: Mapped[str] = mapped_column(String(16), default="grid", nullable=False)
    deck_group_by: Mapped[str] = mapped_column(String(16), default="type", nullable=False)
    # A SEPARATE column from deck_view_mode, deliberately: someone can want art
    # tiles for a 100-card deck and a dense list for a 1,400-card showcase, and
    # one shared column would make each surface silently change the other.
    showcase_view_mode: Mapped[str] = mapped_column(
        String(16), default="grid", server_default="grid", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    # v3.27.4 — replaces the misleading "last activity" proxy on the Admin
    # page (which was `func.max(TransactionLog.created_at)`, i.e. last
    # inventory event — users who only play games / edit decks / log in
    # showed stale dates). Set by POST /login on every successful auth.
    # NULL until next login for existing users (no backfill: the proxy
    # data is semantically different and copying it under the new name
    # would import the same misleading signal).
    last_signed_in_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # last_active_at — distinct from last_signed_in_at: it records the last time
    # the user made an *authenticated request* (not just the last login), stamped
    # from get_current_user / get_optional_current_user, throttled to one write
    # per LAST_ACTIVE_THROTTLE per user. A persistent session that never re-logs
    # in leaves last_signed_in_at stale while this keeps advancing — the gap is
    # the engagement signal. Plain DateTime / naive UTC like every other column.
    # NULL until next authenticated request for existing users (no backfill,
    # and explicitly NOT copied from last_signed_in_at — different semantics).
    last_active_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # #99 — opt-in to watchlist price-alert emails (a card crossing target_price).
    # Default off; NULL for existing rows (no backfill), treated as off.
    price_alerts_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    # #146 — public read-only wishlist share link (same token-as-toggle shape as
    # Deck.share_token, #143): an unguessable secrets.token_urlsafe; presence =
    # the wishlist is publicly viewable at /w/{token}, NULL = private. Revoke =
    # NULL (link 404s at once). Nullable + UNIQUE so a token maps to one user.
    wishlist_share_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    # #179 — read-only API bearer token. The FOURTH token-as-toggle column
    # (Deck.share_token, User.wishlist_share_token, Game.join_code): presence =
    # /api/v1 access is on, NULL = off, revoke = NULL, regenerate = new value.
    # Unlike the other three this one is NOT in a URL — it rides an
    # ``Authorization: Bearer`` header and resolves WHICH USER is asking, which
    # is why require_metrics_token's single shared env-var secret is the wrong
    # precedent. Nullable + UNIQUE so a token maps to exactly one user; NULL is
    # exempt from UNIQUE on both PG and SQLite, so any number of users may have
    # the API switched off.
    # Stores the SHA-256 HEX of the token, never the token (#182). The plaintext
    # is shown once at generation and is unrecoverable afterwards, so a database
    # read — a backup, a support query, a dump — no longer yields working
    # credentials for every user's whole collection. 64 hex chars fits String(64)
    # exactly, and UNIQUE still means one token maps to one user.
    #
    # A plain SHA-256 is the right hash HERE and a bcrypt/argon2 would be
    # cargo-culted: the input is a 256-bit `secrets.token_urlsafe`, not a
    # human-chosen password, so there is no dictionary to attack and no need for
    # a work factor. What matters is that the stored value is not replayable.
    api_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)

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

    @hybrid_property
    def is_guest(self) -> bool:
        """#172 — an account minted by claiming a seat without signing up.

        A ``hybrid_property`` so Python and SQL share ONE definition: the people
        picker filters on it (``~User.is_guest``) while templates read it per
        row. Guests cannot sign in (their password is a secret nobody holds),
        so this is also the answer to "can this account come back".
        """
        return (self.username or "").endswith(_GUEST_USERNAME_SUFFIX)

    @is_guest.inplace.expression
    @classmethod
    def _is_guest_expression(cls):
        return cls.username.like("%" + _GUEST_USERNAME_SUFFIX)

    @property
    def player_label(self) -> str:
        """THE name to show a CO-MEMBER: real name, else handle, else login.

        `display_name or username` was open-coded at ten sites before this, so
        adding a third field meant changing ten places — the multi-copy trap.
        Route every member-facing surface through this.

        **Never use it on an anonymous surface.** /w/{token} and /d/{token}
        deliberately show `display_name` only; that guard is in main.py.
        """
        return (self.real_name or self.display_name or self.username or "").strip()

    @classmethod
    def player_label_expr(cls):
        """SQL form of `player_label`, for ORDER BY (a property can't sort)."""
        from sqlalchemy import func

        return func.coalesce(cls.real_name, cls.display_name, cls.username)


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
    # #76 — creature power/toughness (faithful raw Scryfall strings; can be
    # non-numeric, e.g. "*", "1+*", "X") and keyword abilities (JSON array
    # text, "[]" = processed-no-keywords vs NULL = not yet populated, same
    # contract as produced_tokens). Part of the scryfall_cards seam.
    power: Mapped[str | None] = mapped_column(String(16), nullable=True)
    toughness: Mapped[str | None] = mapped_column(String(16), nullable=True)
    keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    # #100 — Scryfall ``produced_mana`` (JSON array text, e.g. ``["R","W"]``; "[]"
    # = produces nothing vs NULL = not yet backfilled, same contract as keywords).
    # Lets the goldfish auto-add mana on a land tap instead of prompting for color.
    produced_mana: Mapped[str | None] = mapped_column(Text, nullable=True)
    # #180 — Scryfall's EDHREC popularity rank (1 = most-played in Commander).
    # The one licensed, explicitly-offered play-frequency signal in the feed —
    # EDHREC itself has no official API and playgroup.gg's public API is
    # commander-level only. GLOBAL, so it ranks a card's general strength and
    # says nothing about fit with a particular commander. NULL = EDHREC has no
    # rank for the printing, which is missing data, not a low rank.
    edhrec_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    inventory_rows: Mapped[list[InventoryRow]] = relationship(back_populates="card")
    transaction_logs: Mapped[list[TransactionLog]] = relationship(back_populates="card")


class OracleCatalog(Base):
    """Momir Sim #109 — full Scryfall oracle_cards catalog, one row per oracle_id
    (i.e. one row per card NAME, not per printing). This replaces the
    collection-bounded ``cards`` table as the Momir creature source: ``cards``
    only holds what a user owns (~5.6k creature names), so it starved the Momir
    pool and its ``keywords`` were mostly unpopulated. Populated by
    ``app.jobs.oracle_ingest`` (manual/occasional bulk refresh).

    Multi-face layouts store the FRONT face's name/text/P-T/mana_cost with the
    root-level cmc, colors identity, and a representative ``scryfall_id`` (the
    mirror image key — clients build img.cartarch.com URLs from it unchanged).
    ``keywords``/``colors``/``color_identity`` are JSON array text exactly as
    Scryfall provides. ``is_momir_legal`` is the precomputed pool filter (Momir
    queries trust it — the token/vintage/set exclusions live in the ingest, not
    the query)."""

    __tablename__ = "oracle_catalog"

    id: Mapped[int] = mapped_column(primary_key=True)
    oracle_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    mana_cost: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cmc: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    type_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    oracle_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    power: Mapped[str | None] = mapped_column(String(16), nullable=True)
    toughness: Mapped[str | None] = mapped_column(String(16), nullable=True)
    colors: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    layout: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scryfall_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_momir_legal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class CardPrice(Base):
    """MTGJSON-sourced price for one printing + finish (MTGJSON ingest issue).

    The source of truth for displayed price, replacing Scryfall. One row per
    ``(scryfall_id, finish)``. Holds the per-provider USD retail values from the
    daily MTGJSON ingest plus a manual override that always wins. The resolved
    display value — override, then tcgplayer/cardkingdom/cardsphere first-non-null
    (see :func:`app.pricing.resolve_price_value`) — is denormalized back onto
    ``Card.price_usd*`` by the ingest, so every existing read surface
    (``effective_price`` + the SQL price expressions) keeps working unchanged and
    card_prices stays the authoritative upstream.

    ``cardmarket`` is deliberately absent: it is EUR while the three kept
    providers are USD, and mixing currencies silently corrupts a USD valuation.
    Prices are stored as strings to match ``Card.price_usd*`` (parsed via
    :func:`app.pricing.parse_price`). ``price_updated_at`` advances only when a
    fresh provider value actually arrives, so a transient miss keeps the
    last-known value while surfacing staleness instead of looking fresh.
    """

    __tablename__ = "card_prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    scryfall_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    finish: Mapped[str] = mapped_column(String(16), nullable=False)
    tcgplayer_retail: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cardkingdom_retail: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cardsphere_retail: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manual_override: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("scryfall_id", "finish", name="uq_card_prices_printing_finish"),
    )


class CardPriceHistory(Base):
    """#98 — daily snapshot of each priced printing's resolved price, one row per
    ``(scryfall_id, finish, snapshot_date)``. Global (per-printing, not per-user),
    written once a day by the price ingest for every ``card_prices`` row that
    resolves to a value. Feeds 1d/7d/30d deltas + trend surfaces. Mirrors the
    #85 DailyCollectionValue pattern at per-(card,finish) grain; series accrues
    forward (no backfill)."""

    __tablename__ = "card_price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    scryfall_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    finish: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "scryfall_id", "finish", "snapshot_date", name="uq_card_price_history_day"
        ),
    )


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
    # #159 — defaults to "manual" (do nothing) rather than "managed" (sorter may
    # empty this). A user creating a box has just made a filing decision; the
    # default must not undo it. Drawer auto-creation passes mode="managed"
    # explicitly (inventory_service._get_or_create_drawer_location) — it was the
    # only site relying on this default.
    mode: Mapped[str] = mapped_column(String(16), default="manual", nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    user: Mapped[User] = relationship(back_populates="storage_locations")
    parent: Mapped[StorageLocation | None] = relationship(
        remote_side="StorageLocation.id",
        back_populates="children",
    )
    children: Mapped[list[StorageLocation]] = relationship(back_populates="parent")
    inventory_rows: Mapped[list[InventoryRow]] = relationship(back_populates="storage_location")


class SorterRule(Base):
    """#104 — a per-user drawer-sorter rule: a collection-search ``query`` string
    matches cards, which are then filed into ``target_location``. Rules are
    evaluated in ascending ``position``, first match wins; a card matching no
    active rule falls through to the legacy drawer sort (if the user has drawer
    locations) or stays Pending. ``query`` reuses the collection-search grammar
    verbatim (empty = matches everything → a catch-all/default rule)."""

    __tablename__ = "sorter_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    query: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    target_location_id: Mapped[int] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    user: Mapped[User] = relationship()
    target_location: Mapped[StorageLocation] = relationship()


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    user: Mapped[User] = relationship(back_populates="inventory_rows")
    card: Mapped[Card] = relationship(back_populates="inventory_rows")
    storage_location: Mapped[StorageLocation | None] = relationship(back_populates="inventory_rows")


class Deck(Base):
    __tablename__ = "decks"
    # Deck names are unique PER USER (the correct multi-user scope). The
    # pre-v3.1.0 single-tenant ``UNIQUE INDEX`` on ``decks.name`` (global-unique)
    # died at the v4 Postgres cutover — the Alembic baseline (489afd0e62f9)
    # defines only this compound constraint and a NON-unique ``ix_decks_name``.
    # #133 removed the v3.30.18/v3.30.20 ``cross_user_deck_conflict`` workarounds
    # that guarded the old global-unique index; ``ix_decks_name`` remains only as
    # a plain lookup index.
    # #163 — the name-uniqueness scope EXCLUDES retired decks. Deck deletion became
    # a soft retire, and without the partial predicate a retired deck would squat on
    # its name forever, so a user could no longer reuse a name they had "deleted".
    # That would be a user-visible regression from a change meant to be invisible.
    # Partial unique indexes are supported on both Postgres and SQLite.
    __table_args__ = (
        Index(
            "uq_decks_user_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
            sqlite_where=text("retired_at IS NULL"),
        ),
    )

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
    # #121 — the owner's declared bracket (1-5). NULL = undeclared; the UI
    # prompts, never fills. The bracket is what the owner DECLARES; the deck's
    # contents impose a floor on what may be declared (deck_bracket_estimates
    # .floor_bracket). Cartarch verifies a declaration is legal, never guesses.
    declared_bracket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blurb: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    # #163 — soft delete. A deck with game history can no longer be hard-deleted
    # (game_seats.deck_id is RESTRICT), so ``delete_deck`` stamps this instead. The
    # deck still DISBANDS exactly as before (real cards return to the collection,
    # proxies are destroyed) so the user-visible outcome is unchanged; the row and
    # its game history survive. ``list_decks`` filters these out.
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # #163 — COLUMN ONLY in this issue, deliberately read by nothing. #164's
    # placeholder decks (a commander and no cards) will set it False, and the
    # content-dependent surfaces that must then skip them are #164's work, not
    # this issue's. Defaults True so every existing deck is unaffected.
    contents_tracked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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
    # #143 — public read-only share link. An unguessable token
    # (`secrets.token_urlsafe`); presence = the deck is publicly viewable at
    # `/d/{token}` by anyone (no account), NULL = private. The token IS the toggle:
    # generating one publishes, clearing it (revoke) invalidates the link
    # immediately. Nullable + UNIQUE so a token maps to exactly one deck.
    share_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    # #148 — the deck's optional "Considering" holding area: a dedicated per-deck
    # StorageLocation (type="considering") for cards being evaluated while brewing,
    # SEPARATE from the deck proper. Lazily created on first add
    # (get_or_create_considering_location). NULL = no considering area yet. Because
    # considering rows live in THIS location — NOT deck.storage_location_id — every
    # "cards in this deck" query auto-EXCLUDES them (counts / stats / legality /
    # goldfish / exports / public share): considering is opt-IN per surface, the
    # safe default given there is no single deck-cards choke-point. ondelete="SET
    # NULL" documents v4 intent; SQLite doesn't enforce it, so delete_deck disbands
    # the considering rows and drops the location explicitly.
    considering_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Two FKs from decks -> storage_locations (storage_location_id +
    # considering_location_id, #148), so this relationship must name its FK.
    storage_location: Mapped[StorageLocation | None] = relationship(
        foreign_keys=[storage_location_id]
    )
    user: Mapped[User] = relationship(back_populates="decks")
    variant_group: Mapped[VariantGroup | None] = relationship(back_populates="decks")
    # issue #46 — per-deck goals (custom ordered "what this deck is trying to
    # do" list, separate from win rate AND distinct from intent_*). Ordered by
    # position. ``passive_deletes`` keeps the ORM out of the delete path — the
    # NOT NULL FK means a parent delete must DELETE (never NULL) children; on
    # SQLite (FK off) ``delete_deck`` removes the goals explicitly, on Postgres
    # the DB CASCADE does it — same discipline as deck_card_shares.
    goals: Mapped[list[DeckGoal]] = relationship(
        back_populates="deck",
        order_by="DeckGoal.position, DeckGoal.id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class DeckCombo(Base):
    """#103 Phase A — persisted CommanderSpellbook combo results per deck.

    Written ONLY by the combo-refresh daemon (never on the request path — the
    v3.27.9 invariant). ``fingerprint`` is a hash of the deck's played card
    names; the daemon re-POSTs to Spellbook only when it changes, so the
    fingerprint diff IS the cache invalidation (no write-path hooks). ``payload``
    is the ``compute_deck_combos`` dict as JSON. One row per deck (UNIQUE).
    """

    __tablename__ = "deck_combos"

    id: Mapped[int] = mapped_column(primary_key=True)
    deck_id: Mapped[int] = mapped_column(
        ForeignKey("decks.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)


class DeckGoal(Base):
    """Issue #46 — per-deck goals (Feature 1 of 2).

    A custom, ordered list of what a deck is trying to do (e.g. "Win by combo",
    "Cast my commander 3+ times"), SEPARATE from win rate and DISTINCT from the
    ``decks.intent_*`` columns. Removal is a soft-delete (``is_active=False``)
    as the primary action; a hard delete is a separate explicit action.
    Feature 2 (#47) FKs this table for per-game completion tracking, so this
    ships first.

    ``deck_id`` is ON DELETE CASCADE NOT NULL (a goal is meaningless without its
    deck). SQLite enforces no FKs (PRAGMA foreign_keys OFF), so ``delete_deck``
    deletes goals explicitly; the DB CASCADE is Postgres defense-in-depth.
    ``is_active`` uses ``server_default=true()`` (never an integer literal — a
    bare ``1`` breaks CREATE TABLE on Postgres).
    """

    __tablename__ = "deck_goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    deck_id: Mapped[int] = mapped_column(
        ForeignKey("decks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    deck: Mapped[Deck] = relationship(back_populates="goals")


class DeckStrategyProfile(Base):
    """Issue #60 P3 — persisted per-deck strategy profile for the analyzer.

    One row per deck (``deck_id`` unique). ``profile_data`` is the JSON blob of
    the full profile dict (high/medium/low role lists + coverage targets), the
    same shape ``recommendation_service.seed_strategy_profile`` produces.
    ``is_custom=False`` = auto-seeded (safe to regenerate); ``True`` = the user
    edited it, so re-seeding must never silently overwrite it.

    ``deck_id`` is ON DELETE CASCADE NOT NULL (a profile is meaningless without
    its deck). SQLite enforces no FKs (PRAGMA foreign_keys OFF), so
    ``delete_deck`` deletes the profile explicitly; the DB CASCADE is Postgres
    defense-in-depth. ``is_custom`` uses ``server_default=false()`` (never an
    integer literal — a bare ``0`` breaks CREATE TABLE on Postgres).
    """

    __tablename__ = "deck_strategy_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    deck_id: Mapped[int] = mapped_column(
        ForeignKey("decks.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    profile_data: Mapped[str] = mapped_column(Text, nullable=False)
    is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    deck: Mapped[Deck] = relationship()


class DeckPlayProfile(Base):
    """Per-deck *piloting* profile — how to play the deck, not how to build it.

    DISTINCT from ``DeckStrategyProfile`` above, which holds deckbuilding targets
    (lands 36-38, ramp 10-14) for the analyzer. This one holds the pilot's intent:
    primary/secondary plan, hard rules, threat priorities. Consumed by the Forge
    AI-player simulation, which currently keeps this as YAML outside Cartarch —
    so the pilot's own knowledge is invisible to the deck builder today.

    It also exists to CORRECT auto-derived data. ``deck_combos`` descriptions are
    generated and can be wrong (a terminating kill loop misread as an infinite
    draw), and a confidently wrong combo description is worse than none: a policy
    reading it will avoid its own win condition. The pilot needs somewhere
    authoritative to say otherwise — once, at the source — so the deck builder,
    the bracket estimator and the simulation all see the same correction.

    One row per deck (``deck_id`` unique). ``profile_data`` is a JSON blob so the
    shape can evolve without a migration per field. ``is_custom=False`` =
    auto-seeded (safe to regenerate); ``True`` = pilot-edited, so re-seeding must
    never silently overwrite it — same contract as ``DeckStrategyProfile``.

    ``deck_id`` is ON DELETE CASCADE NOT NULL. SQLite enforces no FKs (PRAGMA
    foreign_keys OFF), so ``delete_deck`` deletes the profile explicitly; the DB
    CASCADE is Postgres defense-in-depth. ``is_custom`` uses
    ``server_default=false()`` (never an integer literal — a bare ``0`` breaks
    CREATE TABLE on Postgres).
    """

    __tablename__ = "deck_play_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    deck_id: Mapped[int] = mapped_column(
        ForeignKey("decks.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    profile_data: Mapped[str] = mapped_column(Text, nullable=False)
    is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    deck: Mapped[Deck] = relationship()


class CommanderGlobalStat(Base):
    """Global per-commander statistics harvested from playgroup.gg's PUBLIC API.

    One row per commander card name. This is the worldwide prior — win rate, ELO,
    global rank, sample sizes — from every game playgroup.gg has recorded, no
    authentication required (their /commanders endpoints are open; game-level
    data is playgroup-scoped and NOT harvested — the playgroup has never used
    playgroup.gg, so Cartarch's own games tables remain the only real-game
    source). Refreshed by an in-app daemon loop on the price-ingest pattern;
    ``payload`` keeps the raw response so new fields cost no migration.

    Consumers: the bracket estimator and deck builder, as a third opinion beside
    the pilot's declared brackets and the AI-simulation run labels.
    """

    __tablename__ = "commander_global_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    commander_name: Mapped[str] = mapped_column(
        String(256), nullable=False, unique=True, index=True
    )
    pg_commander_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    global_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    games_won: Mapped[int | None] = mapped_column(Integer, nullable=True)
    games_lost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_wins_by_turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decks_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    games_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class DeckSimResult(Base):
    """Aggregated AI-simulation results for a deck — empirical strength evidence.

    One row per (deck, run_label, strategy): ``run_label`` names the simulation
    batch (e.g. ``gauntlet-2026-08-02``), ``strategy`` the pod-selection meta it
    was measured under (``random`` / ``banded`` / ``spotlight`` / ``core``), and
    wins/games the aggregate. 4-seat Commander pods, so the null baseline is a
    25% win rate. Produced by the Forge AI-player gauntlet and seeded at boot
    from ``app/data/sim_results_seed.json`` (same deploy-like-code pattern as
    the play-profile seed); consumers are the bracket estimator and the deck
    builder's relative-strength signal.

    Results are AI-piloted evidence, not ground truth: decks whose plan the
    search AI cannot execute (combo sequencing, alt-win loops) read LOW.
    ``deck_id`` CASCADE is PG defense-in-depth; ``delete_deck`` cleans up
    explicitly for SQLite, same as every deck-scoped table.
    """

    __tablename__ = "deck_sim_results"
    __table_args__ = (
        UniqueConstraint("deck_id", "run_label", "strategy", name="uq_deck_sim_results_run"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deck_id: Mapped[int] = mapped_column(
        ForeignKey("decks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_label: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    wins: Mapped[int] = mapped_column(Integer, nullable=False)
    games: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    deck: Mapped[Deck] = relationship()


class VariantGroup(Base):
    __tablename__ = "variant_groups"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_variant_groups_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    user: Mapped[User] = relationship()
    decks: Mapped[list[Deck]] = relationship(back_populates="variant_group")


class DeckCardShare(Base):
    """Issue #27 — variant-group deck sharing (membership ≠ location).

    Records that a physical :class:`InventoryRow` (still stored in its
    *source* deck's ``storage_location_id`` — the one-card-one-location
    invariant is PRESERVED, no row duplication) is ALSO a member of a
    sibling build's decklist (``target_deck_id``). It is a **reference, never
    a copy**: the row stays where it physically lives; the share only adds it
    to another deck in the SAME variant group.

    Query semantics (deck_service.py):
      - A deck's full card list = its own ``InventoryRow``s UNION its inbound
        shares (rows where ``target_deck_id == this deck``).
      - The deck card count includes inbound shares.
      - The *collection* count counts ``InventoryRow``s ONLY — a share is
        NEVER counted (the user owns one physical copy; no double-count).

    ``UNIQUE(inventory_row_id, target_deck_id)`` makes a row shared to a deck
    at most once (``share_card_to_deck`` is idempotent on it). All four FKs are
    ``ON DELETE CASCADE NOT NULL``: the share is meaningless without its row,
    its decks, or its group, and dies with any of them. SQLite enforces no FKs
    (PRAGMA foreign_keys OFF), so the cascades are also performed explicitly
    (``clean_inventory_row_references`` on row delete; ``delete_shares_for_deck``
    /``assign_deck_variant_group``/``delete_variant_group`` on the deck/group
    side) — the DB CASCADE is then Postgres defense-in-depth.

    ``created_at`` is a ``timestamptz DEFAULT now()`` per the issue's logical
    schema (``DateTime(timezone=True)`` + ``server_default=text("now()")``), so
    Postgres stamps it server-side. The ORM ``default=utc_now`` supplies the
    value on the SQLite test path (where ``now()`` isn't a function but the
    server_default is never invoked because the Python default always provides
    one).
    """

    __tablename__ = "deck_card_shares"
    __table_args__ = (
        UniqueConstraint(
            "inventory_row_id", "target_deck_id", name="uq_deck_card_shares_row_target"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_row_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_rows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_deck_id: Mapped[int] = mapped_column(
        ForeignKey("decks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_deck_id: Mapped[int] = mapped_column(
        ForeignKey("decks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_group_id: Mapped[int] = mapped_column(
        ForeignKey("variant_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        default=utc_now,
        nullable=False,
    )

    inventory_row: Mapped[InventoryRow] = relationship()


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="transaction_logs")
    card: Mapped[Card | None] = relationship(back_populates="transaction_logs")
    batch: Mapped[ImportBatch | None] = relationship(back_populates="transaction_logs")


class Game(Base):
    __tablename__ = "games"

    # #165 — same PARTIAL unique posture as ``uq_playgroups_join_code``: a claim
    # code must be unambiguous among ENABLED codes, while NULL (claiming off) may
    # repeat across every game that has it disabled. Both dialects, so the
    # predicate emits on SQLite and Postgres alike.
    __table_args__ = (
        Index(
            "uq_games_join_code",
            "join_code",
            unique=True,
            sqlite_where=text("join_code IS NOT NULL"),
            postgresql_where=text("join_code IS NOT NULL"),
        ),
    )

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
    played_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    # v3.27.2 — service-layer enum (CANONICAL_GAME_FORMATS in game_service.py).
    # Column stays nullable=True at the DB level because SQLite can't alter
    # NULL→NOT NULL on an existing column without a table rebuild (reserved
    # for v4). The Python-side default + game_create's normalize_game_format
    # validation ensure new rows always carry a canonical value; the v3.27.2
    # migration backfills existing rows to canonical values too. NULL is
    # effectively unreachable after migration but the column type permits it.
    format: Mapped[str | None] = mapped_column(String(64), nullable=True, default="Commander")
    # #113 physical-table mode — a Momir game played with real basic-land decks:
    # the app skips digital mana/hand/library tracking (the physical cards handle
    # it) and just rolls creatures + runs combat. Nullable/default False; only
    # meaningful for format="Momir".
    momir_physical: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
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
    # #165 — the SEAT-CLAIM code. Distinct from ``client_token`` in kind, not just
    # value: the table token grants control of EVERY seat and must never reach a
    # phone, while this only lets a logged-in member attach THEMSELVES to one
    # unclaimed seat, and only while the game is ``created``. Never confuse them.
    #
    # NULL = claiming disabled. Follows the v3.29.0 ``Playgroup.join_code`` model
    # exactly, including the PARTIAL unique index below — codes are unique among
    # ENABLED ones; NULL repeats freely.
    join_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    # v3.33.2 — wall-clock end timestamp, stamped once by end_game when the
    # game is finalized. NULL = never finalized OR a legacy game predating this
    # column (the game-summary view shows "—" for elapsed in that case; no
    # backfill — past durations are unrecoverable). Elapsed playtime is
    # rendered as ``ended_at − played_at`` (played_at ≈ when live play started).
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
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
    # #166 — the play session (one evening at a table) this game belongs to.
    # NULL is legitimate and permanent for a game with no playgroup: a session
    # BELONGS to a playgroup, so an unaffiliated game has no session to join.
    # Game 64 is exactly that case and is why the model is playgroup-scoped
    # rather than date-scoped — see GameSession.
    # ``SET NULL`` on delete: deleting a session must never take its games with
    # it, the same posture ``playgroup_id`` takes one line above.
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("game_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Game-event history (issue: event log + analytics). Optional operator-picked
    # win condition captured at finalize (combat / commander / combo / attrition /
    # concession / other). NULL = not recorded.
    win_condition: Mapped[str | None] = mapped_column(String(32), nullable=True)

    seats: Mapped[list[GameSeat]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        order_by="GameSeat.seat_number",
    )
    user: Mapped[User] = relationship()
    playgroup: Mapped[Playgroup | None] = relationship()
    # Companion mode (issue: live game state). At most one live-state row per game
    # (working memory during in_progress play). delete-orphan so delete_game
    # (session.delete(game)) drops it in Python — SQLite runs FKs OFF, so the DB
    # CASCADE is Postgres defense-in-depth; end_game deletes it explicitly on finalize.
    live_state: Mapped[GameLiveState | None] = relationship(
        back_populates="game", uselist=False, cascade="all, delete-orphan"
    )
    # Append-only event log (life/cmd/counter/eliminate/turn + live_started /
    # finalized bookends). delete-orphan so delete_game drops them in Python
    # (SQLite FKs OFF); the DB CASCADE is Postgres defense-in-depth.
    events: Mapped[list[GameEvent]] = relationship(
        back_populates="game", cascade="all, delete-orphan", order_by="GameEvent.id"
    )
    session: Mapped[GameSession | None] = relationship(back_populates="games")


class GameSession(Base):
    """#166 — one evening at one table: an ordered set of games in a playgroup.

    **A session belongs to a PLAYGROUP, not to a date, and that is the whole
    design.** Sessions were previously derivable only by clustering `played_at`
    dates, and that clustering is not merely imprecise — it is WRONG on real
    data. 2026-06-28 holds four finalized games spanning 20.2 hours with a 16.2h
    internal gap: game 64 is one member playing with non-members somewhere else
    entirely (`playgroup_id` NULL, a `00:00:00` manual-log default stamp), and
    the other three are a playgroup meetup from 16:10 to 20:09. Date grouping
    folds a foreign game into a playgroup's session. Playgroup scoping excludes
    it for free, because a game with no playgroup has no session to belong to.

    **`ended_at` is set by a person, never by a clock.** A session boundary is a
    social fact; no timeout can tell a midnight-default timestamp from a real
    one, and a session left open is a smaller problem than one that closes while
    people are still playing. NULL `ended_at` = the session is open, and there is
    at most one open session per playgroup (enforced by a PARTIAL unique index,
    the same posture as `uq_games_join_code` and `uq_decks_user_name`).

    **There is deliberately NO benched-deck column.** The house rule — win a
    game and that deck is out for the rest of the session — is fully COMPUTABLE
    from the session's own games, and computing it cannot drift the way a stored
    flag can. Un-finalize a game or correct a placement and a derived answer
    follows; a stored one silently lies. See `session_service.benched_deck_ids`.
    """

    __tablename__ = "game_sessions"
    __table_args__ = (
        Index(
            "uq_game_sessions_open_per_playgroup",
            "playgroup_id",
            unique=True,
            sqlite_where=text("ended_at IS NULL"),
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    playgroup_id: Mapped[int] = mapped_column(
        ForeignKey("playgroups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    # NULL = open. Only a person closes a session (see the class docstring).
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    games: Mapped[list[Game]] = relationship(back_populates="session", order_by="Game.played_at")


class GameSeat(Base):
    __tablename__ = "game_seats"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), nullable=False, index=True)
    seat_number: Mapped[int] = mapped_column(Integer, nullable=False)
    player_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # #163 — RESTRICT, not SET NULL. The old rule nulled this on EVERY seat the
    # deck ever occupied when the deck was deleted, silently erasing that deck's
    # entire game history with no warning and no trace. Deck deletion is now a
    # soft retire (``Deck.retired_at``); this constraint is the backstop for any
    # path that still attempts a hard delete. The app-level null-out that used to
    # run in ``delete_deck`` is REMOVED — without that removal this constraint is
    # decorative, because the seats were already nulled before the DELETE ran.
    deck_id: Mapped[int | None] = mapped_column(
        ForeignKey("decks.id", ondelete="RESTRICT"), nullable=True
    )
    placement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    starting_life: Mapped[int] = mapped_column(Integer, default=40, nullable=False)
    final_life: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # #114 — per-seat elimination cause, persisted at finalize (was transient in
    # the live blob). "life"/"cmd"/"poison"/"deck" auto-tracked, or a manual
    # sub-cause "commander"/"poison"/"effect"/"concession"/"manual". NULL = winner /
    # not recorded. Corrigible via the post-finalization result edit.
    elimination_cause: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
    # #163 — RESTRICT, not SET NULL (same reasoning as ``deck_id`` above): the old
    # rule silently detached a player from every game they ever played when their
    # account was deleted. ``delete_user`` now REFUSES for a user holding seats and
    # points at deactivation (``User.is_active``) instead; its app-level null-out
    # is removed for the same reason as the deck one.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
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
    # issue #47 — per-game goal completion (Feature 2 of 2). delete-orphan so a
    # seat (or its game, via Game.seats' delete-orphan) drops its result rows.
    # NO passive_deletes here (unlike Deck.goals / deck_card_shares): SQLite runs
    # PRAGMA foreign_keys OFF, so the ORM must actively load + delete the children
    # in Python — the game/seat delete path relies on this cascade, not the DB
    # CASCADE (which is Postgres-only defense-in-depth). Deliberately the ONLY
    # delete-orphan on these rows — DeckGoal does NOT also declare one (dual
    # delete-orphan on the same child is ambiguous); the goal/deck-delete side is
    # cleaned explicitly in deck_service.
    goal_results: Mapped[list[GameGoalResult]] = relationship(
        back_populates="game_seat",
        cascade="all, delete-orphan",
    )


class GameGoalResult(Base):
    """Issue #47 — per-game completion of a deck goal (Feature 2 of 2).

    Records whether a seat's deck achieved one of its :class:`DeckGoal`s in one
    game. The grain is the SEAT (one deck in one game), not the game or the deck.
    Rows are written at finalize for the goals ACTIVE at that time — no
    retroactive backfill — so the deck's completion rate is over games tracked
    after each goal existed. There is NO goal-label snapshot: these are deck-
    private analytics that may die with the deck (deliberately unlike the
    seat-level ``deck_name_at_game`` snapshots that preserve shared game history).

    Both FKs are ON DELETE CASCADE NOT NULL + indexed. SQLite enforces no FKs
    (PRAGMA foreign_keys OFF), so the game/seat side is handled by the ORM
    delete-orphan cascade (``GameSeat.goal_results`` / ``Game.seats``) and the
    goal/deck side by explicit cleanup in ``deck_service`` (``delete_deck_goal``
    + ``delete_deck``); the DB CASCADE is Postgres defense-in-depth.
    ``UNIQUE(game_seat_id, deck_goal_id)`` makes the finalize upsert idempotent —
    re-finalizing a game updates the existing row in place. ``achieved`` uses
    ``server_default=false()`` (never an integer literal, which breaks CREATE
    TABLE on Postgres).
    """

    __tablename__ = "game_goal_results"
    __table_args__ = (
        UniqueConstraint("game_seat_id", "deck_goal_id", name="uq_game_goal_results_seat_goal"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    game_seat_id: Mapped[int] = mapped_column(
        ForeignKey("game_seats.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deck_goal_id: Mapped[int] = mapped_column(
        ForeignKey("deck_goals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    achieved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    game_seat: Mapped[GameSeat] = relationship(back_populates="goal_results")


class LiveActionConflict(Base):
    """One row per DETECTED lost update in a live game (#155, for #153).

    **This table exists because the v4.12.7 instrumentation could not outlive the
    question it was asked.** It writes to stdout; the cluster runs no log
    aggregator, ``kubectl logs --previous`` is gone after one restart, and prod
    restarts on every deploy. Measured 2026-08-07: **zero live games had been
    played since the instrumentation shipped**, so it had never observed a single
    action — and the first game's evidence would have been erased by the next
    deploy. A diagnostic that survives neither the pod nor the question cannot
    settle anything.

    **Only CLOBBERS are persisted, not every action.** The per-action line stays
    in the log: it is high-volume (822 events across 4 games), and its value is
    offline correlation. The decisive fact — two requests read version N and both
    wrote N+1, so the second discarded the first — is rare by construction and is
    what a conclusion needs. The DENOMINATOR is free: ``game_events`` already
    persists one row per applied action.

    **No FK on ``game_id``, deliberately** (the ``card_prices.scryfall_id``
    precedent). A record that a game lost a mutation should not evaporate when the
    game is deleted, and a diagnostic must not add a new edge to the delete
    topology ``tests/test_fk_parent_delete.py`` reasons about.

    TEMPORARY, like the instrumentation it backs: drop both when #153 closes.
    """

    __tablename__ = "live_action_conflicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # Nullable: the table token acts for a game, not a person.
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # THE decisive pair. Equal values across two requests mean the second commit
    # overwrote the first's blob.
    version_read: Mapped[int] = mapped_column(Integer, nullable=False)
    version_written: Mapped[int] = mapped_column(Integer, nullable=False)
    already_written: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Sampled at ENTRY, not commit — any two overlapping requests have at least
    # one that started while the other was in flight.
    concurrent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class GameLiveState(Base):
    """Companion mode — the live, mid-game working state of an in_progress game.

    One row per game (``game_id`` UNIQUE), holding the same JSON blob the
    localStorage tracker uses (lives / eliminated / cmd / counters / turn), so
    the existing client render logic can be reused. This is the FIRST mid-game
    server-persisted state — everything else about a game is written only at
    create and finalize. The row is working memory: created by
    ``live_game_service.start_live_game``, mutated by ``apply_live_action``, and
    deleted on finalize (``end_game``) or game delete (``Game.live_state``
    delete-orphan). Final life/turn persist on seats/game as before.

    ``ondelete="CASCADE"`` is Postgres defense-in-depth (SQLite runs FKs OFF).
    ``version`` bumps on every action for the SSE event id + last-write-wins.
    """

    __tablename__ = "game_live_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)

    game: Mapped[Game] = relationship(back_populates="live_state")


class GameEvent(Base):
    """Append-only log of one live-game action (issue: event history + analytics).

    Written inside the SAME transaction as the state mutation it records — every
    ``apply_live_action`` appends exactly one row, plus the ``live_started`` /
    ``finalized`` bookends. ``seat_id`` is the acted-on seat (the RECEIVING seat
    for cmd; NULL for turn + bookends). ``payload`` is the action JSON (minus the
    table/csrf tokens); a cmd payload also carries ``raw_delta`` + ``actual_delta``
    (the post-floor value the service computed) so analytics never re-derives the
    floor rule. ``turn`` is ``state.turn`` at action time (the NEW turn after a
    turn advance). ``actor_kind`` is ``'table'`` (table-token authorized, incl.
    bookends) or ``'seat'`` (a phone). Both FKs CASCADE (Postgres
    defense-in-depth; delete-orphan handles SQLite).
    """

    __tablename__ = "game_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seat_id: Mapped[int | None] = mapped_column(
        ForeignKey("game_seats.id", ondelete="CASCADE"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)

    game: Mapped[Game] = relationship(back_populates="events")


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
    # v4.13.28 — same three canonical values as ``inventory_rows.finish``
    # (``FINISH_OPTIONS``), NOT a token-specific vocabulary: a foil token is
    # foil in the same sense a foil card is, and one set means one label map
    # and one normalizer. ``server_default`` is what lets the migration add a
    # NOT NULL column to existing rows.
    #
    # Unlike ``inventory_rows``, finish is NOT part of any merge key here —
    # ``create_token`` always INSERTs and never merges, so a foil and a normal
    # printing of the same token were already separate rows.
    finish: Mapped[str] = mapped_column(
        String(32), default="normal", server_default="normal", nullable=False
    )
    # ``ondelete="SET NULL"`` recovers a prod raw-SQL invariant the ORM omitted
    # (the migration created this FK with ON DELETE SET NULL — a deleted location
    # nulls the token's placement, keeps the token). SQLite doesn't enforce it
    # (foreign_keys OFF). gate-#5 verified — no parent-delete entrypoint exercises this FK (harness coverage-gate allow-list); the clause is v4 defense-in-depth.
    storage_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

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
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
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
    # #99 — alert dedup state. last_alerted_at is set when a target-cross email
    # fires and cleared on a run where the price is back above target, so the
    # alert fires once per crossing episode (never daily spam). last_alerted_price
    # is the price at that send (for the email + audit). NULL = never alerted.
    last_alerted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_alerted_price: Mapped[float | None] = mapped_column(Float, nullable=True)

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
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)

    # #135 — mirrored location sources. ORM-level cascade (Python-side), which
    # is what actually fires: the project runs SQLite with PRAGMA foreign_keys
    # OFF, so the DB-level CASCADE is Postgres defence-in-depth. Covers both
    # ORM-delete paths at once — delete_showcase and admin delete_user.
    location_sources: Mapped[list[ShowcaseLocationSource]] = relationship(
        cascade="all, delete-orphan"
    )
    items: Mapped[list[ShowcaseItem]] = relationship(
        back_populates="showcase", cascade="all, delete-orphan"
    )
    # v3.30.12 — back_populates pairs this with User.showcases. Closes
    # the v3.29.1 ORM-config gap that surfaced as the "Showcase.user
    # will copy column users.id to column showcases.user_id, which
    # conflicts with relationship(s)" SAWarning at mapper-configure.
    user: Mapped[User] = relationship(back_populates="showcases")


class ShowcaseLocationSource(Base):
    """#135 — a StorageLocation a Showcase MIRRORS live.

    **The structural distinction is the whole point of the locked design
    (2026-06-14, Cluster C).** Curated-vs-mirrored lives in *different tables*
    rather than a flag that every mutation site must remember to set correctly —
    which removes the recon-Q9 root cause (a ShowcaseItem carries no provenance,
    so nothing can tell a snapshot from a hand-pick) **by construction** instead
    of patching one face of it.

    Mirrored membership is COMPUTED at read time, so it can never be stale,
    never orphaned, and needs no sync hooks:

    * a card entering a sourced location appears — no hook;
    * a card MOVED out disappears — no hook. This is the asymmetry that made the
      original bug report. A move never removed a curated item (the item keys on
      ``inventory_row_id``, which survives a move), so "removing it from the box
      dropped it from the showcase" was only ever true when the row was DELETED
      or merged away.

    ``ShowcaseItem`` is kept for hand-curated single cards and is unchanged. On
    cutover every existing ShowcaseItem is treated as **curated**: they carry no
    provenance, so a snapshot cannot be told from a hand-pick, and guessing
    would silently convert someone's deliberate picks into a live mirror. They
    render exactly as they do today; a user who wants live behaviour adds the
    location as a source.
    """

    __tablename__ = "showcase_location_sources"
    __table_args__ = (
        UniqueConstraint("showcase_id", "storage_location_id", name="uq_showcase_location_sources"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    showcase_id: Mapped[int] = mapped_column(
        ForeignKey("showcases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_location_id: Mapped[int] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


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
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)

    user: Mapped[User] = relationship()
    showcase: Mapped[Showcase] = relationship()
    playgroup: Mapped[Playgroup] = relationship()


class WishlistShare(Base):
    """One act of exposing a user's WISHLIST (watchlist) to one playgroup, read-only.

    #146 — mirrors :class:`Share` (the Showcase→playgroup share) but for the whole
    wishlist: one wishlist per user, so the unique pair is ``(user_id, playgroup_id)``
    (a user shared to N playgroups = N rows). Ephemeral — un-sharing hard-deletes the
    row. The co-member view is a names-only projection (no note / target price /
    ownership). The SEPARATE public-link path is ``User.wishlist_share_token``.

    Playgroup-lifecycle cleanup mirrors Share (wired in ``playgroup_service.py``):
    ``delete_playgroup`` deletes rows targeting that playgroup; ``leave_playgroup`` /
    ``remove_member`` delete the departing user's rows for that playgroup. The admin
    user-deletion cascade deletes the user's rows (``user_id`` denormalized for it).
    """

    __tablename__ = "wishlist_shares"
    __table_args__ = (
        UniqueConstraint("user_id", "playgroup_id", name="uq_wishlist_shares_user_playgroup"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    playgroup_id: Mapped[int] = mapped_column(
        ForeignKey("playgroups.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)

    user: Mapped[User] = relationship()
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
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
    # NULL while the trade is still ``proposed``; written on every terminal
    # transition. The single source of truth for "when did this close?".
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    items: Mapped[list[TradeItem]] = relationship(
        back_populates="trade", cascade="all, delete-orphan"
    )
    # Counter-proposals (#): every trade has at least ONE revision, written by
    # create_trade, so "the current items" has one definition from day one
    # rather than a null-revision special case bolted on later.
    revisions: Mapped[list[TradeRevision]] = relationship(
        back_populates="trade", cascade="all, delete-orphan", order_by="TradeRevision.id"
    )
    proposer: Mapped[User | None] = relationship(foreign_keys=[proposer_user_id])
    recipient: Mapped[User | None] = relationship(foreign_keys=[recipient_user_id])
    playgroup: Mapped[Playgroup | None] = relationship()


class TradeRevision(Base):
    """One version of a Trade's item sets — the counter-proposal unit (#).

    **A counter does not mutate the trade; it appends a revision.** The Trade
    keeps its id, its status and its place in both inboxes, and the items of
    every version are still on disk, which is what makes two things cheap that
    are otherwise expensive: showing a diff, and "decline it and the trade
    returns to its original state".

    ``declined_at`` is how a rejected counter steps back: the revision stays
    (it is history, and the diff that was rejected is worth keeping) but stops
    being current, so the previous revision takes over again. Deleting it would
    make "current = the last row" simpler and the record poorer.

    The author is either party — both may counter, and neither is limited to
    one (owner decision, 2026-08-21).
    """

    __tablename__ = "trade_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(
        ForeignKey("trades.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Who issued this version. Revision 1's author is always the proposer.
    # NOT a SET NULL: the account-deletion path abandons a party's pending
    # trades before the user row goes, so a live revision cannot outlive its
    # author (``author_name_at_revision`` carries the name into the record).
    author_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_name_at_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
    # Set when this counter is rejected; the previous revision becomes current
    # again. NULL on every revision that has not been rejected, including
    # superseded ones — "current" is the LAST non-declined revision.
    declined_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    trade: Mapped[Trade] = relationship(back_populates="revisions")
    author: Mapped[User | None] = relationship(foreign_keys=[author_user_id])
    # The relationship is what tells SQLAlchemy that trade_items depends on
    # trade_revisions, so a session.delete(trade) empties the items BEFORE the
    # revisions they point at rather than tripping the FK.
    items: Mapped[list[TradeItem]] = relationship(back_populates="revision")


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
    # Which revision of the trade this line belongs to (counter-proposals).
    # NOT NULL: an item with no revision could never be shown or hidden
    # correctly, and every existing row was backfilled onto revision 1.
    # CASCADE, because a revision's items have no meaning without it — a
    # declined counter is marked declined, never deleted, so this cascade only
    # fires when the whole trade goes.
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("trade_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
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
    revision: Mapped[TradeRevision] = relationship(back_populates="items")
    inventory_row: Mapped[InventoryRow | None] = relationship()
    card: Mapped[Card | None] = relationship()
    showcase_item: Mapped[ShowcaseItem | None] = relationship()


class DailyCollectionValue(Base):
    """One row per user per day: the placed collection value on ``snapshot_date``
    (issue #85). Written by the daily price-ingest job after prices refresh; the
    ``UNIQUE(user_id, snapshot_date)`` upsert makes a same-day re-run idempotent.

    Day is the grain — per-printing history is deliberately NOT stored (price-only
    trend, Option A: what today's holdings were worth on recorded past dates).
    ``total_value`` mirrors the dashboard's placed Collection Value exactly
    (finish-aware, pending excluded), so a snapshot reconciles with the tile.
    Feeds the collection-value-over-time chart.
    """

    __tablename__ = "daily_collection_values"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_value: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "snapshot_date", name="uq_daily_collection_values_user_date"),
    )


class AuditSession(Base):
    """Physical Audit Mode (issue #73) — one reconciliation pass over a single
    storage location. ``snapshot_hash`` fingerprints the location's inventory at
    start so reconciliation can detect drift (optimistic concurrency). At most
    one active/paused session per user is enforced in the service layer.
    """

    __tablename__ = "audit_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_location_id: Mapped[int] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False, index=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # JSON baseline of the location's inventory at start ([{row_id, qty, label}]),
    # so reconciliation can itemize what changed (not just that the hash differs).
    # Nullable: audits started before this column exists diff hash-only.
    snapshot_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional scope: JSON {"set_codes": [...]} restricting the audit to those
    # sets at the location. NULL = full-location audit (the default/original).
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
    paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AuditScan(Base):
    """One scan event inside an audit session. ``inventory_row_id`` is NULL for
    extras and out-of-scope scans (no matching expected row). ``scan_type`` is
    one of ``match`` / ``extra`` / ``partial_match`` / ``out_of_scope`` (the last
    only in scoped audits: a scanned card whose set isn't in the audit's scope).
    """

    __tablename__ = "audit_scans"

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_session_id: Mapped[int] = mapped_column(
        ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inventory_row_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"), nullable=False, index=True)
    finish: Mapped[str] = mapped_column(String(32), default="normal", nullable=False)
    scan_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity_scanned: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    card: Mapped[Card] = relationship()


class AuditLog(Base):
    """Completed-audit record — the "when did I last verify Drawer 3?" signal.
    ``actions_applied`` is a JSON string of the reconciliation changeset applied.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_session_id: Mapped[int] = mapped_column(
        ForeignKey("audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_location_id: Mapped[int] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cards_expected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cards_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cards_missing: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cards_extra: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions_applied: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # Copied from the session at completion: JSON {"set_codes": [...]} for a
    # scoped audit, NULL for a full-location audit. Drives history scope badges
    # and the "last FULL audit" staleness signal on the hub.
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)


class DeckCommander(Base):
    """#163 — a deck's commander identity, as an order-independent SET of cards.

    **Why a join table and not ``decks.commander_card_id`` plus a partner column.**
    Multi-commander is the general case here, not an edge case: 5 of the 39 decks
    carrying any commander have more than one (Partners, Backgrounds, Doctor's
    companion). Arity is not fixed by the rules, so a fixed number of columns is
    wrong on its face.

    More importantly, a column-plus-partner-slot design carries forward the exact
    hazard it is meant to remove. ``game_seats.commander_name_at_game`` already
    records deck 4 both ways —

        "Frodo, Adventurous Hobbit + Sam, Loyal Attendant"
        "Sam, Loyal Attendant + Frodo, Adventurous Hobbit"

    — and a two-column layout just relocates that instability into "which card goes
    in which column". **Lineage comparison is SET EQUALITY over ``card_id``**, never
    string or column comparison.

    NO uniqueness constraint spans decks: whether two of one user's decks sharing a
    commander set are one lineage or two is an OPEN owner decision (#163 amendment
    2), and a constraint here would pre-decide it. Zero users have such a pair today.

    A deck with no rows here has an EMPTY set, which is legal — 4 decks are in that
    state now, and #164's placeholder decks begin there.
    """

    __tablename__ = "deck_commanders"
    __table_args__ = (UniqueConstraint("deck_id", "card_id", name="uq_deck_commanders_deck_card"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    deck_id: Mapped[int] = mapped_column(
        ForeignKey("decks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    card_id: Mapped[int] = mapped_column(
        ForeignKey("cards.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class GameVariant(Base):
    """#163 — a format variant in play for a game. Variants COMPOSE.

    Planechase + Momir + random-deck is a legitimate combination, so a single enum
    column on ``games`` is the wrong shape. One row per (game, variant).

    Variant was previously discoverable only by scanning ``game_events``, and only
    8 of 23 finalized games have an event stream at all — so a manually-logged
    Planechase game was undetectable. ``games.momir_physical`` was the one-off
    predecessor: null on 18 of 23 rows and ``true`` on ZERO, i.e. never once set
    affirmatively.

    Values are service-layer constrained (``VALID_GAME_VARIANTS``), matching the
    project's existing no-DB-CHECK pattern for constrained columns.
    """

    __tablename__ = "game_variants"
    __table_args__ = (UniqueConstraint("game_id", "variant", name="uq_game_variants_game_variant"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
