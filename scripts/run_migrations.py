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
from scripts.migrate_v3_27_0_client_token import main as migrate_v3_27_0_client_token
from scripts.migrate_v3_27_0b_1_deck_identity_snapshot import (
    main as migrate_v3_27_0b_1_deck_identity_snapshot,
)
from scripts.migrate_v3_27_2_format_normalization import (
    main as migrate_v3_27_2_format_normalization,
)
from scripts.migrate_v3_27_3_game_status import main as migrate_v3_27_3_game_status
from scripts.migrate_v3_27_4_user_last_signed_in_at import (
    main as migrate_v3_27_4_user_last_signed_in_at,
)
from scripts.migrate_v3_27_5_seat_user_attribution import (
    main as migrate_v3_27_5_seat_user_attribution,
)
from scripts.migrate_v3_27_12_watchlist import main as migrate_v3_27_12_watchlist
from scripts.migrate_v3_27_14_password_reset_tokens import (
    main as migrate_v3_27_14_password_reset_tokens,
)
from scripts.migrate_v3_28_6_storage_location_note_and_capacity import (
    main as migrate_v3_28_6_storage_location_note_and_capacity,
)
from scripts.migrate_v3_28_7_deck_blurb import (
    main as migrate_v3_28_7_deck_blurb,
)
from scripts.migrate_v3_28_11_watchlist_target_price import (
    main as migrate_v3_28_11_watchlist_target_price,
)
from scripts.migrate_v3_29_0_playgroups import main as migrate_v3_29_0_playgroups
from scripts.migrate_v3_29_1_collection_sharing import (
    main as migrate_v3_29_1_collection_sharing,
)
from scripts.migrate_v3_29_2_pairwise_trading import (
    main as migrate_v3_29_2_pairwise_trading,
)
from scripts.migrate_v3_30_11_card_produced_tokens import (
    main as migrate_v3_30_11_card_produced_tokens,
)
from scripts.migrate_v3_31_0_multi_showcase import (
    main as migrate_v3_31_0_multi_showcase,
)
from scripts.migrate_v3_32_0_game_playgroup import (
    main as migrate_v3_32_0_game_playgroup,
)
from scripts.migrate_v3_33_0_variant_groups import (
    main as migrate_v3_33_0_variant_groups,
)
from scripts.migrate_v3_33_2_game_ended_at import (
    main as migrate_v3_33_2_game_ended_at,
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

    if not _is_applied("v3_27_0_client_token"):
        print("Running v3.27.0 games.client_token migration...")
        migrate_v3_27_0_client_token()
        _mark_applied("v3_27_0_client_token")
    else:
        print("v3.27.0 client_token already applied, skipping")

    if not _is_applied("v3_27_0b_1_deck_identity_snapshot"):
        print("Running v3.27.0b-1 game_seats deck identity snapshot migration...")
        migrate_v3_27_0b_1_deck_identity_snapshot()
        _mark_applied("v3_27_0b_1_deck_identity_snapshot")
    else:
        print("v3.27.0b-1 deck_identity_snapshot already applied, skipping")

    if not _is_applied("v3_27_2_format_normalization"):
        print("Running v3.27.2 games.format normalization migration...")
        migrate_v3_27_2_format_normalization()
        _mark_applied("v3_27_2_format_normalization")
    else:
        print("v3.27.2 format_normalization already applied, skipping")

    if not _is_applied("v3_27_3_game_status"):
        print("Running v3.27.3 games.status enum migration...")
        migrate_v3_27_3_game_status()
        _mark_applied("v3_27_3_game_status")
    else:
        print("v3.27.3 game_status already applied, skipping")

    if not _is_applied("v3_27_4_user_last_signed_in_at"):
        print("Running v3.27.4 users.last_signed_in_at migration...")
        migrate_v3_27_4_user_last_signed_in_at()
        _mark_applied("v3_27_4_user_last_signed_in_at")
    else:
        print("v3.27.4 last_signed_in_at already applied, skipping")

    if not _is_applied("v3_27_5_seat_user_attribution"):
        print("Running v3.27.5 game_seats seat→user attribution migration...")
        migrate_v3_27_5_seat_user_attribution()
        _mark_applied("v3_27_5_seat_user_attribution")
    else:
        print("v3.27.5 seat_user_attribution already applied, skipping")

    if not _is_applied("v3_27_12_watchlist"):
        print("Running v3.27.12 watchlist table migration...")
        migrate_v3_27_12_watchlist()
        _mark_applied("v3_27_12_watchlist")
    else:
        print("v3.27.12 watchlist already applied, skipping")

    if not _is_applied("v3_27_14_password_reset_tokens"):
        print("Running v3.27.14 password_reset_tokens migration...")
        migrate_v3_27_14_password_reset_tokens()
        _mark_applied("v3_27_14_password_reset_tokens")
    else:
        print("v3.27.14 password_reset_tokens already applied, skipping")

    if not _is_applied("v3_28_6_storage_location_note_and_capacity"):
        print("Running v3.28.6 storage_locations note + capacity migration...")
        migrate_v3_28_6_storage_location_note_and_capacity()
        _mark_applied("v3_28_6_storage_location_note_and_capacity")
    else:
        print("v3.28.6 storage_location_note_and_capacity already applied, skipping")

    if not _is_applied("v3_28_7_deck_blurb"):
        print("Running v3.28.7 decks.blurb migration...")
        migrate_v3_28_7_deck_blurb()
        _mark_applied("v3_28_7_deck_blurb")
    else:
        print("v3.28.7 deck_blurb already applied, skipping")

    if not _is_applied("v3_28_11_watchlist_target_price"):
        print("Running v3.28.11 watchlist.target_price migration...")
        migrate_v3_28_11_watchlist_target_price()
        _mark_applied("v3_28_11_watchlist_target_price")
    else:
        print("v3.28.11 watchlist_target_price already applied, skipping")

    if not _is_applied("v3_29_0_playgroups"):
        print("Running v3.29.0 playgroups migration...")
        migrate_v3_29_0_playgroups()
        _mark_applied("v3_29_0_playgroups")
    else:
        print("v3.29.0 playgroups already applied, skipping")

    if not _is_applied("v3_29_1_collection_sharing"):
        print("Running v3.29.1 collection sharing migration...")
        migrate_v3_29_1_collection_sharing()
        _mark_applied("v3_29_1_collection_sharing")
    else:
        print("v3.29.1 collection_sharing already applied, skipping")

    if not _is_applied("v3_29_2_pairwise_trading"):
        print("Running v3.29.2 pairwise trading migration...")
        migrate_v3_29_2_pairwise_trading()
        _mark_applied("v3_29_2_pairwise_trading")
    else:
        print("v3.29.2 pairwise_trading already applied, skipping")

    if not _is_applied("v3_30_11_card_produced_tokens"):
        print("Running v3.30.11 card produced_tokens migration...")
        migrate_v3_30_11_card_produced_tokens()
        _mark_applied("v3_30_11_card_produced_tokens")
    else:
        print("v3.30.11 card_produced_tokens already applied, skipping")

    if not _is_applied("v3_31_0_multi_showcase"):
        print("Running v3.31.0 multi-showcase migration...")
        migrate_v3_31_0_multi_showcase()
        _mark_applied("v3_31_0_multi_showcase")
    else:
        print("v3.31.0 multi_showcase already applied, skipping")

    if not _is_applied("v3_32_0_game_playgroup"):
        print("Running v3.32.0 games.playgroup_id migration...")
        migrate_v3_32_0_game_playgroup()
        _mark_applied("v3_32_0_game_playgroup")
    else:
        print("v3.32.0 game_playgroup already applied, skipping")

    if not _is_applied("v3_33_0_variant_groups"):
        print("Running v3.33.0 variant_groups migration...")
        migrate_v3_33_0_variant_groups()
        _mark_applied("v3_33_0_variant_groups")
    else:
        print("v3.33.0 variant_groups already applied, skipping")

    if not _is_applied("v3_33_2_game_ended_at"):
        print("Running v3.33.2 games.ended_at migration...")
        migrate_v3_33_2_game_ended_at()
        _mark_applied("v3_33_2_game_ended_at")
    else:
        print("v3.33.2 game_ended_at already applied, skipping")

    print("Migration runner complete")


if __name__ == "__main__":
    run()
