"""#155 — a detected lost update survives the pod that detected it.

The v4.12.7 instrumentation logs to stdout. The cluster runs no aggregator,
``kubectl logs --previous`` is gone after one restart, and prod restarts on every
deploy — so the evidence #153 needs is erased before anyone reads it. Measured
2026-08-07: zero live games since the instrumentation shipped, so it had never
observed a single action, and the first one's evidence would have been lost too.

Only CLOBBERS are persisted. The denominator is free — ``game_events`` already
holds one row per applied action.
"""

import app.legacy_tables  # noqa
import app.live_game_service as lgs
from app.models import LiveActionConflict


def _reset():
    lgs._last_written_version.clear()


def _record(db, monkeypatch, *, game_id=1, v_read=1, v_written=2, atype="life"):
    import app.db

    # `_persist_live_conflict` imports SessionLocal from app.db INSIDE the
    # function, so app.db is the seam to patch — patching the caller module
    # would silently do nothing and the test would pass while writing nowhere.
    monkeypatch.setattr(app.db, "SessionLocal", lambda: db, raising=True)
    monkeypatch.setattr(db, "close", lambda: None)
    lgs._record_live_action(game_id, 7, atype, v_read, v_written, "2026-08-07T00:00:00", 0.0, 0)


def test_a_clean_action_persists_nothing(db, monkeypatch):
    """Concurrency alone is not the defect — only a CLOBBER is worth a row."""
    _reset()
    _record(db, monkeypatch, v_read=1, v_written=2)
    assert db.query(LiveActionConflict).count() == 0


def test_a_lost_update_is_persisted(db, monkeypatch):
    """Two commits writing the SAME version: the second discarded the first."""
    _reset()
    _record(db, monkeypatch, v_read=1, v_written=2)
    _record(db, monkeypatch, v_read=1, v_written=2)  # same version written twice

    row = db.query(LiveActionConflict).one()
    assert (row.game_id, row.version_read, row.version_written) == (1, 1, 2)
    assert row.action_type == "life"
    assert row.already_written == 2


def test_the_row_outlives_the_process_that_detected_it(db, monkeypatch):
    """THE point of the table: clearing the in-process detector — what a pod
    restart does — must not take the evidence with it."""
    _reset()
    _record(db, monkeypatch, v_read=1, v_written=2)
    _record(db, monkeypatch, v_read=1, v_written=2)
    assert db.query(LiveActionConflict).count() == 1

    lgs._last_written_version.clear()  # the restart
    assert db.query(LiveActionConflict).count() == 1


def test_a_persist_failure_never_breaks_the_action(db, monkeypatch):
    """Instrumentation may never fail a live game action — pinned by breaking it
    deliberately, the same way the v4.12.7 tests pin the logging path."""
    _reset()

    def _boom():
        raise RuntimeError("database on fire")

    import app.db

    monkeypatch.setattr(app.db, "SessionLocal", _boom, raising=True)
    # Must not raise.
    lgs._record_live_action(1, 7, "life", 1, 2, "2026-08-07T00:00:00", 0.0, 0)
    lgs._record_live_action(1, 7, "life", 1, 2, "2026-08-07T00:00:00", 0.0, 0)
