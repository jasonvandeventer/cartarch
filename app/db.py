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
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "mana_archive.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

# ``check_same_thread`` is a SQLite-only DBAPI argument. Passing it to any other
# driver (psycopg/asyncpg at the v4 Postgres cutover) raises at connect time, so
# it is applied only when the configured backend is SQLite. On SQLite the engine
# is created exactly as before; on any other dialect ``connect_args`` is empty.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)


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
    """Create missing tables and validate that at least one user exists."""
    if not DB_PATH.exists():
        raise RuntimeError(f"Database not found at {DB_PATH}")

    from app import models  # noqa: F401
    from app.models import User

    Base.metadata.create_all(bind=engine)

    with SessionLocal() as session:
        user_count = session.query(User).count()
        if user_count == 0:
            raise RuntimeError("No users found in database. Migration or seed failed.")


def get_session() -> Session:
    """Return a raw session for scripts and non-route callers."""
    return SessionLocal()
