"""Gate #5, Phase 1 — FK parent-delete VERIFICATION HARNESS (RED expected).

Gate #4 encoded a full FK ``ondelete`` topology in the Alembic baseline (every
non-trivial clause flagged ``# gate-#5 pending verification``). Those clauses are
*declared* but have never been verified against the app's actual parent-delete
code paths under FK enforcement. This module is that verifier.

WHY METADATA-DRIVEN (the meta-lesson). A hand-written matrix of "which child does
which delete clean" undercounts exactly the way the ORM-blind audit did — it
misses the raw legacy tables and any FK nobody remembered. So the harness is built
from ``Base.metadata`` itself (with ``app.legacy_tables`` imported, so the 7
raw-SQL tables' FKs are in scope), and a coverage gate asserts that EVERY parent
with a consequential (CASCADE / SET NULL) child FK is either exercised by an
entrypoint here or explicitly allow-listed. Add an FK to the schema and this
harness forces you to account for it — complete by construction.

THE 2D STRUCTURE: (parent-delete entrypoint) × (FK ondelete consequence).
For each app service that deletes a parent (``delete_deck``, ``delete_user``,
``delete_playgroup``, ``delete_variant_group``, ``delete_showcase``,
``delete_inventory_row``) we seed the parent + one child row across ALL of that
parent's child FKs (raw legacy tables included — that's what catches the
``deck_bracket_*`` leak), delete through the REAL app path, then assert:

  1. No app error — the delete completes (doesn't raise on an enforced FK).
  2. DB outcome matches the declared ondelete — CASCADE children gone, SET NULL
     children's reference nulled (and kept).
  3. Zero residual orphans — via the legacy-aware orphan scan reused verbatim
     from ``scripts/sweep_fk_orphans.find_orphans`` (the same scan the cutover
     loader validates with).

TWO POSTURES, both dual-backend (SQLite + Postgres via TEST_DATABASE_URL):
  * ``fk_off``  — PRODUCTION's posture TODAY (``PRAGMA foreign_keys`` OFF; on PG
    we reproduce it with ``SET session_replication_role = replica``). This is
    where the leaks MANIFEST as orphans — the app, not the DB, must do the
    cleanup, and where it doesn't, an orphan accrues. This is the prod reality
    "why the leaks exist".
  * ``fk_on``   — the v4 CUTOVER posture (FK enforced). Here the DB itself applies
    each declared ``ON DELETE`` clause, so a CASCADE/SET NULL leak is MASKED by
    the engine — but a NO ACTION child the app forgot to clean turns into a hard
    ``IntegrityError`` (assertion 1 fails) instead of a silent orphan.

Running BOTH is deliberate: ``fk_off`` proves the orphan exists (the RED the prod
audit predicts); ``fk_on`` proves whether the declared topology is *sufficient* at
the cutover or whether the app path actively breaks under enforcement. Phase 2's
fix is scoped by exactly which cells are red in which posture.

THIS IS A RED HARNESS. The known leaks (``delete_deck`` not cleaning the raw
``deck_bracket_*`` / ``deck_token_requirements`` / nulling ``game_seats.deck_id``;
``delete_user`` bulk-deleting decks + inventory + never touching
``token_inventory`` / ``games``) are EXPECTED to fail here. Do not "fix" them by
editing this file — Phase 2 fixes the services.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import text

# CRITICAL: importing app.legacy_tables registers the 7 raw-SQL tables (and their
# FKs: deck_bracket_estimates/findings.deck_id, game_changer_cards.card_id, ...) on
# Base.metadata. Without it the harness — and the conftest create_all — are blind
# to them, the exact undercount this gate exists to prevent. (scripts.sweep_fk_orphans
# imports it too, so it is already on the metadata for the whole pytest session.)
import app.legacy_tables  # noqa: F401
import app.models  # noqa: F401
from app.db import Base
from app.legacy_tables import (
    deck_bracket_estimates as t_bracket_estimates,
)
from app.legacy_tables import (
    deck_bracket_findings as t_bracket_findings,
)
from app.models import (
    Card,
    Deck,
    DeckTokenRequirement,
    Game,
    GameSeat,
    ImportBatch,
    InventoryRow,
    PasswordResetToken,
    Playgroup,
    PlaygroupMember,
    Share,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    TokenInventory,
    Trade,
    TradeItem,
    TransactionLog,
    User,
    VariantGroup,
    WatchlistItem,
)
from scripts.sweep_fk_orphans import _fk_specs, find_orphans

# ---------------------------------------------------------------------------
# Reporting accumulator — every (entrypoint × FK × posture) cell lands here so
# the final test can print the full RED matrix regardless of which asserts fail.
# ---------------------------------------------------------------------------
RESULTS: list[dict] = []


def _record(**row) -> None:
    RESULTS.append(row)


def _utc() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Posture control: reproduce the FK-OFF prod reality on BOTH backends.
#   - SQLite: the ``db`` fixture already runs PRAGMA foreign_keys OFF.
#   - Postgres: FKs are always enforced, so wrap the delete in
#     ``session_replication_role = replica`` — the exact mechanism the cutover
#     loader (scripts/migrate_sqlite_to_pg.py) and tests/test_orphan_sweep.py use.
# ---------------------------------------------------------------------------
@contextmanager
def _posture(session, posture: str):
    is_pg = session.bind.dialect.name == "postgresql"
    if posture == "fk_off" and is_pg:
        session.execute(text("SET session_replication_role = replica"))
        session.commit()
        try:
            yield
        finally:
            session.execute(text("SET session_replication_role = origin"))
            session.commit()
    else:
        # fk_off on SQLite (db fixture, PRAGMA off) or fk_on on either backend.
        yield


# ---------------------------------------------------------------------------
# A seeded child FK under test: which FK it is, its declared ondelete, and the
# PK of the row we created so we can read its post-delete state.
# ---------------------------------------------------------------------------
@dataclass
class ChildFK:
    key: str  # "child_table.col->parent" — matches find_orphans' key shape
    table: str
    col: str
    ondelete: str  # CASCADE | SET NULL | NO ACTION (declared)
    pk: int
    note: str = ""
    # SET NULL semantics: when the child row is expected to SURVIVE the parent
    # delete (the A4 "preserve the record, null the ref" intent), require state ==
    # "null". When the child is legitimately removed by a higher-level delete (e.g.
    # a pending trade deleted with its party), "absent" is also acceptable — the only
    # failure is an actual orphan (state == "set"). Default True (the strict case).
    expect_survives: bool = True
    # Optional extra post-delete assertion (e.g. verify a snapshot was populated).
    # Returns an error string or None.
    post_check: Callable | None = None


@dataclass
class Seeded:
    parent_table: str
    parent_id: int
    children: list[ChildFK] = field(default_factory=list)
    # extra owner-supplied callable to run the real delete service
    delete: Callable | None = None


def _row_state(session, table_name: str, col: str, pk) -> str:
    """'absent' (row gone) | 'null' (row present, FK col NULL) | 'set' (present, FK set)."""
    t = Base.metadata.tables[table_name]
    pkcol = list(t.primary_key.columns)[0]
    found = session.execute(sa.select(t.c[col]).where(pkcol == pk)).first()
    if found is None:
        return "absent"
    return "null" if found[0] is None else "set"


def _game_snapshot_check(game_id: int):
    """post_check for games.user_id SET NULL: verify the gate-#5 amendment preserved
    the recorder's identity — user_name_at_game must be populated (not left NULL)
    when user_id is nulled, else the read-only banner degrades to 'another player'."""

    def _check(session) -> str | None:
        t = Base.metadata.tables["games"]
        row = session.execute(
            sa.select(t.c.user_id, t.c.user_name_at_game).where(t.c.id == game_id)
        ).first()
        if row is None:
            return "game row unexpectedly deleted"
        if row[0] is not None:
            return "user_id not nulled"
        if not row[1]:
            return "user_name_at_game snapshot NOT populated (banner would lose the name)"
        return None

    return _check


def _nested_locations_cleared_check(*location_ids: int):
    """post_check for the leaf-first storage_locations delete (Gate #7): assert EVERY
    seeded location in the nested tree is actually gone, AND that the delete completed
    without raising on the self-ref FK (a crash surfaces as app_error → all cells fail).
    NOTE: this cell does not go RED against the *prior* single-statement bulk delete —
    a NO ACTION self-ref FK is checked at STATEMENT END (verified on PG18), so deleting
    the whole tree in one statement was already safe. The leaf-first code is defensive
    hardening; this cell positively asserts the tree fully clears and never raises on
    any backend/posture, and guards against a future regression to a per-statement /
    parent-before-child delete (which WOULD crash, like gate-#5 games.user_id)."""

    def _check(session) -> str | None:
        t = Base.metadata.tables["storage_locations"]
        for lid in location_ids:
            row = session.execute(sa.select(t.c.id).where(t.c.id == lid)).first()
            if row is not None:
                return f"nested location {lid} NOT deleted (leaf-first delete incomplete)"
        return None

    return _check


# ---------------------------------------------------------------------------
# Tiny seed helpers (valid rows — required NOT NULL columns supplied).
# ---------------------------------------------------------------------------
_SEQ = {"n": 0}


def _uniq() -> int:
    _SEQ["n"] += 1
    return _SEQ["n"]


def _user(s, name=None) -> User:
    u = User(username=name or f"u{_uniq()}@example.com", password_hash="x")
    s.add(u)
    s.flush()
    return u


def _card(s) -> Card:
    c = Card(
        scryfall_id=f"sid-{_uniq()}",
        name=f"Card {_uniq()}",
        set_code="TST",
        collector_number=str(_uniq()),
        price_usd="5.00",
    )
    s.add(c)
    s.flush()
    return c


def _loc(s, user_id, name="Box", ltype="box") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=ltype, mode="manual", sort_order=0)
    s.add(loc)
    s.flush()
    return loc


def _row(s, user_id, card_id, loc_id, *, is_proxy=False, pending=False, qty=1) -> InventoryRow:
    r = InventoryRow(
        card_id=card_id,
        user_id=user_id,
        quantity=qty,
        finish="normal",
        is_pending=pending,
        is_proxy=is_proxy,
        storage_location_id=loc_id,
    )
    s.add(r)
    s.flush()
    return r


def _deck(s, user_id, loc_id, name="Deck") -> Deck:
    d = Deck(user_id=user_id, name=f"{name} {_uniq()}", storage_location_id=loc_id)
    s.add(d)
    s.flush()
    return d


def _showcase_item_on(s, user, row) -> tuple[Showcase, ShowcaseItem]:
    sc = Showcase(user_id=user.id, name=f"SC{_uniq()}")
    s.add(sc)
    s.flush()
    si = ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1)
    s.add(si)
    s.flush()
    return sc, si


