"""#166 — backfill historical games into playgroup-scoped sessions.

Idempotent. Run with ``--apply`` to write; the default is a dry run that prints
exactly what it would do and writes nothing.

    python -m scripts.backfill_game_sessions            # dry run
    python -m scripts.backfill_game_sessions --apply

**Games with no playgroup get NO session, and that is a decision, not a gap.** A
session belongs to a playgroup, so an unaffiliated game has nothing to belong to.
Prod has exactly one — game 64, SaintWacko playing with non-members, stamped
``00:00:00`` by the manual-log default. It is reported explicitly rather than
skipped silently, because a gap you know about is a different thing from one you
don't.

**Game 64 is also the argument for the whole model.** 2026-06-28 holds four
finalized games spanning 20.2 hours with a 16.2-hour internal gap: game 64 at
midnight with no playgroup, then a real meetup from 16:10 to 20:09. **Date
clustering folds a foreign game into a playgroup's session.** Scoping by
playgroup excludes it for free — no clock rule required, and no clock rule could
have done it, since nothing about a midnight timestamp distinguishes a default
from a real one.

**Date grouping is used HERE and nowhere else.** Historical games predate
sessions, so grouping them at all requires inferring a boundary; new games join
their playgroup's OPEN session, which is a social boundary set by a person.

## The expected numbers moved, and the issue's acceptance criterion is stale

#166 was written 2026-07-27 and specifies **44** deck-sessions. Re-measured
2026-08-04 the correct answer is **52**, and the difference is not drift in this
script — it is 10 seats that gained a ``deck_id`` in the meantime, on 7 #164
placeholder decks, via #164's backfill and #175's finalize capture. Excluding
``contents_tracked = false`` decks reproduces the old 45/44/18 exactly.

**Placeholder decks are NOT excluded here.** A placeholder is a deck that was
physically brought to a table and played; the house rule applied to it, and the
session record is about what was played rather than about what Cartarch knows the
contents of. Excluding them would also make this the second reader of
``contents_tracked``, which ``tests/test_history_durability.py`` guards
deliberately.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from app.db import SessionLocal
from app.models import Game, GameSession
from app.session_service import FINALIZED


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        before_sessions = session.query(GameSession).count()
        before_attached = session.query(Game).filter(Game.session_id.isnot(None)).count()
        print(f"before: {before_sessions} sessions, {before_attached} games attached")

        games = (
            session.query(Game)
            .filter(Game.status == FINALIZED)
            .order_by(Game.played_at, Game.id)
            .all()
        )

        unaffiliated = [g for g in games if g.playgroup_id is None]
        for g in unaffiliated:
            print(
                f"  NO SESSION game {g.id} @ {g.played_at} — no playgroup "
                f"(a session belongs to a playgroup; this is permanent, not pending)"
            )

        # (playgroup, calendar date) is the historical grouping key. Runtime uses
        # the open session instead — see the module docstring.
        buckets: dict[tuple[int, object], list[Game]] = defaultdict(list)
        for g in games:
            if g.playgroup_id is None:
                continue
            buckets[(g.playgroup_id, g.played_at.date())].append(g)

        created = attached = already = 0
        for (playgroup_id, day), bucket in sorted(buckets.items(), key=lambda kv: str(kv[0])):
            existing_ids = {g.session_id for g in bucket if g.session_id is not None}
            if len(existing_ids) == 1:
                # Idempotency: this bucket already has a session. Attach any
                # stragglers to it rather than minting a second one.
                target_id = existing_ids.pop()
                target = session.get(GameSession, target_id)
            elif existing_ids:
                print(
                    f"  SKIP playgroup {playgroup_id} {day}: games already split across "
                    f"{len(existing_ids)} sessions {sorted(existing_ids)} — not merging automatically"
                )
                continue
            else:
                target = GameSession(
                    playgroup_id=playgroup_id,
                    started_at=bucket[0].played_at,
                    # CLOSED on creation: a historical evening is over. Leaving it
                    # open would also collide with the partial unique index the
                    # moment a second historical day was processed.
                    ended_at=bucket[-1].played_at,
                )
                session.add(target)
                session.flush()
                created += 1
                print(
                    f"  SESSION playgroup {playgroup_id} {day}: {len(bucket)} games "
                    f"({bucket[0].played_at:%H:%M}–{bucket[-1].played_at:%H:%M})"
                )
            for g in bucket:
                if g.session_id == target.id:
                    already += 1
                    continue
                g.session_id = target.id
                attached += 1

        print(
            f"\n{'APPLIED' if args.apply else 'DRY RUN'}: "
            f"{created} sessions created, {attached} games attached, "
            f"{already} already correct, {len(unaffiliated)} left sessionless"
        )
        if args.apply:
            session.commit()
            print("committed")
        else:
            # Roll back so a dry run is genuinely a dry run. The #164 incident is
            # why this is explicit rather than trusted to "we never called commit":
            # a nested call that commits on its own turns a dry run into a write.
            session.rollback()
            print("rolled back — nothing was written")
    finally:
        session.close()


if __name__ == "__main__":
    main()
