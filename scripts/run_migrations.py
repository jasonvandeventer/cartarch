from app.db import engine
from app.migrations import ensure_migrations_table, has_migration, record_migration
from scripts.migrate_v3_4_decks_as_locations import main as migrate_v3_4
from scripts.migrate_v3_5_drop_deck_items import main as migrate_v3_5_deck_items
from scripts.migrate_v3_5_inventory_role import main as migrate_v3_5_role
from scripts.migrate_v3_7_admin_user import main as migrate_v3_7_admin
from scripts.migrate_v3_8_8_color_identity import main as migrate_v3_8_8_color_identity
from scripts.migrate_v3_8_card_attrs import main as migrate_v3_8_card_attrs
from scripts.migrate_v3_9_5_row_tags import main as migrate_v3_9_5_row_tags
from scripts.migrate_v3_9_6_legalities import main as migrate_v3_9_6_legalities
from scripts.migrate_v3_11_3_clear_deck_pending import main as migrate_v3_11_3_clear_deck_pending
from scripts.migrate_v3_11_display_name import main as migrate_v3_11_display_name
from scripts.migrate_v3_13_games import main as migrate_v3_13_games
from scripts.migrate_v3_14_20_scrub_legacy_tags import main as migrate_v3_14_20_scrub_legacy_tags
from scripts.migrate_v3_14_seat_position import main as migrate_v3_14_seat_position
from scripts.migrate_v3_15_0_bracket_v2_tables import main as migrate_v3_15_0_bracket_v2_tables
from scripts.migrate_v3_15_0_seed_bracket_rules import main as migrate_v3_15_0_seed_bracket_rules
from scripts.migrate_v3_15_0_seed_card_tags import main as migrate_v3_15_0_seed_card_tags
from scripts.migrate_v3_15_0_seed_game_changers import main as migrate_v3_15_0_seed_game_changers
from scripts.migrate_v3_15_1_bracket_v2_intent_confidence import (
    main as migrate_v3_15_1_intent_confidence,
)
from scripts.migrate_v3_16_0_token_inventory import main as migrate_v3_16_0_token_inventory


def _is_applied(name: str) -> bool:
    with engine.connect() as conn:
        return has_migration(conn, name)


def _mark_applied(name: str) -> None:
    with engine.begin() as conn:
        record_migration(conn, name)