def _trade_item_on(s, proposer, recipient, card, row, status="proposed") -> tuple[Trade, TradeItem]:
    t = Trade(proposer_user_id=proposer.id, recipient_user_id=recipient.id, status=status)
    s.add(t)
    s.flush()
    ti = TradeItem(
        trade_id=t.id,
        side="offered",
        inventory_row_id=row.id,
        card_id=card.id,
        finish="normal",
        quantity=1,
    )
    s.add(ti)
    s.flush()
    return t, ti


def _bracket_rows(s, deck_id: int) -> tuple[int, int]:
    """Insert a raw deck_bracket_estimates + deck_bracket_findings pair for a deck."""
    est_pk = s.execute(
        t_bracket_estimates.insert().values(
            deck_id=deck_id,
            estimated_bracket=2,
            mechanics_bracket=2,
            final_bracket=2,
            rules_version="t",
        )
    ).inserted_primary_key[0]
    find_pk = s.execute(
        t_bracket_findings.insert().values(
            deck_id=deck_id,
            estimate_id=est_pk,
            finding_type="x",
            message="m",
        )
    ).inserted_primary_key[0]
    return est_pk, find_pk


# ---------------------------------------------------------------------------
# Entrypoint seeders. Each returns a Seeded(parent, children[], delete-callable).
# Children are enumerated to cover ALL of the parent's child FKs (consequential
# ones especially) so no leak can hide.
# ---------------------------------------------------------------------------
def seed_delete_deck(s) -> Seeded:
    from app import deck_service

    owner = _user(s)
    other = _user(s)
    card = _card(s)
    deck_loc = _loc(s, owner.id, "DeckLoc", ltype="deck")
    deck = _deck(s, owner.id, deck_loc.id)

    # Inventory in the deck: a REAL row (disbands→pending) and a PROXY row
    # (deleted outright by delete_deck — WITHOUT clean_inventory_row_references).
    _row(s, owner.id, card.id, deck_loc.id, is_proxy=False)
    proxy = _row(s, owner.id, card.id, deck_loc.id, is_proxy=True)
    # Reference the proxy row so its uncleaned delete shows as an orphan.
    _sc, si = _showcase_item_on(s, owner, proxy)
    _t, ti = _trade_item_on(s, owner, other, card, proxy)

    est_pk, find_pk = _bracket_rows(s, deck.id)
    dtr = DeckTokenRequirement(deck_id=deck.id, token_name="Treasure")
    s.add(dtr)
    s.flush()

    game = Game(user_id=owner.id)
    s.add(game)
    s.flush()
    seat = GameSeat(game_id=game.id, seat_number=1, player_name="P1", deck_id=deck.id)
    s.add(seat)
    s.flush()
    s.commit()

    children = [
        ChildFK(
            "deck_bracket_estimates.deck_id->decks",
            "deck_bracket_estimates",
            "deck_id",
            "CASCADE",
            est_pk,
            "raw legacy table",
        ),
        ChildFK(
            "deck_bracket_findings.deck_id->decks",
            "deck_bracket_findings",
            "deck_id",
            "CASCADE",
            find_pk,
            "raw legacy table",
        ),
        ChildFK(
            "deck_token_requirements.deck_id->decks",
            "deck_token_requirements",
            "deck_id",
            "CASCADE",
            dtr.id,
        ),
        ChildFK("game_seats.deck_id->decks", "game_seats", "deck_id", "SET NULL", seat.id),
        ChildFK(
            "showcase_items.inventory_row_id->inventory_rows",
            "showcase_items",
            "inventory_row_id",
            "CASCADE",
            si.id,
            "via proxy-row delete",
        ),
        ChildFK(
            "trade_items.inventory_row_id->inventory_rows",
            "trade_items",
            "inventory_row_id",
            "SET NULL",
            ti.id,
            "via proxy-row delete",
        ),
    ]
    deck_id, owner_id = deck.id, owner.id
    return Seeded(
        "decks", deck_id, children, lambda sess: deck_service.delete_deck(sess, deck_id, owner_id)
    )


