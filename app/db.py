"""Database engine, session factory, and startup validation.

The app fails fast if the configured SQLite file is missing. This prevents a
local dev server from silently booting against a fresh empty database.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "mana_archive.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

# ``check_same_thread`` is a SQLite-only DBAPI argument. Passing it to any other
# driver (psycopg/asyncpg at the v4 Postgres cutover) raises at connect time, so
# it is applied only when the configured backend is SQLite. On SQLite the engine
# is created exactly as before; on any other dialect ``connect_args`` is empty.
_is_sqlite = DATABASE_URL.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

# Creating the data directory is a SQLite-only concern (it's where the .db file
# lives). On a Postgres boot (v4) there is no local DB file, so making /data is
# pointless — guard it to the SQLite branch. This was the last unconditional
# SQLite-ism that fired at import time regardless of backend.
if _is_sqlite:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

# ``pool_pre_ping`` validates a pooled connection (and transparently reconnects) before
# use — important on Postgres (v4) behind transaction-mode PgBouncer / across a network
# that can drop idle connections. Not applied to the single-file SQLite engine, whose
# connection never goes stale, so SQLite behavior is unchanged.
#
# Pool sizing (Postgres/non-SQLite only — these QueuePool params must NOT be passed to
# the SQLite engine, whose create_engine call therefore stays byte-identical):
#   - ``pool_size=10``      steady pooled connections for the single replica (request
#                           path + the 3 background writer daemons; comfortably under a
#                           default PG ``max_connections`` even with max_overflow).
#   - ``max_overflow=5``    burst headroom above pool_size under transient load.
#   - ``pool_recycle=1800`` recycle a connection after 30 min idle — PG (and any proxy
#                           in front of it) drops idle connections server-side, so a
#                           connection older than this is proactively replaced rather
#                           than handed out stale. pre_ping catches the rest.
# Single-replica scope (Gate #7); PgBouncer + worker-split tuning is deferred to v4.0.x.
_engine_kwargs: dict = {"connect_args": _connect_args, "pool_pre_ping": not _is_sqlite}
if not _is_sqlite:
    _engine_kwargs.update(pool_size=10, max_overflow=5, pool_recycle=1800)
engine = create_engine(DATABASE_URL, **_engine_kwargs)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Harden SQLite against the Longhorn detach/reattach corruption class.

    The cluster has twice corrupted single-writer DB files when a stateful pod
    rescheduled and its Longhorn volume was detached uncleanly (see the
    SQLite-on-Longhorn corruption analysis in the platform docs). These PRAGMAs,
    set on every connection, address it:

    - ``journal_mode=WAL``: on an abrupt detach the WAL is *replayed* on next
      open rather than leaving a half-written rollback journal — recoverable,
      not "database disk image is malformed". A shutdown checkpoint (below)
      also collapses it to a single consistent file before a clean detach.
    - ``synchronous=NORMAL``: the WAL-safe durability level — no corruption on
      crash, at most the last transaction is lost on power loss.
    - ``busy_timeout``: the three background writer daemons + request path
      contend for the single writer; wait rather than erroring with SQLITE_BUSY.

    These PRAGMAs are SQLite-only syntax. On any other backend (Postgres at v4)
    this listener must no-op rather than run them, so the body is gated on the
    engine dialect. On SQLite the branch runs exactly as it always has.
    """
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


# Set when the app is shutting down. The background writer daemons watch this so
# they can stop between batches, letting the DB be checkpointed and closed
# cleanly before Kubernetes unmounts the volume and Longhorn detaches it.
shutdown_event = threading.Event()


def checkpoint_and_dispose() -> None:
    """Flush the WAL into the main DB file and close all pooled connections.

    The graceful-termination half of the corruption mitigation: after the
    writer daemons have stopped, this leaves a single, consistent database file
    on disk before the volume is unmounted. Best-effort — never raises.
    """
    try:
        # WAL checkpoint is SQLite-only (the Longhorn clean-detach story). On Postgres
        # there is no WAL file to collapse; skip it and just dispose the pool.
        if engine.dialect.name == "sqlite":
            with engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:  # noqa: BLE001 — shutdown path must not raise
        print(f"[shutdown] wal_checkpoint failed: {exc}", flush=True)
    finally:
        engine.dispose()


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)
Base = declarative_base()


def init_db() -> None:
    """Validate the schema is present (create missing tables on SQLite/dev only) and that at least one user exists."""
    # The missing-file guard is a SQLite-only dev safety (don't boot against a fresh
    # empty file). On Postgres (v4) there is no DB file; the schema is owned by Alembic
    # and existence is proven by the user-count check below + a live connection.
    if DATABASE_URL.startswith("sqlite") and not DB_PATH.exists():
        raise RuntimeError(f"Database not found at {DB_PATH}")

    from app import models  # noqa: F401
    from app.models import User

    # create_all is gated to the SQLite (dev) branch only. On Postgres (v4/prod)
    # the schema is owned exclusively by Alembic, applied by the ArgoCD PreSync
    # migration hook before the app rolls. create_all against prod silently
    # created ORM tables without stamping alembic_version, masking unrun
    # migrations until the first ALTER-bearing migration would crashloop — the
    # v4.0.30 ledger-drift incident (2026-06-27). Dev SQLite keeps auto-create
    # for local convenience; the user-count check below still validates the boot.
    if _is_sqlite:
        Base.metadata.create_all(bind=engine)

    with SessionLocal() as session:
        user_count = session.query(User).count()
        if user_count == 0:
            raise RuntimeError("No users found in database. Migration or seed failed.")


def get_session() -> Session:
    """Return a raw session for scripts and non-route callers."""
    return SessionLocal()