def run():
    print("Starting migration runner")

    ensure_migrations_table()

    if not _is_applied("v3_4_decks_as_locations"):
        print("Running v3.4 migration...")
        migrate_v3_4()
        _mark_applied("v3_4_decks_as_locations")
    else:
        print("v3.4 migration already applied, skipping")

    if not _is_applied("v3_5_drop_deck_items"):
        print("Running v3.5 drop deck_items migration...")
        migrate_v3_5_deck_items()
        _mark_applied("v3_5_drop_deck_items")
    else:
        print("v3.5 drop_deck_items already applied, skipping")

    if not _is_applied("v3_5_inventory_role"):
        print("Running v3.5 inventory role migration...")
        migrate_v3_5_role()
        _mark_applied("v3_5_inventory_role")
    else:
        print("v3.5 inventory_role already applied, skipping")

    if not _is_applied("v3_7_admin_user"):
        print("Running v3.7 admin user migration...")
        migrate_v3_7_admin()
        _mark_applied("v3_7_admin_user")
    else:
        print("v3.7 admin_user already applied, skipping")

    if not _is_applied("v3_8_card_attrs"):
        print("Running v3.8 card attrs migration...")
        migrate_v3_8_card_attrs()
        _mark_applied("v3_8_card_attrs")
    else:
        print("v3.8 card_attrs already applied, skipping")

    if not _is_applied("v3_8_8_color_identity"):
        print("Running v3.8.8 color_identity migration...")
        migrate_v3_8_8_color_identity()
        _mark_applied("v3_8_8_color_identity")
    else:
        print("v3.8.8 color_identity already applied, skipping")

    if not _is_applied("v3_9_5_row_tags"):
        print("Running v3.9.5 row tags migration...")
        migrate_v3_9_5_row_tags()
        _mark_applied("v3_9_5_row_tags")
    else:
        print("v3.9.5 row_tags already applied, skipping")

    if not _is_applied("v3_9_6_legalities"):
        print("Running v3.9.6 legalities migration...")
        migrate_v3_9_6_legalities()
        _mark_applied("v3_9_6_legalities")
    else:
        print("v3.9.6 legalities already applied, skipping")

    if not _is_applied("v3_11_display_name"):
        print("Running v3.11 display_name migration...")
        migrate_v3_11_display_name()
        _mark_applied("v3_11_display_name")
    else:
        print("v3.11 display_name already applied, skipping")

    if not _is_applied("v3_11_3_clear_deck_pending"):
        print("Running v3.11.3 clear deck pending migration...")
        migrate_v3_11_3_clear_deck_pending()
        _mark_applied("v3_11_3_clear_deck_pending")
    else:
        print("v3.11.3 clear_deck_pending already applied, skipping")

    if not _is_applied("v3_13_games"):
        print("Running v3.13 games migration...")
        migrate_v3_13_games()
        _mark_applied("v3_13_games")
    else:
        print("v3.13 games already applied, skipping")

    if not _is_applied("v3_14_seat_position"):
        print("Running v3.14 seat position migration...")
        migrate_v3_14_seat_position()
        _mark_applied("v3_14_seat_position")
    else:
        print("v3.14 seat_position already applied, skipping")

    if not _is_applied("v3_14_20_scrub_legacy_tags"):
        print("Running v3.14.20 legacy-tag scrub migration...")
        migrate_v3_14_20_scrub_legacy_tags()
        _mark_applied("v3_14_20_scrub_legacy_tags")
    else:
        print("v3.14.20 scrub_legacy_tags already applied, skipping")

    if not _is_applied("v3_15_0_bracket_v2_tables"):
        print("Running v3.15.0 Bracket V2 schema migration...")
        migrate_v3_15_0_bracket_v2_tables()
        _mark_applied("v3_15_0_bracket_v2_tables")
    else:
        print("v3.15.0 bracket_v2_tables already applied, skipping")

    if not _is_applied("v3_15_0_seed_bracket_rules"):
        print("Running v3.15.0 bracket rules seed...")
        migrate_v3_15_0_seed_bracket_rules()
        _mark_applied("v3_15_0_seed_bracket_rules")
    else:
        print("v3.15.0 seed_bracket_rules already applied, skipping")

    if not _is_applied("v3_15_0_seed_game_changers"):
        print("Running v3.15.0 Game Changer seed...")
        migrate_v3_15_0_seed_game_changers()
        _mark_applied("v3_15_0_seed_game_changers")
    else:
        print("v3.15.0 seed_game_changers already applied, skipping")

    if not _is_applied("v3_15_0_seed_card_tags"):
        print("Running v3.15.0 card-tag auto-seed...")
        migrate_v3_15_0_seed_card_tags()
        _mark_applied("v3_15_0_seed_card_tags")
    else:
        print("v3.15.0 seed_card_tags already applied, skipping")

    if not _is_applied("v3_15_1_bracket_v2_intent_confidence"):
        print("Running v3.15.1 Bracket V2 intent + confidence schema...")
        migrate_v3_15_1_intent_confidence()
        _mark_applied("v3_15_1_bracket_v2_intent_confidence")
    else:
        print("v3.15.1 intent_confidence already applied, skipping")

    if not _is_applied("v3_16_0_token_inventory"):
        print("Running v3.16.0 token inventory schema...")
        migrate_v3_16_0_token_inventory()
        _mark_applied("v3_16_0_token_inventory")
    else:
        print("v3.16.0 token_inventory already applied, skipping")

    print("Migration runner complete")


if __name__ == "__main__":
    run()