def seed_delete_inventory_row(s) -> Seeded:
    from app import inventory_service

    owner = _user(s)
    other = _user(s)
    card = _card(s)
    loc = _loc(s, owner.id)
    row = _row(s, owner.id, card.id, loc.id)
    _sc, si = _showcase_item_on(s, owner, row)
    _t, ti = _trade_item_on(s, owner, other, card, row)
    s.commit()

    children = [
        ChildFK(
            "showcase_items.inventory_row_id->inventory_rows",
            "showcase_items",
            "inventory_row_id",
            "CASCADE",
            si.id,
        ),
        ChildFK(
            "trade_items.inventory_row_id->inventory_rows",
            "trade_items",
            "inventory_row_id",
            "SET NULL",
            ti.id,
        ),
    ]
    row_id, owner_id = row.id, owner.id
    return Seeded(
        "inventory_rows",
        row_id,
        children,
        lambda sess: inventory_service.delete_inventory_row(sess, row_id, owner_id),
    )


def seed_delete_user(s) -> Seeded:
    from app.routes.admin import delete_user as admin_delete_user

    admin = _user(s, "admin@example.com")
    owner = _user(s)
    other = _user(s)
    card = _card(s)
    loc = _loc(s, owner.id)
    deck_loc = _loc(s, owner.id, "DeckLoc", ltype="deck")
    deck = _deck(s, owner.id, deck_loc.id)
    row = _row(s, owner.id, card.id, loc.id)

    # NESTED locations (Gate #7): a parent → child StorageLocation tree via the self-ref
    # ``parent_id`` FK (NO ACTION). The old bulk delete in delete_user dropped all of a
    # user's locations in ONE statement — under PG enforcement that could delete the
    # parent before the child and trip the self-ref FK. The leaf-first fix must clear the
    # whole tree on both backends/postures. (Without this seed the harness was blind to
    # the leaf ordering — every prior location it seeded was flat/parentless.)
    nest_parent = _loc(s, owner.id, "NestParent")
    nest_child = StorageLocation(
        user_id=owner.id,
        name="NestChild",
        type="box",
        mode="manual",
        sort_order=0,
        parent_id=nest_parent.id,
    )
    s.add(nest_child)
    s.flush()

    # the user's own showcase referencing their row
    _sc, si = _showcase_item_on(s, owner, row)
    # a CROSS-USER TERMINAL trade referencing this user's row: status="accepted" so
    # the pending-trade pre-cleanup does NOT delete it — it must SURVIVE the user
    # delete with its inventory_row_id NULLed (the durable *_at_trade snapshot is the
    # record). This is the genuine leak the old bulk inventory-delete orphaned.
    _t, ti = _trade_item_on(s, other, owner, card, row, status="accepted")

    vg = VariantGroup(user_id=owner.id, name=f"VG{_uniq()}")
    s.add(vg)
    s.flush()
    deck.variant_group_id = vg.id

    batch = ImportBatch(user_id=owner.id, filename="paste")
    s.add(batch)
    s.flush()
    tlog = TransactionLog(user_id=owner.id, event_type="import")
    s.add(tlog)
    s.flush()

    game = Game(user_id=owner.id)
    s.add(game)
    s.flush()
    seat = GameSeat(game_id=game.id, seat_number=1, player_name="P1", user_id=owner.id)
    s.add(seat)
    s.flush()

    tok = TokenInventory(user_id=owner.id, name="Treasure", storage_location_id=loc.id)
    s.add(tok)
    s.flush()

    pg = Playgroup(name=f"PG{_uniq()}", created_by=owner.id)
    s.add(pg)
    s.flush()
    pm = PlaygroupMember(playgroup_id=pg.id, user_id=owner.id, role="owner")
    s.add(pm)
    s.flush()

    wl = WatchlistItem(user_id=owner.id, card_id=card.id)
    s.add(wl)
    s.flush()
    prt = PasswordResetToken(
        user_id=owner.id, token_hash="h", expires_at=_utc() + timedelta(hours=1)
    )
    s.add(prt)
    s.flush()

    est_pk, find_pk = _bracket_rows(s, deck.id)
    s.commit()

    children = [
        ChildFK(
            "storage_locations.user_id->users", "storage_locations", "user_id", "NO ACTION", loc.id
        ),
        ChildFK(
            "storage_locations.parent_id->storage_locations",
            "storage_locations",
            "parent_id",
            "NO ACTION",
            nest_child.id,
            "nested-location LEAF-FIRST delete (Gate #7): the user's whole location tree "
            "must clear without tripping the self-ref FK under PG enforcement",
            post_check=_nested_locations_cleared_check(nest_parent.id, nest_child.id),
        ),
        ChildFK("inventory_rows.user_id->users", "inventory_rows", "user_id", "NO ACTION", row.id),
        ChildFK("decks.user_id->users", "decks", "user_id", "NO ACTION", deck.id),
        ChildFK("variant_groups.user_id->users", "variant_groups", "user_id", "NO ACTION", vg.id),
        ChildFK(
            "import_batches.user_id->users", "import_batches", "user_id", "NO ACTION", batch.id
        ),
        ChildFK(
            "transaction_logs.user_id->users", "transaction_logs", "user_id", "NO ACTION", tlog.id
        ),
        ChildFK(
            "games.user_id->users",
            "games",
            "user_id",
            "SET NULL",
            game.id,
            "gate-#5 amendment: was NO ACTION (crash); now SET NULL + name snapshot",
            post_check=_game_snapshot_check(game.id),
        ),
        ChildFK("game_seats.user_id->users", "game_seats", "user_id", "SET NULL", seat.id),
        ChildFK("token_inventory.user_id->users", "token_inventory", "user_id", "CASCADE", tok.id),
        ChildFK("watchlist.user_id->users", "watchlist", "user_id", "CASCADE", wl.id),
        ChildFK(
            "password_reset_tokens.user_id->users",
            "password_reset_tokens",
            "user_id",
            "CASCADE",
            prt.id,
        ),
        ChildFK("playgroups.created_by->users", "playgroups", "created_by", "NO ACTION", pg.id),
        ChildFK(
            "playgroup_members.user_id->users", "playgroup_members", "user_id", "NO ACTION", pm.id
        ),
        ChildFK(
            "showcase_items.inventory_row_id->inventory_rows",
            "showcase_items",
            "inventory_row_id",
            "CASCADE",
            si.id,
            "transitive via inventory delete",
        ),
        ChildFK(
            "trade_items.inventory_row_id->inventory_rows",
            "trade_items",
            "inventory_row_id",
            "SET NULL",
            ti.id,
            "cross-user TERMINAL trade — must survive, ref nulled",
        ),
        ChildFK(
            "deck_bracket_estimates.deck_id->decks",
            "deck_bracket_estimates",
            "deck_id",
            "CASCADE",
            est_pk,
            "transitive via deck delete",
        ),
        ChildFK(
            "deck_bracket_findings.deck_id->decks",
            "deck_bracket_findings",
            "deck_id",
            "CASCADE",
            find_pk,
            "transitive via deck delete",
        ),
    ]
    admin_obj, target_id = admin, owner.id
    return Seeded(
        "users",
        target_id,
        children,
        lambda sess: admin_delete_user(
            user_id=target_id, session=sess, current_user=admin_obj, _=None
        ),
    )


