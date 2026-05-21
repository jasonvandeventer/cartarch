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
from scripts.migrate_v3_16_1_token_back_set_collector import (
    main as migrate_v3_16_1_token_back_set_collector,
)
from scripts.migrate_v3_18_0_inventory_language import main as migrate_v3_18_0_inventory_language
from scripts.migrate_v3_19_0_inventory_is_proxy import main as migrate_v3_19_0_inventory_is_proxy
from scripts.migrate_v3_19_1_inventory_from_position import (
    main as migrate_v3_19_1_inventory_from_position,
)
from scripts.migrate_v3_19_2_backfill_from_position import (
    main as migrate_v3_19_2_backfill_from_position,
)
from scripts.migrate_v3_20_0_user_deck_view_prefs import (
    main as migrate_v3_20_0_user_deck_view_prefs,
)
from scripts.migrate_v3_22_0_tag_confidence_model import (
    main as migrate_v3_22_0_tag_confidence_model,
)
from scripts.migrate_v3_23_3_promote_intrinsic_auto_certain import (
    main as migrate_v3_23_3_promote_intrinsic_auto_certain,
)
from scripts.migrate_v3_23_7_token_dfc_backfill import (
    main as migrate_v3_23_7_token_dfc_backfill,
)
from scripts.migrate_v3_23_8_card_traits import main as migrate_v3_23_8_card_traits
from scripts.migrate_v3_25_0_scryfall_cards import main as migrate_v3_25_0_scryfall_cards
from scripts.migrate_v3_25_1_first_seat_number import main as migrate_v3_25_1_first_seat_number
from scripts.migrate_v3_26_2_storage_location_mode import (
    main as migrate_v3_26_2_storage_location_mode,
)
from scripts.migrate_v3_26_6_game_seats_art_background_hidden import (
    main as migrate_v3_26_6_game_seats_art_background_hidden,
)


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

    if not _is_applied("v3_16_1_token_back_set_collector"):
        print("Running v3.16.1 token back set/collector columns...")
        migrate_v3_16_1_token_back_set_collector()
        _mark_applied("v3_16_1_token_back_set_collector")
    else:
        print("v3.16.1 token_back_set_collector already applied, skipping")

    if not _is_applied("v3_18_0_inventory_language"):
        print("Running v3.18.0 inventory language column migration...")
        migrate_v3_18_0_inventory_language()
        _mark_applied("v3_18_0_inventory_language")
    else:
        print("v3.18.0 inventory_language already applied, skipping")

    if not _is_applied("v3_19_0_inventory_is_proxy"):
        print("Running v3.19.0 inventory is_proxy column migration...")
        migrate_v3_19_0_inventory_is_proxy()
        _mark_applied("v3_19_0_inventory_is_proxy")
    else:
        print("v3.19.0 inventory_is_proxy already applied, skipping")

    if not _is_applied("v3_19_1_inventory_from_position"):
        print("Running v3.19.1 inventory from_drawer/from_slot migration...")
        migrate_v3_19_1_inventory_from_position()
        _mark_applied("v3_19_1_inventory_from_position")
    else:
        print("v3.19.1 inventory_from_position already applied, skipping")

    if not _is_applied("v3_19_2_backfill_from_position"):
        print("Running v3.19.2 backfill of from_drawer/from_slot from audit log...")
        migrate_v3_19_2_backfill_from_position()
        _mark_applied("v3_19_2_backfill_from_position")
    else:
        print("v3.19.2 backfill_from_position already applied, skipping")

    if not _is_applied("v3_20_0_user_deck_view_prefs"):
        print("Running v3.20.0 user deck view prefs migration...")
        migrate_v3_20_0_user_deck_view_prefs()
        _mark_applied("v3_20_0_user_deck_view_prefs")
    else:
        print("v3.20.0 user_deck_view_prefs already applied, skipping")

    if not _is_applied("v3_22_0_tag_confidence_model"):
        print("Running v3.22.0 tag confidence model migration...")
        migrate_v3_22_0_tag_confidence_model()
        _mark_applied("v3_22_0_tag_confidence_model")
    else:
        print("v3.22.0 tag_confidence_model already applied, skipping")

    if not _is_applied("v3_23_3_promote_intrinsic_auto_certain"):
        print("Running v3.23.3 intrinsic-tag promotion migration...")
        migrate_v3_23_3_promote_intrinsic_auto_certain()
        _mark_applied("v3_23_3_promote_intrinsic_auto_certain")
    else:
        print("v3.23.3 promote_intrinsic_auto_certain already applied, skipping")

    if not _is_applied("v3_23_7_token_dfc_backfill"):
        print("Running v3.23.7 token DFC backfill migration...")
        migrate_v3_23_7_token_dfc_backfill()
        _mark_applied("v3_23_7_token_dfc_backfill")
    else:
        print("v3.23.7 token_dfc_backfill already applied, skipping")

    if not _is_applied("v3_23_8_card_traits"):
        print("Running v3.23.8 card-traits column migration...")
        migrate_v3_23_8_card_traits()
        _mark_applied("v3_23_8_card_traits")
    else:
        print("v3.23.8 card_traits already applied, skipping")

    if not _is_applied("v3_25_0_scryfall_cards"):
        print("Running v3.25.0 scryfall_cards cache schema migration...")
        migrate_v3_25_0_scryfall_cards()
        _mark_applied("v3_25_0_scryfall_cards")
    else:
        print("v3.25.0 scryfall_cards already applied, skipping")

    if not _is_applied("v3_25_1_first_seat_number"):
        print("Running v3.25.1 games.first_seat_number migration...")
        migrate_v3_25_1_first_seat_number()
        _mark_applied("v3_25_1_first_seat_number")
    else:
        print("v3.25.1 first_seat_number already applied, skipping")

    if not _is_applied("v3_26_2_storage_location_mode"):
        print("Running v3.26.2 storage_locations.mode migration...")
        migrate_v3_26_2_storage_location_mode()
        _mark_applied("v3_26_2_storage_location_mode")
    else:
        print("v3.26.2 storage_location_mode already applied, skipping")

    if not _is_applied("v3_26_6_game_seats_art_background_hidden"):
        print("Running v3.26.6 game_seats.art_background_hidden migration...")
        migrate_v3_26_6_game_seats_art_background_hidden()
        _mark_applied("v3_26_6_game_seats_art_background_hidden")
    else:
        print("v3.26.6 art_background_hidden already applied, skipping")

    print("Migration runner complete")


if __name__ == "__main__":
    run()
