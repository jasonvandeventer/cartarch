"""Shared pytest fixtures (v3.37.0 — test-suite migration to pytest).

Engine-agnostic by design: the ``db_engine`` fixture is a throwaway temp-FILE
SQLite database today, but it is built so the v4 SQLite→Postgres cutover can
repoint it at a Postgres URL by changing ONLY this fixture — then the same
suite becomes the migration's behavioural-equivalence gate (green on SQLite AND
Postgres = equivalence proven, not assumed).

DATA_DIR / DEV_MODE are set here, before any ``app`` import, so that
``app.db`` (which builds a module-global engine from DATA_DIR at import time and
``mkdir``s it) lands in a throwaway temp dir rather than ``/data`` or the real
dev DB. The fixtures below never use that global engine — route tests override
``get_db_session`` to point at the temp engine — but importing the app must not
touch real data.
"""

from __future__ import annotations

import os
import tempfile

# MUST run before any `app.*` import (app.db reads DATA_DIR at import time).
os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cartarch-pytest-"))
os.environ.setdefault("SESSION_SECRET_KEY", "test-only-secret")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import Base  # noqa: E402

# Set TEST_DATABASE_URL to a Postgres URL to run the WHOLE suite against Postgres
# (the v4 dual-backend equivalence gate). Unset → the temp-FILE SQLite behaviour
# below, byte-identical to before. On Postgres the suite shares one database, so
# each engine fixture drops+recreates the schema for a clean per-test slate.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def _make_test_engine(tmp_path, filename, *, fk_on):
    """Build a per-test engine: Postgres if TEST_DATABASE_URL is set, else temp SQLite.

    ``fk_on`` requests FK enforcement — a SQLite-only PRAGMA; Postgres always
    enforces FKs, so the flag is a no-op there (which is exactly the cutover posture).
    """
    if TEST_DATABASE_URL:
        engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
        Base.metadata.drop_all(engine)  # clean slate (shared PG database)
        Base.metadata.create_all(engine)
        return engine

    engine = create_engine(
        f"sqlite:///{tmp_path / filename}",
        connect_args={"check_same_thread": False},
    )
    if fk_on:
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_connection, _record):  # noqa: ANN001
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_engine(tmp_path):
    """A temp-FILE SQLite engine (or Postgres via TEST_DATABASE_URL) with the full schema."""
    engine = _make_test_engine(tmp_path, "test.db", fk_on=False)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db(db_engine):
    """A Session bound to the temp engine. ``expire_on_commit=False`` so objects
    stay usable in assertions after commit (matches the existing suites)."""
    session_factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user(db):
    """A seeded, committed User for authenticated route tests."""
    from app.models import User

    u = User(username="tester@example.com", password_hash="x")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def client(db_engine, user):
    """FastAPI ``TestClient`` with the DB dependency pointed at the temp engine,
    the current user pinned to ``user``, and CSRF disabled — the clean
    dependency-override seam (the same one the v4 cutover repoints at Postgres).
    """
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    session_factory = sessionmaker(bind=db_engine, expire_on_commit=False)

    def _override_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    main.app.dependency_overrides[get_current_user] = lambda: user
    main.app.dependency_overrides[require_csrf_token] = lambda: None
    try:
        yield TestClient(main.app)
    finally:
        for dep in (get_db_session, get_current_user, require_csrf_token):
            main.app.dependency_overrides.pop(dep, None)


@pytest.fixture
def fk_db_engine(tmp_path):
    """FK-enforcing engine — the Postgres cutover posture (production SQLite runs FKs
    OFF). On SQLite this sets ``PRAGMA foreign_keys=ON``; on Postgres (TEST_DATABASE_URL)
    FKs are always enforced, so the same tests run under real PG enforcement. An unclean
    delete that orphans a referencing row raises ``IntegrityError`` either way.
    """
    engine = _make_test_engine(tmp_path, "fk_test.db", fk_on=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def fk_db(fk_db_engine):
    """A Session on the FK-enforcing engine (see ``fk_db_engine``)."""
    session = sessionmaker(bind=fk_db_engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
