"""Single source for the application's "now" timestamp.

``utc_now()`` returns **timezone-aware UTC** (``datetime.now(UTC)``). Every
former ``datetime.utcnow()`` call site (and the SQLAlchemy ``default=`` column
callables in ``models.py``) routes through here, so the whole app emits aware
UTC uniformly.

History: this helper was introduced (v3.36.13) as the single flip point for the
naive→aware transition — it returned naive UTC until #130 flipped it here,
paired with Postgres ``timestamptz`` columns (the ``UTCDateTime`` type in
``models.py``, which also normalizes SQLite's naive reads back to aware). The
centralization is what made this a one-line change instead of a 49-site hunt.

No app-layer imports live here by design, so any module (including ``models`` and
``db``) can import it without an import cycle.
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    # #130 — timezone-aware UTC, paired with Postgres ``timestamptz`` columns
    # (the ``UTCDateTime`` type in ``models.py``). This is the single flip point
    # the v3.36.13 centralization set up. Every stored/compared timestamp is now
    # aware UTC end to end.
    return datetime.now(UTC)