def seed_delete_playgroup(s) -> Seeded:
    from app import playgroup_service

    owner = _user(s)
    member = _user(s)
    pg = Playgroup(name=f"PG{_uniq()}", created_by=owner.id)
    s.add(pg)
    s.flush()
    pm_owner = PlaygroupMember(playgroup_id=pg.id, user_id=owner.id, role="owner")
    pm_member = PlaygroupMember(playgroup_id=pg.id, user_id=member.id, role="member")
    s.add_all([pm_owner, pm_member])
    s.flush()

    game = Game(user_id=owner.id, playgroup_id=pg.id)
    s.add(game)
    s.flush()
    # a Share + Trade scoped to the playgroup
    sc = Showcase(user_id=owner.id, name=f"SC{_uniq()}")
    s.add(sc)
    s.flush()
    share = Share(user_id=owner.id, showcase_id=sc.id, playgroup_id=pg.id)
    s.add(share)
    s.flush()
    trade = Trade(
        proposer_user_id=owner.id,
        recipient_user_id=member.id,
        playgroup_id=pg.id,
        status="accepted",
    )
    s.add(trade)
    s.flush()
    s.commit()

    children = [
        ChildFK("games.playgroup_id->playgroups", "games", "playgroup_id", "SET NULL", game.id),
        ChildFK("shares.playgroup_id->playgroups", "shares", "playgroup_id", "NO ACTION", share.id),
        ChildFK(
            "playgroup_members.playgroup_id->playgroups",
            "playgroup_members",
            "playgroup_id",
            "NO ACTION",
            pm_member.id,
        ),
        ChildFK("trades.playgroup_id->playgroups", "trades", "playgroup_id", "NO ACTION", trade.id),
    ]
    pg_id, owner_id = pg.id, owner.id
    return Seeded(
        "playgroups",
        pg_id,
        children,
        lambda sess: playgroup_service.delete_playgroup(sess, owner_id, pg_id),
    )


