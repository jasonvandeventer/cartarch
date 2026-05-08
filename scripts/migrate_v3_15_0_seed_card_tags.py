"""Run the bracket-v2 auto-tagger across every Card row and write to card_tags.

This is a one-shot seed for V1. Future card additions (via Scryfall import)
can re-run the same logic incrementally — TODO: hook into the price-refresh
loop in a follow-up so newly-imported cards get auto-tagged.
"""

from __future__ import annotations

from app.bracket_v2_service import tag_card_from_oracle, upsert_card_tags
from app.db import SessionLocal
from app.models import Card


def main() -> None:
    session = SessionLocal()
    try:
        cards = session.query(Card).all()
        tagged = 0
        total_tags = 0
        for card in cards:
            tags = tag_card_from_oracle(card)
            if tags:
                upsert_card_tags(session, card.id, tags)
                tagged += 1
                total_tags += len(tags)
        session.commit()
        print(f"Auto-tagged {tagged} of {len(cards)} cards ({total_tags} tags written)")
    finally:
        session.close()


if __name__ == "__main__":
    main()
