"""Seed only the disposable container test database, never application data."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url  # noqa: E402

if make_url(os.environ["DATABASE_URL"]).database != "cartarch_test":
    raise SystemExit("Smoke fixtures require the disposable cartarch_test database")

from app.auth import hash_password  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Card, Deck, InventoryRow, StorageLocation, User  # noqa: E402

with SessionLocal() as session:
    assert session.query(User).count() == 0, "Smoke database must be empty"
    user = User(
        username="smoke@example.invalid", password_hash=hash_password("local-smoke-password")
    )
    session.add(user)
    session.flush()
    location = StorageLocation(user_id=user.id, name="Smoke Deck", type="deck", mode="manual")
    box = StorageLocation(
        user_id=user.id,
        name="A long storage location name for layout checks",
        type="box",
        mode="manual",
    )
    session.add_all([location, box])
    session.flush()
    session.add(
        Deck(
            user_id=user.id, name="Smoke Deck", storage_location_id=location.id, format="commander"
        )
    )
    for i in range(100):
        card = Card(
            scryfall_id=f"smoke-{i}",
            name="Forest" if i == 0 else f"Test Artifact {i:03}",
            set_code="tst",
            collector_number=str(i),
            type_line="Basic Land — Forest" if i == 0 else "Artifact",
            mana_cost="{1}",
            cmc=1,
            color_identity="G" if i == 0 else "",
            price_usd="1.00",
        )
        session.add(card)
        session.flush()
        session.add(
            InventoryRow(
                user_id=user.id,
                card_id=card.id,
                quantity=4 if i == 0 else 1,
                storage_location_id=location.id,
                finish="normal",
                is_pending=False,
            )
        )
    session.commit()
print("Seeded 100-card smoke deck")