def seed_delete_variant_group(s) -> Seeded:
    from app import deck_service

    owner = _user(s)
    loc = _loc(s, owner.id, "DeckLoc", ltype="deck")
    vg = VariantGroup(user_id=owner.id, name=f"VG{_uniq()}")
    s.add(vg)
    s.flush()
    deck = _deck(s, owner.id, loc.id)
    deck.variant_group_id = vg.id
    s.flush()
    s.commit()

    children = [
        ChildFK(
            "decks.variant_group_id->variant_groups",
            "decks",
            "variant_group_id",
            "SET NULL",
            deck.id,
        ),
    ]
    vg_id, owner_id = vg.id, owner.id
    return Seeded(
        "variant_groups",
        vg_id,
        children,
        lambda sess: deck_service.delete_variant_group(sess, owner_id, vg_id),
    )


def seed_delete_showcase(s) -> Seeded:
    from app import share_service

    owner = _user(s)
    member = _user(s)
    card = _card(s)
    loc = _loc(s, owner.id)
    row = _row(s, owner.id, card.id, loc.id)
    sc, si = _showcase_item_on(s, owner, row)

    pg = Playgroup(name=f"PG{_uniq()}", created_by=owner.id)
    s.add(pg)
    s.flush()
    share = Share(user_id=owner.id, showcase_id=sc.id, playgroup_id=pg.id)
    s.add(share)
    s.flush()
    # a trade item pointing at the showcase item (SET NULL on showcase delete)
    trade = Trade(proposer_user_id=owner.id, recipient_user_id=member.id, status="proposed")
    s.add(trade)
    s.flush()
    ti = TradeItem(
        trade_id=trade.id,
        side="offered",
        inventory_row_id=row.id,
        card_id=card.id,
        finish="normal",
        quantity=1,
        showcase_item_id=si.id,
    )
    s.add(ti)
    s.flush()
    s.commit()

    children = [
        ChildFK(
            "showcase_items.showcase_id->showcases",
            "showcase_items",
            "showcase_id",
            "NO ACTION",
            si.id,
            "ORM cascade delete-orphan",
        ),
        ChildFK("shares.showcase_id->showcases", "shares", "showcase_id", "NO ACTION", share.id),
        ChildFK(
            "trade_items.showcase_item_id->showcase_items",
            "trade_items",
            "showcase_item_id",
            "SET NULL",
            ti.id,
            "transitive via item cascade",
        ),
    ]
    sc_id, owner_id = sc.id, owner.id
    return Seeded(
        "showcases",
        sc_id,
        children,
        lambda sess: share_service.delete_showcase(sess, owner_id, sc_id),
    )


