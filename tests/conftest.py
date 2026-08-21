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
from app.legacy_tables import metadata as _legacy_metadata  # noqa: E402

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
        _legacy_metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        _legacy_metadata.create_all(engine)
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
    _legacy_metadata.create_all(engine)
    return engine


@pytest.fixture
def db_engine(tmp_path):
    """A temp-FILE SQLite engine (or Postgres via TEST_DATABASE_URL) with the full schema.

    **FKs are ENFORCED**, matching production Postgres. They used to be off, so
    fixtures could reference parents that did not exist — `audit_session_id=1`,
    `showcase_id=1`, `source_deck_id=1` — and eight tests passed on SQLite while
    failing on every Postgres run, which made the PG suite useless as a gate.
    A test that genuinely needs a dangling reference takes `no_fk_db`.
    """
    engine = _make_test_engine(tmp_path, "test.db", fk_on=True)
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
def row_reference_parents(db, user):
    """Real parent rows for everything that can reference an InventoryRow.

    Returns a namespace: ``.showcase``, ``.trade``, ``.variant_group``,
    ``.source_deck``, ``.target_deck``.

    Three "references survive this operation" tests built their references with
    magic ids — ``showcase_id=1``, ``trade_id=1``, ``source_deck_id=1`` — and
    said so in a comment: "FK enforcement is off in the default db fixture, so
    placeholder deck/group ids are fine". True on SQLite, which runs foreign
    keys OFF; on Postgres every one is a ForeignKeyViolation, so those tests
    failed on every PG run and the PG suite could never be a clean gate. The
    parents' identities are incidental to what the tests assert — but they have
    to exist.
    """
    from types import SimpleNamespace

    from app.models import Deck, Showcase, Trade, TradeRevision, VariantGroup

    showcase = Showcase(user_id=user.id, name="Refs")
    trade = Trade(status="proposed")
    vg = VariantGroup(user_id=user.id, name="Variants")
    db.add_all([showcase, trade, vg])
    db.flush()
    # Counter-proposals: a TradeItem belongs to a REVISION (NOT NULL), so a
    # reference-parent trade needs one the way it needs an id.
    trade_revision = TradeRevision(trade_id=trade.id, author_user_id=user.id)
    db.add(trade_revision)
    db.flush()
    source = Deck(user_id=user.id, name="Share Source", variant_group_id=vg.id)
    target = Deck(user_id=user.id, name="Share Target", variant_group_id=vg.id)
    db.add_all([source, target])
    db.flush()
    return SimpleNamespace(
        showcase=showcase,
        trade=trade,
        trade_revision=trade_revision,
        variant_group=vg,
        source_deck=source,
        target_deck=target,
    )


@pytest.fixture(autouse=True)
def _clear_commander_options_cache():
    """The commander suggestion list is cached per-process per DAY (it reads 1.3 MB),
    which would otherwise leak one test's seeded cards into the next."""
    from app.recommendation_service import _COMMANDER_OPTIONS_CACHE

    _COMMANDER_OPTIONS_CACHE.clear()
    yield
    _COMMANDER_OPTIONS_CACHE.clear()


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
def no_fk_db_engine(tmp_path):
    """FK enforcement OFF — for the two tests whose SUBJECT is an orphan row.

    The default `db_engine` enforces foreign keys, matching production Postgres.
    A test that must CREATE a dangling reference (the orphan sweep, the
    "[orphaned location]" stats branch) cannot do that under enforcement, so it
    opts out here rather than the whole suite opting out for it — which is how
    a fixture bug like `audit_session_id=1` stayed invisible for a year.

    On Postgres `fk_on` is a no-op (FKs are always enforced), so a test needing
    a real orphan must additionally skip there; that is a true statement about
    production, not a gap.
    """
    engine = _make_test_engine(tmp_path, "no_fk_test.db", fk_on=False)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def no_fk_db(no_fk_db_engine):
    """A Session on the non-enforcing engine (see ``no_fk_db_engine``)."""
    session = sessionmaker(bind=no_fk_db_engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


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


@pytest.fixture(autouse=True)
def _reset_live_overlap_registry():
    """Clear #155's in-process overlap registry between tests.

    It keys on ``game_id`` in module-global state. Every test gets a FRESH database, so
    ids restart at 1 and the previous test's version history reads as "someone already
    wrote version N" — a false LOST UPDATE. pytest only surfaces captured logs for
    FAILING tests, so this was silently polluting passing runs.

    Test isolation only, NOT a production concern: prod has one long-lived database where
    a given game id's version only ever climbs. Deliberately not "fixed" by weakening the
    detector to an exact-equality check — ``>=`` is what catches a writer that clobbered
    two earlier commits, and that case is real.
    """
    from app import live_game_service

    live_game_service._last_written_version.clear()
    live_game_service._live_in_flight.clear()
    yield


# ---------------------------------------------------------------------------
# Template render tracking — feeds tests/test_template_coverage.py
# ---------------------------------------------------------------------------
#
# A template no test ever RENDERS can drift indefinitely: prod's Jinja is
# permissive (see #157), so a missing context key renders empty rather than
# raising, and nothing surfaces it. That is exactly how import_preview.html came
# to read `row.location_type` — a key `parse_text_list` never emits — and sit
# that way for months until v4.13.23 finally loaded the page in a test and the
# strict env raised.
#
# Hooking the LOADER rather than `get_template` is deliberate: `{% extends %}`
# and `{% include %}` resolve through `loader.get_source`, so a partial reached
# only via inheritance still counts as rendered.
RENDERED_TEMPLATES: set[str] = set()


def pytest_configure(config):  # noqa: D401 — pytest hook
    from app.dependencies import templates

    loader = templates.env.loader
    if loader is None or getattr(loader, "_cartarch_tracked", False):
        return
    original = loader.get_source

    def get_source(environment, template):
        RENDERED_TEMPLATES.add(str(template))
        return original(environment, template)

    loader.get_source = get_source
    loader._cartarch_tracked = True
