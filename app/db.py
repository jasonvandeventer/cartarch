"""Database engine, session factory, and startup validation.

The app fails fast if the configured SQLite file is missing. This prevents a
local dev server from silently booting against a fresh empty database.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "mana_archive.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