@dataclass
class Entrypoint:
    name: str
    parent_table: str
    seed: Callable


ENTRYPOINTS = [
    Entrypoint("delete_deck", "decks", seed_delete_deck),
    Entrypoint("delete_inventory_row", "inventory_rows", seed_delete_inventory_row),
    Entrypoint("delete_user", "users", seed_delete_user),
    Entrypoint("delete_playgroup", "playgroups", seed_delete_playgroup),
    Entrypoint("delete_variant_group", "variant_groups", seed_delete_variant_group),
    Entrypoint("delete_showcase", "showcases", seed_delete_showcase),
]


# ---------------------------------------------------------------------------
# Coverage gate — metadata-driven completeness (the anti-undercount mechanism).
# ---------------------------------------------------------------------------
def _consequential_parents() -> dict[str, list[tuple[str, str, str]]]:
    """{parent_table: [(child_table, child_col, ondelete)]} for every CASCADE / SET NULL FK."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    for child, col, parent_name, _pcol, ondelete in _fk_specs():
        if ondelete in ("CASCADE", "SET NULL"):
            out.setdefault(parent_name, []).append((child.name, col, ondelete))
    return out


# Consequential parents with NO dedicated parent-delete entrypoint, each with the
# reason it is not directly exercised here. The coverage test fails if a NEW
# consequential parent appears that is neither an entrypoint nor allow-listed —
# that is precisely the "third unknown leak" surfacing mechanism.
KNOWN_NO_ENTRYPOINT = {
    "cards": "catalog table — no user/app action deletes a Card (FKs: card_tags "
    "CASCADE, game_changer_cards SET NULL, watchlist/inventory/transaction).",
    "deck_bracket_estimates": "raw bracket table — rows are replaced by "
    "bracket_v2_service.persist_estimate (raw SQL clears prior), not a "
    "parent-delete entrypoint; its child findings.estimate_id is CASCADE.",
    "storage_locations": "deleted only transitively inside delete_deck / delete_user "
    "/ location_service; token_inventory.storage_location_id (SET NULL) is "
    "exercised via the delete_user case.",
    "token_inventory": "deleted via token_service; deck_token_requirements."
    "token_inventory_id (SET NULL) — not yet a dedicated entrypoint.",
}
# Parents covered transitively by an existing entrypoint (not a direct parent here).
TRANSITIVELY_COVERED = {
    "showcase_items": "delete_showcase nulls trade_items.showcase_item_id via item cascade",
    "inventory_rows": "covered directly by delete_inventory_row",
}


def test_coverage_every_consequential_parent_is_accounted_for():
    """Complete-by-construction gate: enumerate every parent with a CASCADE/SET NULL
    child FK from Base.metadata (legacy tables included) and assert each is either an
    entrypoint, transitively covered, or explicitly allow-listed. A new/unaccounted
    consequential parent is the 'third unknown leak' and FAILS here."""
    consequential = _consequential_parents()
    entrypoint_parents = {e.parent_table for e in ENTRYPOINTS}
    accounted = entrypoint_parents | set(TRANSITIVELY_COVERED) | set(KNOWN_NO_ENTRYPOINT)

    print("\n=== Consequential parents (CASCADE/SET NULL FK targets) ===")
    for parent in sorted(consequential):
        kids = ", ".join(f"{c}.{col} {od}" for c, col, od in consequential[parent])
        cover = (
            "ENTRYPOINT"
            if parent in entrypoint_parents
            else "transitive"
            if parent in TRANSITIVELY_COVERED
            else "no-entrypoint(allowlisted)"
            if parent in KNOWN_NO_ENTRYPOINT
            else "*** UNACCOUNTED ***"
        )
        print(f"  {parent:<24} [{cover}]  <- {kids}")

    unaccounted = set(consequential) - accounted
    assert not unaccounted, (
        f"Consequential parents with no entrypoint and not allow-listed: {sorted(unaccounted)}"
    )


def test_seeded_ondelete_matches_declared_topology():
    """Each ChildFK we seed hard-codes an ondelete; assert it equals what
    Base.metadata actually declares, so the harness can't drift from the baseline."""
    declared = {
        f"{child.name}.{col}->{parent}": ondelete
        for child, col, parent, _pcol, ondelete in _fk_specs()
    }
    mismatches = []
    for key, od in _EXPECTED_ONDELETE.items():
        if declared.get(key) != od:
            mismatches.append(f"{key}: harness says {od}, metadata says {declared.get(key)}")
    assert not mismatches, "Harness ondelete drifted from baseline:\n" + "\n".join(mismatches)


