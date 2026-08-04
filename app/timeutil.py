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
from zoneinfo import ZoneInfo

# The playgroup's wall clock. Stored timestamps are aware UTC end to end (#130);
# this is the zone anything DATE-GRAINED must be read in, because a calendar day
# is a local fact. Lives here rather than in ``dependencies`` so modules with no
# app-layer imports (``session_service``, scripts) can use the same definition —
# a second copy is how "the display says Sunday and the grouping says Monday"
# happens. ``dependencies.format_local_datetime`` renders through this too.
#
# Single-tenant today. A per-playgroup or per-user zone is a one-line swap here
# plus a lookup at the call sites, and is the upgrade path if it is ever needed.
LOCAL_TZ = ZoneInfo("America/Chicago")


def local_date(moment: datetime):
    """The CALENDAR DATE a moment falls on, in the playgroup's local zone.

    #166 — grouping games into sessions by ``played_at::date`` in **UTC** splits
    one evening in two the moment play runs past 19:00 Central (00:00 UTC). No
    game in prod has yet, because this playgroup plays Sunday *afternoons* — but
    game 64 already demonstrates the shift: stored ``2026-06-28 00:00 UTC``, it
    is **Saturday 2026-06-27 19:00** locally, a different day entirely.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(LOCAL_TZ).date()


def utc_now() -> datetime:
    # #130 — timezone-aware UTC, paired with Postgres ``timestamptz`` columns
    # (the ``UTCDateTime`` type in ``models.py``). This is the single flip point
    # the v3.36.13 centralization set up. Every stored/compared timestamp is now
    # aware UTC end to end.
    return datetime.now(UTC)
