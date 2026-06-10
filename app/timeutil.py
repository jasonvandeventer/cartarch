"""Single source for the application's "now" timestamp.

``utc_now()`` returns **naive UTC** — byte-for-byte what ``datetime.utcnow()``
returned everywhere before this helper existed. Every former ``datetime.utcnow()``
call site (and the SQLAlchemy ``default=`` column callables in ``models.py``)
routes through here.

Why centralize but NOT change the semantics yet: the v4 SQLite→Postgres cutover
will flip the whole app to timezone-aware time (``datetime.now(UTC)`` paired with
Postgres ``timestamptz``) in ONE deliberate change — and this is the single line
that flips. Until then naive UTC is preserved on purpose: the DB stores naive
datetimes, and mixing naive/aware values against them is exactly the failure mode
this centralization exists to make a one-line fix instead of a 49-site hunt.

No app-layer imports live here by design, so any module (including ``models`` and
``db``) can import it without an import cycle.
"""

from datetime import datetime


def utc_now() -> datetime:
    # v4: flip to ``return datetime.now(UTC)`` alongside Postgres timestamptz,
    # in one deliberate change. Naive UTC today to match the DB-stored values.
    return datetime.utcnow()