# The ondelete each FK-key is asserted under (kept in sync with the ChildFK seeds;
# checked against Base.metadata by the test above).
_EXPECTED_ONDELETE = {
    "deck_bracket_estimates.deck_id->decks": "CASCADE",
    "deck_bracket_findings.deck_id->decks": "CASCADE",
    "deck_token_requirements.deck_id->decks": "CASCADE",
    "game_seats.deck_id->decks": "SET NULL",
    "showcase_items.inventory_row_id->inventory_rows": "CASCADE",
    "trade_items.inventory_row_id->inventory_rows": "SET NULL",
    "games.user_id->users": "SET NULL",  # gate-#5 amendment (was NO ACTION)
    "game_seats.user_id->users": "SET NULL",
    "token_inventory.user_id->users": "CASCADE",
    "watchlist.user_id->users": "CASCADE",
    "password_reset_tokens.user_id->users": "CASCADE",
    "games.playgroup_id->playgroups": "SET NULL",
    "decks.variant_group_id->variant_groups": "SET NULL",
    "trade_items.showcase_item_id->showcase_items": "SET NULL",
}


# ---------------------------------------------------------------------------
# The 2D harness: (entrypoint) × (posture). pytest's own pass/fail IS the matrix.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("posture", ["fk_off", "fk_on"])
@pytest.mark.parametrize("ep", ENTRYPOINTS, ids=lambda e: e.name)
def test_parent_delete(ep: Entrypoint, posture: str, request):
    session = request.getfixturevalue("db" if posture == "fk_off" else "fk_db")
    backend = session.bind.dialect.name
    seeded = ep.seed(session)

    # --- run the real app delete path under the chosen posture ---
    app_error = None
    with _posture(session, posture):
        try:
            seeded.delete(session)
        except Exception as exc:  # noqa: BLE001 — capture, classify, report
            app_error = f"{type(exc).__name__}: {exc}"
            session.rollback()

    # --- evaluate per-FK outcomes + the global legacy-aware orphan scan ---
    if app_error is None:
        session.expire_all()
        orphans = find_orphans(session)
    else:
        orphans = {}  # delete rolled back; orphan scan not meaningful

    fk_failures: list[str] = []
    for child in seeded.children:
        state = "n/a" if app_error else _row_state(session, child.table, child.col, child.pk)
        orphaned = child.key in orphans and child.pk in orphans.get(child.key, [])

        ok = True
        reason = ""
        if app_error is not None:
            ok, reason = False, "app_error"
        elif child.ondelete == "CASCADE":
            ok = state == "absent"
            reason = "" if ok else f"CASCADE child not deleted (state={state})"
        elif child.ondelete == "SET NULL":
            if child.expect_survives:
                ok = state == "null"
                reason = "" if ok else f"SET NULL ref not nulled (state={state})"
            else:  # child legitimately removable; only an orphan (state==set) fails
                ok = state in ("null", "absent")
                reason = "" if ok else f"SET NULL ref orphaned (state={state})"
        else:  # NO ACTION — only requirement is it isn't left an orphan
            ok = not orphaned
            reason = "" if ok else "NO ACTION child orphaned"
        if orphaned and ok:  # belt-and-suspenders: a residual orphan is always a leak
            ok, reason = False, "residual orphan"
        # Optional per-FK post-delete assertion (e.g. snapshot populated).
        if ok and not app_error and child.post_check is not None:
            pc = child.post_check(session)
            if pc:
                ok, reason = False, pc

        _record(
            entrypoint=ep.name,
            posture=posture,
            backend=backend,
            fk=child.key,
            ondelete=child.ondelete,
            state=state,
            orphaned=orphaned,
            ok=ok,
            reason=reason or ("app_error" if app_error else ""),
            note=child.note,
        )
        if not ok:
            fk_failures.append(f"{child.key} [{child.ondelete}] — {reason or 'app_error'}")

    # Assertion 1: no app error. Assertion 2+3: per-FK outcome + zero residual orphans.
    msgs = []
    if app_error:
        msgs.append(f"APP ERROR on delete: {app_error}")
    if fk_failures:
        msgs.append("FK outcome failures:\n    " + "\n    ".join(fk_failures))
    residual = {k: v for k, v in orphans.items()}
    if residual:
        msgs.append(f"residual orphans (find_orphans): {residual}")

    assert not msgs, f"[{ep.name} / {posture} / {backend}] RED:\n  " + "\n  ".join(msgs)


