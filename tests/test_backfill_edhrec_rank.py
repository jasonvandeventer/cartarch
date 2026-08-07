"""#180 backfill — copies the rank from the bulk cache, writes nothing on a dry run.

The dry-run contract is tested THROUGH A ROLLBACK, not by reading the flag: the
#164 incident was a "dry run" that wrote five decks to production because a
function it called committed on its own.
"""

import app.legacy_tables  # noqa
from app.legacy_tables import scryfall_cards
from app.models import Card
from scripts import backfill_edhrec_rank as bf


def _seed(db, *, sid="sf-1", cache_rank=42, card_rank=None):
    db.add(
        Card(
            name="Ranked",
            scryfall_id=sid,
            set_code="tst",
            set_name="T",
            collector_number="1",
            rarity="rare",
            edhrec_rank=card_rank,
        )
    )
    db.execute(
        scryfall_cards.insert().values(scryfall_id=sid, name="Ranked", edhrec_rank=cache_rank)
    )
    db.commit()


def _run(db, monkeypatch, *, apply=False):
    monkeypatch.setattr(bf, "SessionLocal", lambda: db)
    argv = ["backfill_edhrec_rank"] + (["--apply"] if apply else [])
    monkeypatch.setattr("sys.argv", argv)
    # The script closes its session in `finally`; keep the fixture usable.
    monkeypatch.setattr(db, "close", lambda: None)
    bf.main()


def test_apply_copies_the_rank_from_the_cache(db, monkeypatch):
    _seed(db)
    _run(db, monkeypatch, apply=True)
    assert db.query(Card).one().edhrec_rank == 42


def test_a_dry_run_writes_nothing(db, monkeypatch):
    """Attempt the write, then assert the COMMITTED state is unchanged.

    Asserted from a SEPARATE session on the same engine — checking through
    ``db`` proves nothing, because ``Session.expire_all()`` discards pending
    in-memory changes and the assertion would pass against a script that never
    wrote at all.

    **What this does NOT prove:** deleting the script's ``session.rollback()``
    leaves this green (verified). An uncommitted flush is invisible to another
    connection and would be rolled back at close anyway, so no test at this
    level can distinguish the two. The rollback guards a future edit that adds a
    commit — see the note beside it. Recorded here so nobody later reads this
    test as the rollback's proof.
    """
    from sqlalchemy.orm import sessionmaker

    _seed(db)
    _run(db, monkeypatch, apply=False)

    observer = sessionmaker(bind=db.get_bind())()
    try:
        assert observer.query(Card).one().edhrec_rank is None
    finally:
        observer.close()


def test_rerunning_is_a_no_op(db, monkeypatch):
    _seed(db, card_rank=42)
    _run(db, monkeypatch, apply=True)
    assert db.query(Card).one().edhrec_rank == 42


def test_a_card_the_cache_cannot_rank_stays_null(db, monkeypatch):
    """NULL is 'unknown', not 'unpopular' — basics and tokens carry no rank."""
    _seed(db, cache_rank=None)
    _run(db, monkeypatch, apply=True)
    db.expire_all()
    assert db.query(Card).one().edhrec_rank is None


def test_updated_at_is_left_alone(db, monkeypatch):
    """Advancing it would push the card OUT of the price-refresh staleness
    window and suppress a real metadata refresh it was otherwise due."""
    _seed(db)
    before = db.query(Card).one().updated_at
    _run(db, monkeypatch, apply=True)
    assert db.query(Card).one().updated_at == before