def test_zzz_print_red_matrix():
    """Print the full (entrypoint × FK × posture) matrix gathered above. Always
    'passes' — it's the report. Runs last (name sorts after test_parent_delete in
    collection order is not guaranteed, so it reads whatever RESULTS exist)."""
    if not RESULTS:
        pytest.skip("no results gathered (run with the rest of the module)")
    print("\n\n================= FK PARENT-DELETE RED MATRIX =================")
    hdr = f"{'entrypoint':<22}{'posture':<8}{'FK':<52}{'ondel':<9}{'state':<7}{'ok'}"
    by_ep: dict[str, list[dict]] = {}
    for r in RESULTS:
        by_ep.setdefault(r["entrypoint"], []).append(r)
    for ep in sorted(by_ep):
        print(f"\n--- {ep} ---")
        print(hdr)
        for r in sorted(by_ep[ep], key=lambda x: (x["posture"], x["fk"])):
            flag = "ok " if r["ok"] else "FAIL"
            extra = "" if r["ok"] else f"  <- {r['reason']}"
            print(
                f"{r['entrypoint']:<22}{r['posture']:<8}{r['fk']:<52}"
                f"{r['ondelete']:<9}{r['state']:<7}{flag}{extra}"
            )
    total = len(RESULTS)
    fails = sum(1 for r in RESULTS if not r["ok"])
    print(f"\nCELLS: {total}   RED: {fails}   GREEN: {total - fails}")
    print("===============================================================\n")
