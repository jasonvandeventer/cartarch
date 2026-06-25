"""Import subsystem routes (extracted from main.py during the v4 reorg).

Covers the full card-import flow: the /import landing page, the CSV / paste-list /
manual preview + reconcile-preview + commit routes, and their private helpers
(_parsed_rows_from_form, normalize_proxy_value_for_commit, _build_line_to_location_map,
_deck_for_storage_location, _annotate_collection_dupes, and the two
_commit_*_with_reconciliation handlers).

Behaviour is byte-identical to the pre-extraction handlers in main.py — this move
changes wiring only, not logic. import_commit's accepted high complexity (F-grade)
was deliberately NOT refactored as part of the move.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import (
    RedirectResponse,
)
from sqlalchemy.orm import Session

from app.audit_service import log_transaction
from app.deck_service import (
    find_inventory_matches_for_deck_import,
    list_decks_basic,
    pull_card_to_deck,
    share_card_to_deck,
)
from app.dependencies import (
    DRAWER_SORTER_USERNAMES,
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
)
from app.import_service import (
    _distinct_locations_from_rows,
    auto_create_locations,
    compute_duplicate_counts_for_resolved,
    normalize_finish,
    normalize_language,
    parse_scanner_csv,
    parse_text_list,
    persist_import_rows,
    resolve_location_names,
)
from app.inventory_service import (
    find_inventory_matches_for_collection_import,
    place_imported_rows,
    resort_collection,
    route_intake_to_bulk,
    summarize_intake_routing,
)
from app.location_service import (
    get_location,
    list_locations,
)
from app.models import Card, Deck, InventoryRow, User
from app.scryfall import (
    fetch_card_by_scryfall_id,
    fetch_card_by_set_and_number,
    search_cards_by_name,
)
from app.timeutil import utc_now

router = APIRouter()

# Import size caps (S4) — reject oversized paste/CSV uploads BEFORE any parsing
# so a malicious or accidental large blob can't consume excessive memory or
# processing time. Applies to the two raw-input entry points only (the CSV
# upload preview and the paste-text preview); the downstream commit /
# reconcile routes receive already-parsed parallel-array form fields, never a
# raw blob, so the cap belongs at the preview seam.
MAX_IMPORT_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_IMPORT_LINES = 5_000


def _count_lines(text: bytes | str) -> int:
    """Line count over every separator kind (\\n, \\r, \\r\\n), trailing-aware.

    ``str.splitlines()`` / ``bytes.splitlines()`` split on every line-boundary
    kind — so a \\r-only (classic-Mac) or \\r\\n (Windows) file counts correctly
    instead of reading as one giant line — and do NOT emit a phantom final
    element for a trailing separator (a valid 5,000-line file ending in a newline
    counts as 5,000, not 5,001, so it isn't wrongly rejected). Works on bytes or
    str so both the CSV-byte and paste-text paths share one definition.
    """
    return len(text.splitlines())


def _enforce_import_size_limits(num_bytes: int, num_lines: int) -> None:
    """Raise ValueError (→ global handler → clean 400) if an import exceeds the
    byte or line cap. Called before parsing on both the paste-text and CSV
    upload paths; the message is reader-facing so the user knows to split the
    import into smaller pieces.
    """
    if num_bytes > MAX_IMPORT_BYTES:
        raise ValueError(
            f"Import too large: {num_bytes:,} bytes exceeds the "
            f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB limit. "
            "Split it into smaller imports and try again."
        )
    if num_lines > MAX_IMPORT_LINES:
        raise ValueError(
            f"Import too large: {num_lines:,} lines exceeds the "
            f"{MAX_IMPORT_LINES:,}-line limit. "
            "Split it into smaller imports and try again."
        )


@router.get("/import")
def import_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    return render(
        request,
        "import.html",
        {
            "title": "Import",
            "current_user": current_user,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
            "locations": list_locations(session, current_user.id),
        },
    )


@router.post("/import/preview")
async def import_preview(
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    # [import-preview] diagnostic instrumentation (no logic changes) — splits
    # parser time from render/serialization time to localize the 524 timeout.
    _t0 = time.perf_counter()
    # S4 — reject by the DECLARED size BEFORE reading the body into RAM, so a
    # gigabyte upload can't OOM the pod via file.read(). Starlette populates
    # file.size from the multipart parse (spooled to disk above its threshold),
    # so this is the pre-read guard; len(file_bytes) below is the belt-and-braces
    # re-check for the rare case where size is unset.
    if file.size is not None:
        _enforce_import_size_limits(file.size, 0)
    file_bytes = await file.read()
    _t_read = time.perf_counter()
    # Now safely capped at MAX_IMPORT_BYTES — re-check the actual bytes and the
    # line count before any parsing begins.
    _enforce_import_size_limits(len(file_bytes), _count_lines(file_bytes))
    result = parse_scanner_csv(file_bytes)
    _t_parsed = time.perf_counter()

    # v3.30.15 — resolve per-row Location values against the user's
    # StorageLocations. Surfaces ambiguities (2+ matches), missing names
    # (auto-create confirm), and a duplicate warning for clean matches.
    distinct_loc_names = _distinct_locations_from_rows(result["valid_rows"])
    location_resolutions = resolve_location_names(session, current_user.id, distinct_loc_names)
    duplicate_counts = compute_duplicate_counts_for_resolved(
        session, current_user.id, result["valid_rows"], location_resolutions
    )

    context = {
        "title": "Import Preview",
        "valid_rows": result["valid_rows"],
        "invalid_rows": result["invalid_rows"],
        "format_name": result["format_name"],
        "filename": file.filename,
        "current_user": current_user,
        "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
        "locations": list_locations(session, current_user.id),
        "decks": list_decks_basic(session, user_id=current_user.id),
        "location_resolutions": location_resolutions,
        "duplicate_counts": duplicate_counts,
        "auto_create_error": None,
    }
    _t_ctx = time.perf_counter()
    response = render(request, "import_preview.html", context)
    _t_rendered = time.perf_counter()
    print(
        f"[import-preview] route: file.read={_t_read - _t0:.2f}s "
        f"parse_scanner_csv={_t_parsed - _t_read:.2f}s "
        f"context_build(locations+decks)={_t_ctx - _t_parsed:.2f}s "
        f"render={_t_rendered - _t_ctx:.2f}s "
        f"valid={len(result['valid_rows'])} invalid={len(result['invalid_rows'])}",
        flush=True,
    )
    return response


@router.post("/import/list/preview")
async def import_list_preview(
    request: Request,
    card_list: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    # S4 — cap size before any parsing begins. Measure the ACTUAL UTF-8 byte
    # size, NOT len(card_list): a character can be up to 4 bytes in UTF-8, so the
    # character count under-rejects (a 2M-char paste of multi-byte chars is up to
    # 8 MB yet len() reads it as < the 2 MB byte cap). Encode once and reuse the
    # bytes for both the byte cap and the line count so the parser, not this
    # check, is the only place the payload is materialized twice.
    card_bytes = card_list.encode("utf-8")
    _enforce_import_size_limits(len(card_bytes), _count_lines(card_bytes))
    result = parse_text_list(card_list)
    # v3.30.15 — paste-list flow won't typically carry Location values, but
    # the template branches on the resolution context keys, so they must be
    # present. The helpers degrade to empty gracefully.
    distinct_loc_names = _distinct_locations_from_rows(result["valid_rows"])
    location_resolutions = resolve_location_names(session, current_user.id, distinct_loc_names)
    duplicate_counts = compute_duplicate_counts_for_resolved(
        session, current_user.id, result["valid_rows"], location_resolutions
    )
    return render(
        request,
        "import_preview.html",
        {
            "title": "Import Preview",
            "valid_rows": result["valid_rows"],
            "invalid_rows": result["invalid_rows"],
            "format_name": result["format_name"],
            "filename": "pasted list",
            "current_user": current_user,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
            "locations": list_locations(session, current_user.id),
            "decks": list_decks_basic(session, user_id=current_user.id),
            "location_resolutions": location_resolutions,
            "duplicate_counts": duplicate_counts,
            "auto_create_error": None,
        },
    )


def _parsed_rows_from_form(
    line_number: list[str],
    name: list[str],
    scryfall_id: list[str],
    set_code: list[str],
    collector_number: list[str],
    finish: list[str],
    quantity: list[str],
    location: list[str],
    language: list[str] | None = None,
    location_type: list[str] | None = None,
    role: list[str] | None = None,
    tags: list[str] | None = None,
    is_proxy: list[str] | None = None,
) -> list[dict]:
    """Rebuild the parsed-row dicts from the parallel-array form fields.

    Shared by /import/commit, /import/reconcile-preview, and any future
    handler that receives the same field shape from import_preview.html.

    v3.30.16 — extended with four new optional parallel arrays
    (location_type / role / tags / is_proxy). Each falls back to the
    safe default when the array is absent or short (matches old 6-column
    CSVs that never carried these fields). The new fields are written
    into the parsed-row dict alongside the existing ones so
    persist_import_rows / the resolution helpers consume them
    transparently.
    """
    rows = []
    languages = language or []
    location_types = location_type or []
    roles = role or []
    tags_list = tags or []
    is_proxy_list = is_proxy or []
    for i in range(len(line_number)):
        # v3.30.16 — is_proxy form field carries the string "true"/"false";
        # parse_proxy_bool returns (bool, valid). Form values come from
        # hidden inputs we wrote ourselves so they're always one of the
        # two strings — invalid values would have already been routed to
        # invalid_rows at parse_scanner_csv time and never reach a form.
        raw_proxy = is_proxy_list[i] if i < len(is_proxy_list) else ""
        proxy_value, _ = normalize_proxy_value_for_commit(raw_proxy)
        rows.append(
            {
                "line_number": int(line_number[i]),
                "name": name[i] if i < len(name) else "",
                "scryfall_id": scryfall_id[i],
                "set_code": set_code[i],
                "collector_number": collector_number[i],
                "finish": normalize_finish(finish[i]),
                "quantity": int(quantity[i]),
                "location": location[i],
                "location_type": (location_types[i] if i < len(location_types) else "")
                .strip()
                .lower(),
                "language": normalize_language(languages[i]) if i < len(languages) else "en",
                "role": (roles[i] if i < len(roles) else "").strip(),
                "tags": tags_list[i] if i < len(tags_list) else "",
                "is_proxy": proxy_value,
            }
        )
    return rows


def normalize_proxy_value_for_commit(raw: str) -> tuple[bool, bool]:
    """Mirror of import_service.parse_proxy_bool used at commit-form-rebuild
    time. Form values are always one of the two recognized strings (we
    write them ourselves into the hidden inputs); any other value falls
    back to False with valid=True (the form has no untrusted path here).
    """
    cleaned = (raw or "").strip().lower()
    if cleaned == "true":
        return (True, True)
    return (False, True)


def _build_line_to_location_map(
    session: Session,
    user_id: int,
    parsed_rows: list[dict],
    choice_names: list[str],
    choice_ids: list[str],
    auto_create_confirm: str,
    choice_types: list[str] | None = None,
) -> tuple[dict[int, int], list[str], list[str]]:
    """v3.30.15 — build the per-row line_number → StorageLocation.id map.

    Consumes the parallel ``location_choice_name[]`` / ``location_choice_id[]``
    arrays the preview step emits and the ``auto_create_confirm`` checkbox
    value, plus the user's existing StorageLocations (so a clean single-match
    name resolves without requiring a UI choice).

    Each ``choice_id`` value carries one of:
      * ``"<positive int>"`` — use this StorageLocation.id directly
        (picker-resolved or single-match).
      * ``"0"`` — auto-create the corresponding name (gated by
        ``auto_create_confirm == "yes"``).
      * ``"-1"`` or empty — skip per-row resolution for this name; rows
        with this Location fall through to ``target_location_id``.

    v3.30.16 — new optional ``choice_types`` parallel array carries the
    per-name type for auto-create (``""`` / ``"box"`` / ``"binder"`` /
    ``"drawer"`` / ``"deck"`` / ``"other"``). For names with ``cid_int == 0``
    the type is threaded through to ``auto_create_locations`` so the
    auto-created location gets the right type instead of defaulting to
    ``"other"``. For non-zero choice_id values (picker/clean/skip) the
    type is ignored — the existing location's type wins per Decision 12.

    Returns ``(line_to_location_id, needs_confirm_names, skipped_deck_conflicts)``:
      * ``line_to_location_id`` — map of CSV line_number → resolved id.
        Empty dict if the caller should take the existing 3-branch dispatch.
      * ``needs_confirm_names`` — non-empty list of names whose auto-create
        was requested but ``auto_create_confirm != "yes"``. The route MUST
        re-render the preview with no writes in this case (Decision 3
        "User cancels → batch is rejected without writes").
      * ``skipped_deck_conflicts`` (v3.30.20) — display names of deck
        auto-create rows that the user opted into but that auto_create_locations
        silently dropped (the v3.30.18 try/except IntegrityError fallback
        for the pre-v3.1.0 legacy decks.name UNIQUE auto-index — fires when
        another user owns the name). The route handler threads this list
        to import_result.html so the result page can warn the user that
        N decks were skipped due to legacy name conflicts. Belt-and-
        suspenders alongside the v3.30.20 pre-warn in the preview UI: if
        a tampered submission slips a conflicting deck-create past the
        pre-warn, the try/except still catches it AND the result page
        notes it.
    """
    name_to_id: dict[str, int] = {}
    auto_create_names: list[str] = []
    # v3.30.16 — per-name type override map; only populated for names
    # entering the auto-create flow. Keyed by lowercased name to match
    # auto_create_locations' lookup convention.
    auto_create_type_overrides: dict[str, str] = {}
    # v3.30.20 — display-form lookup for skipped-deck-conflict reporting.
    # Keyed by lowercased name; value is the original CSV display form.
    display_by_normalized: dict[str, str] = {}

    choice_types = choice_types or []
    for i, (raw_name, raw_id) in enumerate(zip(choice_names, choice_ids, strict=False)):
        normalized = (raw_name or "").strip().lower()
        if not normalized:
            continue
        try:
            cid_int = int(raw_id)
        except (ValueError, TypeError):
            continue
        if cid_int > 0:
            name_to_id[normalized] = cid_int
        elif cid_int == 0:
            auto_create_names.append(raw_name.strip())
            display_by_normalized[normalized] = raw_name.strip()
            raw_type = (choice_types[i] if i < len(choice_types) else "").strip().lower()
            if raw_type:
                auto_create_type_overrides[normalized] = raw_type

    # v3.30.17 — deck-type auto-create rows have their own per-row opt-in
    # checkbox in the preview UI (Part B); the global auto_create_confirm
    # only gates NON-deck rows. Per-deck checkboxes work by toggling the
    # row's location_choice_id between "0" (create) and "-1" (skip),
    # so by the time we reach this code an unchecked deck row already has
    # cid_int=-1 and never enters auto_create_names. The split below
    # preserves the same shape for the global-confirm gate: only non-deck
    # names trigger the "must confirm" re-render. Deck rows that DID make
    # it into auto_create_names (checkbox checked) proceed straight to
    # auto_create_locations regardless of the global confirm state.
    auto_create_non_deck_names = [
        n for n in auto_create_names if auto_create_type_overrides.get(n.lower(), "") != "deck"
    ]
    if auto_create_non_deck_names and auto_create_confirm != "yes":
        return ({}, auto_create_non_deck_names, [])

    # v3.30.20 — record the set of names the user intended to create as
    # decks BEFORE calling auto_create_locations. After the call, anything
    # missing from the resolution map is a silently-skipped row — either
    # the v3.30.18 IntegrityError fallback fired (legacy cross-user
    # constraint) or the orphaned-Deck-no-paired-SL edge case. Both are
    # surfaced on the result page so the user knows the cards landed in
    # Pending rather than into the deck they ticked.
    intended_deck_creates = {
        n.lower(): display_by_normalized.get(n.lower(), n)
        for n in auto_create_names
        if auto_create_type_overrides.get(n.lower(), "") == "deck"
    }

    if auto_create_names:
        # Validation barrier already passed; create the missing locations.
        # auto_create_locations validates each requested type against
        # VALID_LOCATION_TYPES (minus root) and raises ValueError on miss.
        # The route handler catches ValueError and surfaces it via the
        # v3.30.15 auto-create-not-confirmed re-render pattern.
        # v3.30.17 — auto_create_locations routes type="deck" through
        # deck_service.create_deck so the paired Deck row lands atomically.
        created = auto_create_locations(
            session, user_id, auto_create_names, name_to_type=auto_create_type_overrides
        )
        name_to_id.update(created)

    # v3.30.20 — diff intended-deck-creates against actually-created.
    # Names that were requested but never landed in name_to_id are the
    # skipped-due-to-conflict rows (the v3.30.18 try/except fallback
    # caught them).
    skipped_deck_conflicts = [
        display_by_normalized.get(lname, lname)
        for lname in intended_deck_creates
        if lname not in name_to_id
    ]

    line_to_loc: dict[int, int] = {}
    for r in parsed_rows:
        raw = (r.get("location") or "").strip().lower()
        if not raw:
            continue
        if raw in name_to_id:
            try:
                line_num = int(r.get("line_number"))
            except (ValueError, TypeError):
                continue
            line_to_loc[line_num] = name_to_id[raw]

    return (line_to_loc, [], skipped_deck_conflicts)


def _deck_for_storage_location(
    session: Session, user_id: int, storage_location_id: int
) -> Deck | None:
    """If the given storage_location is a deck-type location owned by the user,
    return the Deck record that owns it. Otherwise None.
    """
    if storage_location_id <= 0:
        return None
    loc = get_location(session, location_id=storage_location_id, user_id=user_id)
    if loc is None or loc.type != "deck":
        return None
    return session.query(Deck).filter(Deck.storage_location_id == loc.id).first()


def _intake_routing_preview(
    session: Session, current_user: User, target_location_id: int, matches_rows: list[dict]
) -> dict | None:
    """v3.38.0 — the auto-sort intake-routing verdict for the reconcile-preview:
    ``{"drawers": N, "bulk": M}`` of imported copies, or ``None`` when it doesn't
    apply (an explicit destination was chosen so nothing auto-sorts, the user
    isn't a drawer-sorter, or nothing would route). Keeps the "N → drawers,
    M → bulk" line off previews where intake routing won't run."""
    if target_location_id != 0 or current_user.username not in DRAWER_SORTER_USERNAMES:
        return None
    drawers_n, bulk_n = summarize_intake_routing(session, current_user.id, matches_rows)
    if drawers_n == 0 and bulk_n == 0:
        return None
    return {"drawers": drawers_n, "bulk": bulk_n}


def _annotate_collection_dupes(rows: list[dict]) -> None:
    """Tag each collection-mode reconciliation row with display flags so the
    partial can render a focused "show only duplicates" view.

    Adds two booleans per row (mutating in place):

      - ``has_owned_match``  — total_user_owned > 0; the row is a duplicate
                               of something the user already owns somewhere.
      - ``is_deck_only_dupe`` — owned_breakdown has entries and ALL of them
                                are deck-type. This is the case where the
                                user's "duplicate" is allocated to a deck
                                rather than a free-collection location, and
                                a re-import shouldn't silently skip — the
                                deck copy is in use, the user probably wants
                                a new copy. The collection-mode template
                                auto-expands the per-row review when any
                                row has this flag set.
    """
    for r in rows:
        breakdown = r.get("owned_breakdown") or []
        r["has_owned_match"] = bool(breakdown) and (r.get("total_user_owned", 0) > 0)
        r["is_deck_only_dupe"] = r["has_owned_match"] and all(
            (b.get("location_type") == "deck") for b in breakdown
        )


@router.post("/import/reconcile-preview")
async def import_reconcile_preview(
    request: Request,
    target_location_id: int = Form(0),
    line_number: list[str] = Form([]),
    name: list[str] = Form([]),
    scryfall_id: list[str] = Form([]),
    set_code: list[str] = Form([]),
    collector_number: list[str] = Form([]),
    finish: list[str] = Form([]),
    quantity: list[str] = Form([]),
    location: list[str] = Form([]),
    language: list[str] = Form([]),
    # v3.30.16 — receive the five new parallel arrays so HTMX-included
    # reconciliation requests preserve them through the round trip. Not
    # used by the reconciliation logic itself (matching the v3.30.15
    # "reconciliation paths bypassed in v3.30.15 path" contract), but
    # consumed by _parsed_rows_from_form to keep the form-state shape
    # consistent.
    location_type: list[str] = Form([]),
    role: list[str] = Form([]),
    tags: list[str] = Form([]),
    is_proxy: list[str] = Form([]),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """HTMX endpoint: fires when the destination dropdown changes on the
    import preview. Returns the inner HTML of #reconciliation-panel.

    Dispatches on destination type:
      - Deck destination → deck-reconciliation path (Session 2 / v3.16.13-14).
        Renders the partial in `reconcile_mode="deck"` with movable matches,
        target-deck and other-deck breakdowns, and move/move-plus-new/import
        action options.
      - Non-deck destination (drawer/binder/box/other) OR auto-sort
        (target_location_id == 0) → collection-reconciliation path
        (Session A / v3.16.15+). Renders the partial in
        `reconcile_mode="collection"` with skip/delta/new actions based on
        total cross-location ownership.

    The wrapper div #reconciliation-panel lives in import_preview.html and
    is untouched by hx-swap=innerHTML — only its inner content changes.
    """
    parsed_rows = _parsed_rows_from_form(
        line_number,
        name,
        scryfall_id,
        set_code,
        collector_number,
        finish,
        quantity,
        location,
        language,
        location_type,
        role,
        tags,
        is_proxy,
    )

    # Decorate each parsed row's resolved card with a display_name for
    # the partial template. Same logic used by both reconciliation paths.
    def _decorate_display_names(rows: list[dict]) -> None:
        name_by_index = {r.get("line_number"): r.get("name") for r in parsed_rows}
        card_ids = [r["card_id"] for r in rows if r.get("card_id")]
        card_name_by_id: dict[int, str] = {}
        if card_ids:
            for c in session.query(Card.id, Card.name).filter(Card.id.in_(card_ids)).all():
                card_name_by_id[c.id] = c.name
        for row in rows:
            from_form = name_by_index.get(row.get("line_number")) or ""
            row["display_name"] = (
                from_form or card_name_by_id.get(row.get("card_id")) or row.get("scryfall_id", "")
            )

    deck = _deck_for_storage_location(session, current_user.id, target_location_id)

    if deck is not None:
        # Deck destination — existing path.
        matches_rows = find_inventory_matches_for_deck_import(
            session, current_user.id, deck.id, parsed_rows
        )
        _decorate_display_names(matches_rows)

        total_to_move = sum(r["recommended_move_qty"] for r in matches_rows)
        # Anticipated auto-merge: when target deck already has a row for
        # this (card, finish), the import_new path folds ALL of
        # recommended_new_qty into that existing row instead of creating
        # a duplicate.
        total_to_merge = sum(
            r["recommended_new_qty"] for r in matches_rows if r["total_in_target_deck"] > 0
        )
        total_to_import_new = sum(r["recommended_new_qty"] for r in matches_rows) - total_to_merge
        # v3.33.0 — copies covered by a sibling variant deck (no move, no import).
        total_covered_by_variant = sum(r.get("variant_covered_qty", 0) for r in matches_rows)

        return render(
            request,
            "_import_reconciliation.html",
            {
                "reconcile_mode": "deck",
                "rows": matches_rows,
                "deck_name": deck.name,
                "total_to_move": total_to_move,
                "total_to_import_new": total_to_import_new,
                "total_to_merge": total_to_merge,
                "total_covered_by_variant": total_covered_by_variant,
                "is_variant_group": deck.variant_group_id is not None,
                "is_brew": deck.is_brew,
            },
        )

    # Non-deck destination (auto-sort or any non-deck location).
    matches_rows = find_inventory_matches_for_collection_import(
        session, current_user.id, parsed_rows
    )
    _decorate_display_names(matches_rows)
    _annotate_collection_dupes(matches_rows)

    total_to_skip = 0
    total_to_delta = 0
    total_to_new = 0
    for r in matches_rows:
        action = r["recommended_action"]
        if action == "skip_already_owned":
            total_to_skip += r["quantity_needed"]
        elif action == "import_delta":
            total_to_delta += r["recommended_new_qty"]
            total_to_skip += r["quantity_needed"] - r["recommended_new_qty"]
        else:  # import_new
            total_to_new += r["recommended_new_qty"]

    has_deck_only_dupes = any(r.get("is_deck_only_dupe") for r in matches_rows)

    # Destination name for the summary (when not auto-sort).
    destination_name: str | None = None
    if target_location_id > 0:
        loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
        if loc is not None:
            destination_name = loc.name

    return render(
        request,
        "_import_reconciliation.html",
        {
            "reconcile_mode": "collection",
            "rows": matches_rows,
            "deck_name": destination_name,  # template treats this as a generic destination label
            "total_to_skip": total_to_skip,
            "total_to_delta": total_to_delta,
            "total_to_new": total_to_new,
            "manual_mode": False,
            "has_deck_only_dupes": has_deck_only_dupes,
            "bulk_routing": _intake_routing_preview(
                session, current_user, target_location_id, matches_rows
            ),
        },
    )


def _commit_deck_import_with_reconciliation(
    session: Session,
    user_id: int,
    deck: Deck,
    parsed_rows: list[dict],
    actions: list[str],
    move_qtys: list[int],
    new_qtys: list[int],
    filename: str,
) -> dict:
    """Per-row dispatch for deck imports under Refined Model A.

    For each parsed row, the user's reconciliation choice (or its default)
    selects one of three paths:

    - move_existing:        all copies come from existing inventory
                            (pull_card_to_deck loop, multi-source in order)
    - move_existing_plus_new: some copies move, some are imported new
    - import_new:           the existing persist_import_rows path

    After persist_import_rows creates new pending rows for the import_new
    portion, this handler does an auto-merge pass: for each new row whose
    (card_id, finish) already has a placed row in the target deck, the
    new row's quantity is added to the existing deck row and the new row
    is deleted (rather than placed alongside as a duplicate). The merged
    count is tracked separately from imported_count and reported back to
    the result page. Singleton-format decks (Commander etc.) never end
    up with two rows for the same printing as a result.

    Returns a dict mirroring persist_import_rows' shape plus extra counts
    used by import_result.html:

      imported_count       — unique (card, finish) rows that ended up as
                             NEW deck rows (after the auto-merge pass)
      total_quantity       — total copies of those new deck rows
      moved_count          — total copies moved from non-deck inventory
                             via pull_card_to_deck
      merged_count         — total copies merged into existing deck rows
                             instead of creating duplicates
      failed_rows          — list of rows that couldn't be resolved
      stale_match_rows     — rows where the preview said "move N" but
                             inventory had less than N at commit time;
                             the shortfall fell back to import_new
      batch_id             — most recent ImportBatch created for the new
                             import portion (None if no new copies were
                             imported)
      imported_row_ids     — IDs of rows that actually became new deck
                             rows (merged-then-deleted rows are excluded)
    """
    moved_count = 0
    shared_count = 0  # issue #27 — copies materialized as variant-group shares
    stale_match_rows: list[dict] = []
    new_import_rows: list[dict] = []
    new_import_indices: list[int] = []  # position in parsed_rows for the new portion

    for idx, row in enumerate(parsed_rows):
        action = actions[idx] if idx < len(actions) else "import_new"
        move_qty = int(move_qtys[idx]) if idx < len(move_qtys) else 0
        new_qty = int(new_qtys[idx]) if idx < len(new_qtys) else int(row["quantity"])

        if action == "import_new":
            # v3.37.x Brew Mode: a card owned NOWHERE (import_new) added to a
            # brew becomes a PROXY row so it shows in the deck but never counts
            # as owned (the buy-list reads this flag). persist_import_rows reads
            # the row's "is_proxy" key. This is the single source for the brew
            # proxy rule — both the paste/CSV deck import AND the single-card
            # add-card route funnel through here, so add-card no longer sets it.
            if deck.is_brew:
                row["is_proxy"] = "true"
            new_import_rows.append(row)
            new_import_indices.append(idx)
            continue

        if action == "covered_by_variant":
            # issue #27 — the card is covered by a sibling build in this deck's
            # variant group. Instead of a silent no-op, MATERIALIZE the coverage
            # as a share so the sibling's physical copy becomes a visible member
            # of THIS deck's list (idempotent — re-import never duplicates; the
            # physical row never moves). Skip rows already shared in.
            recheck = find_inventory_matches_for_deck_import(session, user_id, deck.id, [row])[0]
            for match in recheck.get("other_deck_matches", []):
                if not match.get("is_variant_sibling") or match.get("is_shared_in"):
                    continue
                try:
                    share_card_to_deck(
                        session,
                        user_id,
                        inventory_row_id=match["inventory_row_id"],
                        target_deck_id=deck.id,
                    )
                    shared_count += 1
                except ValueError:
                    pass
            continue

        # Re-resolve matches at commit time (preview state may be stale).
        recheck = find_inventory_matches_for_deck_import(session, user_id, deck.id, [row])[0]
        available = recheck["total_available"]

        # How many can we actually move now, capped by the user's
        # requested move_qty AND the current inventory.
        actual_move_qty = min(move_qty, available)
        shortfall = move_qty - actual_move_qty

        # Walk matches in order, draining each until actual_move_qty is hit.
        remaining_to_move = actual_move_qty
        for match in recheck["matches"]:
            if remaining_to_move <= 0:
                break
            pull_qty = min(remaining_to_move, match["quantity_available"])
            pulled_ok = pull_card_to_deck(
                session=session,
                user_id=user_id,
                deck_id=deck.id,
                inventory_row_id=match["inventory_row_id"],
                quantity=pull_qty,
            )
            if pulled_ok:
                moved_count += pull_qty
                remaining_to_move -= pull_qty

        # If the move shortfall was non-zero, we promised the user N moves
        # but only delivered some. Cover the rest as new imports and flag
        # this row in the result so import_result.html can warn.
        compensating_new = shortfall + new_qty
        if shortfall > 0:
            stale_match_rows.append(
                {
                    "line_number": row.get("line_number"),
                    "name": row.get("name") or row.get("scryfall_id"),
                    "expected_move": move_qty,
                    "actual_move": actual_move_qty,
                }
            )
        if compensating_new > 0:
            # Schedule a new import for the remaining quantity.
            row_for_new = dict(row)
            row_for_new["quantity"] = compensating_new
            # Brew Mode: the new portion of a partial move isn't backed by a
            # pulled real copy, so it's a proxy too (same rule as import_new).
            if deck.is_brew:
                row_for_new["is_proxy"] = "true"
            new_import_rows.append(row_for_new)
            new_import_indices.append(idx)

    # Run the existing import path for everything that didn't get moved.
    new_imported_count = 0
    new_total_quantity = 0
    merged_count = 0
    failed_rows: list[dict] = []
    batch_id = None
    imported_row_ids: list[int] = []
    if new_import_rows:
        result = persist_import_rows(session, new_import_rows, filename=filename, user_id=user_id)
        new_imported_count = result["imported_count"]
        new_total_quantity = result.get("total_quantity", new_imported_count)
        failed_rows = result["failed_rows"]
        batch_id = result["batch_id"]
        imported_row_ids = result.get("imported_row_ids", [])

        # Auto-merge pass: for each new pending row whose (card_id, finish)
        # already has a placed row in the target deck, increment the
        # existing row's quantity and delete the new one. Otherwise queue
        # it for normal placement. Singleton-correct behavior — never two
        # rows for the same printing in the same deck.
        if imported_row_ids and deck.storage_location_id:
            new_pending_rows = (
                session.query(InventoryRow).filter(InventoryRow.id.in_(imported_row_ids)).all()
            )
            existing_deck_rows = (
                session.query(InventoryRow)
                .filter(
                    InventoryRow.user_id == user_id,
                    InventoryRow.storage_location_id == deck.storage_location_id,
                    InventoryRow.is_pending.is_(False),
                )
                .all()
            )
            existing_by_key: dict[tuple[int, str], InventoryRow] = {
                (r.card_id, r.finish): r for r in existing_deck_rows
            }
            rows_to_place: list[int] = []
            merged_row_ids: set[int] = set()
            now_ts = utc_now()
            for new_row in new_pending_rows:
                key = (new_row.card_id, new_row.finish)
                existing = existing_by_key.get(key)
                if existing is None:
                    rows_to_place.append(new_row.id)
                    continue
                existing.quantity += new_row.quantity
                existing.updated_at = now_ts
                merged_count += new_row.quantity
                merged_row_ids.add(new_row.id)
                log_transaction(
                    session=session,
                    user_id=user_id,
                    event_type="import_merge",
                    card_id=new_row.card_id,
                    finish=new_row.finish,
                    quantity_delta=new_row.quantity,
                    source_location="import",
                    destination_location=f"deck:{deck.name}",
                    batch_id=batch_id,
                    inventory_row_id=existing.id,
                    note=(
                        f"Merged {new_row.quantity} new copies into existing "
                        f"deck row in {deck.name}"
                    ),
                    flush=False,
                )
                session.delete(new_row)

            if rows_to_place:
                place_imported_rows(
                    session,
                    rows_to_place,
                    user_id=user_id,
                    location_id=deck.storage_location_id,
                )

            # Update reported counts: imported_count/total_quantity reflect
            # rows that ended up as NEW deck rows. Rows that merged into an
            # existing deck row instead are tracked in merged_count.
            if merged_row_ids:
                merged_rows_total_qty = sum(
                    r.quantity for r in new_pending_rows if r.id in merged_row_ids
                )
                new_imported_count = max(0, new_imported_count - len(merged_row_ids))
                new_total_quantity = max(0, new_total_quantity - merged_rows_total_qty)
                imported_row_ids = [rid for rid in imported_row_ids if rid not in merged_row_ids]

            session.commit()

    return {
        "imported_count": new_imported_count,
        "total_quantity": new_total_quantity,
        "moved_count": moved_count,
        "shared_count": shared_count,
        "merged_count": merged_count,
        "failed_rows": failed_rows,
        "stale_match_rows": stale_match_rows,
        "batch_id": batch_id,
        "imported_row_ids": imported_row_ids,
    }


def _commit_collection_import_with_reconciliation(
    session: Session,
    user_id: int,
    target_location_id: int,
    parsed_rows: list[dict],
    actions: list[str],
    new_qtys: list[int],
    filename: str,
) -> dict:
    """Per-row dispatch for non-deck imports under the sync model
    (Refined Model A, design doc collection_import_sync.md §4).

    Sibling of ``_commit_deck_import_with_reconciliation`` for non-deck
    destinations. The user's choices map to three actions:

    - ``skip_already_owned``  → no ``InventoryRow`` created. Increment
                                ``skipped_count`` and emit an
                                ``import_skipped`` ``TransactionLog`` event
                                for audit.
    - ``import_delta``        → create + place ``new_qty`` copies (the user
                                owns some but fewer than they're importing).
    - ``import_new``          → create + place full ``quantity_needed`` copies
                                (override: the user explicitly wants new
                                copies even though they may already own
                                some).

    Re-resolves matches at commit time via
    ``find_inventory_matches_for_collection_import``. If a row's recommended
    action was ``skip_already_owned`` at preview but ``total_user_owned``
    has decreased below ``quantity_needed`` by commit (a concurrent change
    — user sold a card between preview and commit), fall back to
    ``import_delta`` with ``new_qty = quantity_needed - actual_owned`` and
    record the adjustment in ``stale_match_rows``. Stale-match fallback
    only triggers in the dangerous direction (less owned than expected);
    if the user gained inventory between preview and commit, their
    explicit ``import_delta`` / ``import_new`` choice is honored as-is.

    Args:
        session, user_id:       per-user-scoped session
        target_location_id:     0 = auto-sort (no ``place_imported_rows``
                                call; caller handles drawer-sorter resort);
                                >0 = specific non-deck location to place new
                                rows into. Per design doc §8.1, new rows are
                                placed alongside any existing rows for the
                                same (card, finish) at the destination —
                                merge-into-existing is a v3.16.X polish
                                target.
        parsed_rows:            same shape as
                                ``persist_import_rows`` input
        actions, new_qtys:      parallel arrays from the reconciliation
                                form; ``actions[i]`` is the user's choice
                                for ``parsed_rows[i]``, ``new_qtys[i]`` is
                                the qty to import as new (0 for skip).
        filename:               passed through to ``persist_import_rows``.

    Returns:
        Dict shaped for ``import_result.html``::

            {
                "imported_count":     int,  # unique new InventoryRows created
                "total_quantity":     int,  # total copies imported as new
                "skipped_count":      int,  # total copies skipped (skip + delta-portion)
                "failed_rows":        list[dict],
                "stale_match_rows":   list[dict],
                "batch_id":           int | None,
                "imported_row_ids":   list[int],
            }
    """
    skipped_count = 0
    stale_match_rows: list[dict] = []
    new_import_rows: list[dict] = []

    # Re-resolve at commit time so we can detect inventory drift since
    # preview. Single batched query — same shape as the read function.
    recheck = find_inventory_matches_for_collection_import(session, user_id, parsed_rows)
    recheck_by_line = {r["line_number"]: r for r in recheck}

    for idx, row in enumerate(parsed_rows):
        action = actions[idx] if idx < len(actions) else "import_new"
        form_new_qty = int(new_qtys[idx]) if idx < len(new_qtys) else int(row.get("quantity") or 1)
        quantity_needed = max(1, int(row.get("quantity") or 1))
        line_number = row.get("line_number")
        rc = recheck_by_line.get(line_number, {})
        actual_owned = rc.get("total_user_owned", 0)
        rc_card_id = rc.get("card_id")

        if action == "skip_already_owned":
            # Stale-match check: did the user's actual ownership drop below
            # the expected count between preview and commit?
            if actual_owned < quantity_needed:
                fallback_new_qty = quantity_needed - actual_owned
                stale_match_rows.append(
                    {
                        "line_number": line_number,
                        "name": row.get("name") or row.get("scryfall_id"),
                        "expected_skip": quantity_needed,
                        "actual_new_qty": fallback_new_qty,
                        "reason": "inventory_decreased",
                    }
                )
                row_for_new = dict(row)
                row_for_new["quantity"] = fallback_new_qty
                new_import_rows.append(row_for_new)
                skipped_count += actual_owned  # the rest is now imported
            else:
                # Still safe to skip.
                skipped_count += quantity_needed
                if rc_card_id is not None:
                    log_transaction(
                        session=session,
                        user_id=user_id,
                        event_type="import_skipped",
                        card_id=rc_card_id,
                        finish=(row.get("finish") or "normal").strip().lower(),
                        quantity_delta=0,
                        source_location="import",
                        destination_location="(skipped — already owned)",
                        batch_id=None,
                        inventory_row_id=None,
                        note=f"Skipped {quantity_needed} — already own {actual_owned}",
                        flush=False,
                    )
            continue

        # import_delta or import_new — trust the form's new_qty.
        if form_new_qty <= 0:
            # User overrode action to non-skip but set qty to 0. Treat as skip.
            skipped_count += quantity_needed
            continue

        row_for_new = dict(row)
        row_for_new["quantity"] = form_new_qty
        new_import_rows.append(row_for_new)
        # Any remaining quantity beyond new_qty is implicitly skipped (the
        # user owns enough already — for import_delta only; import_new with
        # the full qty contributes 0 to skipped_count).
        skipped_count += max(0, quantity_needed - form_new_qty)

    # Run the existing import path for everything that didn't get skipped.
    imported_count = 0
    total_quantity = 0
    failed_rows: list[dict] = []
    batch_id = None
    imported_row_ids: list[int] = []

    if new_import_rows:
        result = persist_import_rows(session, new_import_rows, filename=filename, user_id=user_id)
        imported_count = result["imported_count"]
        total_quantity = result.get("total_quantity", imported_count)
        failed_rows = result["failed_rows"]
        batch_id = result["batch_id"]
        imported_row_ids = result.get("imported_row_ids", [])

        # Place at destination if one was selected. target_location_id == 0
        # (auto-sort) skips placement — the route's drawer-sorter logic
        # handles those rows via resort_collection on the parent flow.
        if imported_row_ids and target_location_id > 0:
            place_imported_rows(
                session,
                imported_row_ids,
                user_id=user_id,
                location_id=target_location_id,
            )

    # Always commit so the import_skipped TransactionLog entries land even
    # when there are no new imports (pure-skip case).
    session.commit()

    return {
        "imported_count": imported_count,
        "total_quantity": total_quantity,
        "skipped_count": skipped_count,
        "failed_rows": failed_rows,
        "stale_match_rows": stale_match_rows,
        "batch_id": batch_id,
        "imported_row_ids": imported_row_ids,
    }


@router.post("/import/commit")
async def import_commit(
    request: Request,
    filename: str = Form("uploaded.csv"),
    line_number: list[str] = Form([]),
    name: list[str] = Form([]),
    scryfall_id: list[str] = Form([]),
    set_code: list[str] = Form([]),
    collector_number: list[str] = Form([]),
    finish: list[str] = Form([]),
    quantity: list[str] = Form([]),
    location: list[str] = Form([]),
    language: list[str] = Form([]),
    location_type: list[str] = Form([]),
    role: list[str] = Form([]),
    tags: list[str] = Form([]),
    is_proxy: list[str] = Form([]),
    target_location_id: int = Form(0),
    reconcile_action: list[str] = Form([]),
    reconcile_move_qty: list[str] = Form([]),
    reconcile_new_qty: list[str] = Form([]),
    location_choice_name: list[str] = Form([]),
    location_choice_id: list[str] = Form([]),
    location_choice_type: list[str] = Form([]),
    auto_create_confirm: str = Form("no"),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    rows = _parsed_rows_from_form(
        line_number,
        name,
        scryfall_id,
        set_code,
        collector_number,
        finish,
        quantity,
        location,
        language,
        location_type,
        role,
        tags,
        is_proxy,
    )

    placed_in = None
    placed_in_url = "/pending"
    placed_in_kind = None
    moved_count = 0
    stale_match_rows: list[dict] = []

    # v3.30.15 — per-row Location resolution.
    #
    # Build the line_number → resolved_StorageLocation.id map from the
    # parallel choice arrays surfaced by the preview step. If the user
    # asked to auto-create one or more missing locations but did not check
    # the confirm box, abort the batch with NO writes and re-render the
    # preview with an explicit error (per Decision 3).
    #
    # If the resulting map is non-empty, take the v3.30.15 resolution path:
    # rows with a resolved Location land directly placed; rows without
    # (blank Location, or user opted-out per-name) fall through to the
    # existing target_location_id / drawer-sorter behavior for the unresolved
    # rows only. The reconciliation paths are bypassed in this case — the
    # CSV is treated as carrying its own destination semantics.
    # v3.30.16 — _build_line_to_location_map may raise ValueError when an
    # auto-create choice carries an invalid type (e.g. tampered form field
    # with location_choice_type="dungeon"). Caught here and routed back
    # through the same preview re-render shape as the not-confirmed path,
    # surfacing the failure to the user without crashing the request.
    try:
        line_to_location_id, needs_confirm_names, skipped_deck_conflicts = (
            _build_line_to_location_map(
                session,
                current_user.id,
                rows,
                location_choice_name,
                location_choice_id,
                auto_create_confirm,
                choice_types=location_choice_type,
            )
        )
        invalid_type_error: str | None = None
    except ValueError as exc:
        line_to_location_id = {}
        needs_confirm_names = []
        skipped_deck_conflicts = []
        invalid_type_error = str(exc)

    if invalid_type_error or needs_confirm_names:
        # Auto-create requested but not confirmed → reject batch, re-render
        # preview with the same row state + an explicit error message.
        # v3.30.16 — the same re-render shape also handles the
        # invalid-Location-Type-on-auto-create failure surfaced as a
        # ValueError above.
        distinct_loc_names = _distinct_locations_from_rows(rows)
        location_resolutions = resolve_location_names(session, current_user.id, distinct_loc_names)
        duplicate_counts = compute_duplicate_counts_for_resolved(
            session, current_user.id, rows, location_resolutions
        )
        if invalid_type_error:
            error_message = invalid_type_error
        else:
            error_message = (
                "Confirm the auto-create of new locations before importing, "
                "or change those rows' choices."
            )
        return render(
            request,
            "import_preview.html",
            {
                "title": "Import Preview",
                "valid_rows": rows,
                "invalid_rows": [],
                "format_name": "(re-confirmation needed)",
                "filename": filename,
                "current_user": current_user,
                "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
                "locations": list_locations(session, current_user.id),
                "decks": list_decks_basic(session, user_id=current_user.id),
                "location_resolutions": location_resolutions,
                "duplicate_counts": duplicate_counts,
                "auto_create_error": error_message,
            },
        )

    if line_to_location_id:
        result = persist_import_rows(
            session,
            rows,
            user_id=current_user.id,
            filename=filename,
            line_to_location_id=line_to_location_id,
        )

        # Run place_imported_rows per resolved location so newly-placed rows
        # merge with any existing placed copy at the same destination
        # (Decision 5 — merge behavior unchanged; reused via the established
        # place_imported_rows code path rather than duplicated inside
        # persist_import_rows).
        placed_by_loc = result.get("placed_row_ids_by_location") or {}
        for loc_id, row_ids_at_loc in placed_by_loc.items():
            if row_ids_at_loc:
                place_imported_rows(
                    session,
                    row_ids_at_loc,
                    user_id=current_user.id,
                    location_id=loc_id,
                )

        # Unresolved rows (no per-row Location) fall through to the existing
        # target_location_id / drawer-sorter behavior. Reconciliation paths
        # are deliberately bypassed in the v3.30.15 path — a CSV carrying
        # Location values is treated as carrying its own destination
        # semantics. Reconciliation remains intact for blank-Location CSVs.
        pending_row_ids = result.get("pending_row_ids") or []
        merged_count = 0
        if pending_row_ids and target_location_id:
            place_imported_rows(
                session,
                pending_row_ids,
                user_id=current_user.id,
                location_id=target_location_id,
            )
            loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
            placed_in = loc.name if loc else None
            placed_in_url = f"/locations/{target_location_id}" if loc else "/pending"
            placed_in_kind = ("deck" if loc.type == "deck" else "location") if loc else None
        elif pending_row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
            # v3.38.0 intake routing: divert cheap non-staple surplus to Bulk
            # BEFORE the sorter runs, so the sorter only ever places keepers.
            route_intake_to_bulk(session, current_user.id, pending_row_ids)
            resort_collection(session, user_id=current_user.id)
            return RedirectResponse(url="/pending", status_code=303)
        elif placed_by_loc:
            # All rows resolved per-row; surface the first resolved location
            # as the placed_in for the result page, with a "multiple
            # locations" hint when more than one was used.
            first_loc_id = next(iter(placed_by_loc.keys()))
            loc = get_location(session, location_id=first_loc_id, user_id=current_user.id)
            if len(placed_by_loc) == 1 and loc:
                placed_in = loc.name
                placed_in_url = f"/locations/{first_loc_id}"
                placed_in_kind = "deck" if loc.type == "deck" else "location"
            else:
                placed_in = f"{len(placed_by_loc)} locations"
                placed_in_url = "/collection"
                placed_in_kind = "multiple"

        return render(
            request,
            "import_result.html",
            {
                "title": "Import Results",
                "imported_count": result["imported_count"],
                "total_quantity": result.get("total_quantity", result["imported_count"]),
                "moved_count": moved_count,
                "merged_count": merged_count,
                "skipped_count": 0,
                "stale_match_rows": stale_match_rows,
                "failed_rows": result["failed_rows"],
                "batch_id": result["batch_id"],
                "placed_in": placed_in,
                "placed_in_url": placed_in_url,
                "placed_in_kind": placed_in_kind,
                "skipped_deck_conflicts": skipped_deck_conflicts,
                "current_user": current_user,
            },
        )

    # 3-branch dispatch (no per-row resolution):
    #  (a) deck destination + reconciliation → deck per-row helper
    #  (b) non-deck destination + reconciliation → collection per-row helper
    #  (c) no reconciliation fields → existing path, byte-identical
    deck = _deck_for_storage_location(session, current_user.id, target_location_id)
    has_reconciliation = any(reconcile_action)
    skipped_count = 0

    if deck is not None and has_reconciliation:
        result = _commit_deck_import_with_reconciliation(
            session=session,
            user_id=current_user.id,
            deck=deck,
            parsed_rows=rows,
            actions=reconcile_action,
            move_qtys=[int(q or 0) for q in reconcile_move_qty],
            new_qtys=[int(q or 0) for q in reconcile_new_qty],
            filename=filename,
        )
        moved_count = result["moved_count"]
        merged_count = result.get("merged_count", 0)
        stale_match_rows = result["stale_match_rows"]
        placed_in = deck.name
        placed_in_url = f"/locations/{target_location_id}"
        placed_in_kind = "deck"
    elif deck is None and has_reconciliation:
        result = _commit_collection_import_with_reconciliation(
            session=session,
            user_id=current_user.id,
            target_location_id=target_location_id,
            parsed_rows=rows,
            actions=reconcile_action,
            new_qtys=[int(q or 0) for q in reconcile_new_qty],
            filename=filename,
        )
        merged_count = 0
        skipped_count = result.get("skipped_count", 0)
        stale_match_rows = result["stale_match_rows"]
        row_ids = result.get("imported_row_ids", [])

        if target_location_id:
            loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
            placed_in = loc.name if loc else None
            placed_in_url = f"/locations/{target_location_id}" if loc else "/pending"
            placed_in_kind = ("deck" if loc.type == "deck" else "location") if loc else None
            # No resort here: an explicit destination (box/binder/other or a
            # deck) was chosen, so the cards belong THERE. Running the drawer
            # sorter would yank them straight back out into the drawers. The
            # sorter only runs on the "Auto-sort to drawers" path (the elif
            # below, where no target_location_id was selected).
        elif row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
            # v3.38.0 intake routing: divert cheap non-staple surplus to Bulk
            # BEFORE the sorter runs, so the sorter only ever places keepers.
            route_intake_to_bulk(session, current_user.id, row_ids)
            resort_collection(session, user_id=current_user.id)
            return RedirectResponse(url="/pending", status_code=303)
    else:
        result = persist_import_rows(session, rows, filename=filename, user_id=current_user.id)
        merged_count = 0
        row_ids = result.get("imported_row_ids", [])

        if row_ids and target_location_id:
            place_imported_rows(
                session, row_ids, user_id=current_user.id, location_id=target_location_id
            )
            loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
            placed_in = loc.name if loc else None
            placed_in_url = f"/locations/{target_location_id}" if loc else "/pending"
            placed_in_kind = ("deck" if loc.type == "deck" else "location") if loc else None
            # No resort here: an explicit destination (box/binder/other or a
            # deck) was chosen, so the cards belong THERE. Running the drawer
            # sorter would yank them straight back out into the drawers. The
            # sorter only runs on the "Auto-sort to drawers" path (the elif
            # below, where no target_location_id was selected).

        elif row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
            # v3.38.0 intake routing: divert cheap non-staple surplus to Bulk
            # BEFORE the sorter runs, so the sorter only ever places keepers.
            route_intake_to_bulk(session, current_user.id, row_ids)
            resort_collection(session, user_id=current_user.id)
            return RedirectResponse(url="/pending", status_code=303)

    return render(
        request,
        "import_result.html",
        {
            "title": "Import Results",
            "imported_count": result["imported_count"],
            "total_quantity": result.get("total_quantity", result["imported_count"]),
            "moved_count": moved_count,
            "merged_count": merged_count,
            "shared_count": result.get("shared_count", 0),
            "skipped_count": skipped_count,
            "stale_match_rows": stale_match_rows,
            "failed_rows": result["failed_rows"],
            "batch_id": result["batch_id"],
            "placed_in": placed_in,
            "placed_in_url": placed_in_url,
            "placed_in_kind": placed_in_kind,
            "current_user": current_user,
        },
    )


@router.post("/import/manual/preview")
async def manual_import_preview(
    request: Request,
    scryfall_id: str = Form(""),
    set_code: str = Form(""),
    collector_number: str = Form(""),
    finish: str = Form("normal"),
    quantity: int = Form(1),
    language: str = Form("en"),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    card = None
    resolved_id = ""

    if scryfall_id.strip():
        resolved_id = scryfall_id.strip()
        card = fetch_card_by_scryfall_id(resolved_id)
    else:
        card = fetch_card_by_set_and_number(set_code, collector_number)
        if card:
            resolved_id = card["scryfall_id"]

    return render(
        request,
        "manual_preview.html",
        {
            "title": "Manual Import Preview",
            "card": card,
            "resolved_scryfall_id": resolved_id,
            "finish": normalize_finish(finish),
            "quantity": max(1, quantity),
            "language": normalize_language(language),
            "set_code": set_code,
            "collector_number": collector_number,
            "current_user": current_user,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
            "locations": list_locations(session, current_user.id),
            "decks": list_decks_basic(session, user_id=current_user.id),
        },
    )


@router.post("/import/manual/search")
async def manual_import_search(
    request: Request,
    name: str = Form(...),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    results = search_cards_by_name(name)

    return render(
        request,
        "manual_search_results.html",
        {
            "title": "Choose Printing",
            "query": name,
            "results": results,
            "current_user": current_user,
        },
    )


@router.post("/import/manual/reconcile-preview")
async def manual_import_reconcile_preview(
    request: Request,
    target_location_id: int = Form(0),
    scryfall_id: str = Form(""),
    set_code: str = Form(""),
    collector_number: str = Form(""),
    finish: str = Form("normal"),
    quantity: int = Form(1),
    language: str = Form("en"),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """HTMX endpoint for the single-card (manual) import preview.

    Dispatch shape mirrors /import/reconcile-preview (deck vs non-deck),
    but for the manual flow we always set `manual_mode=True` in the
    collection-mode render context. That flips the action-select default
    to `import_new` (acquisition semantics) instead of `skip_already_owned`,
    per design doc §5.5: manual single-card entries are usually
    acquisitions, not sync operations.

    For deck destinations the manual flow uses the same defaults as the
    CSV flow — manual_mode is collection-mode-only.
    """
    parsed_rows = [
        {
            "line_number": 1,
            "scryfall_id": scryfall_id,
            "set_code": set_code,
            "collector_number": collector_number,
            "finish": normalize_finish(finish),
            "quantity": max(1, quantity),
            "location": "",
            "name": "",
            "language": normalize_language(language),
        }
    ]

    def _decorate_single(rows: list[dict]) -> None:
        if rows and rows[0].get("card_id"):
            c = session.query(Card.name).filter(Card.id == rows[0]["card_id"]).first()
            rows[0]["display_name"] = c.name if c else scryfall_id
        elif rows:
            rows[0]["display_name"] = scryfall_id

    deck = _deck_for_storage_location(session, current_user.id, target_location_id)

    if deck is not None:
        matches_rows = find_inventory_matches_for_deck_import(
            session, current_user.id, deck.id, parsed_rows
        )
        _decorate_single(matches_rows)

        total_to_move = sum(r["recommended_move_qty"] for r in matches_rows)
        total_to_merge = sum(
            r["recommended_new_qty"] for r in matches_rows if r["total_in_target_deck"] > 0
        )
        total_to_import_new = sum(r["recommended_new_qty"] for r in matches_rows) - total_to_merge
        # v3.33.0 — copies covered by a sibling variant deck (no move, no import).
        total_covered_by_variant = sum(r.get("variant_covered_qty", 0) for r in matches_rows)

        return render(
            request,
            "_import_reconciliation.html",
            {
                "reconcile_mode": "deck",
                "rows": matches_rows,
                "deck_name": deck.name,
                "total_to_move": total_to_move,
                "total_to_import_new": total_to_import_new,
                "total_to_merge": total_to_merge,
                "total_covered_by_variant": total_covered_by_variant,
                "is_variant_group": deck.variant_group_id is not None,
                "is_brew": deck.is_brew,
            },
        )

    # Non-deck destination — collection mode with manual_mode=True.
    matches_rows = find_inventory_matches_for_collection_import(
        session, current_user.id, parsed_rows
    )
    _decorate_single(matches_rows)
    _annotate_collection_dupes(matches_rows)

    total_to_skip = 0
    total_to_delta = 0
    total_to_new = 0
    for r in matches_rows:
        action = r["recommended_action"]
        if action == "skip_already_owned":
            total_to_skip += r["quantity_needed"]
        elif action == "import_delta":
            total_to_delta += r["recommended_new_qty"]
            total_to_skip += r["quantity_needed"] - r["recommended_new_qty"]
        else:
            total_to_new += r["recommended_new_qty"]

    has_deck_only_dupes = any(r.get("is_deck_only_dupe") for r in matches_rows)

    destination_name: str | None = None
    if target_location_id > 0:
        loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
        if loc is not None:
            destination_name = loc.name

    return render(
        request,
        "_import_reconciliation.html",
        {
            "reconcile_mode": "collection",
            "rows": matches_rows,
            "deck_name": destination_name,
            "total_to_skip": total_to_skip,
            "total_to_delta": total_to_delta,
            "total_to_new": total_to_new,
            "manual_mode": True,  # default flips to import_new
            "has_deck_only_dupes": has_deck_only_dupes,
            "bulk_routing": _intake_routing_preview(
                session, current_user, target_location_id, matches_rows
            ),
        },
    )


@router.post("/import/manual/commit")
async def manual_import_commit(
    request: Request,
    scryfall_id: str = Form(""),
    set_code: str = Form(""),
    collector_number: str = Form(""),
    finish: str = Form("normal"),
    quantity: int = Form(1),
    language: str = Form("en"),
    target_location_id: int = Form(0),
    reconcile_action: list[str] = Form([]),
    reconcile_move_qty: list[str] = Form([]),
    reconcile_new_qty: list[str] = Form([]),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    parsed_rows = [
        {
            "line_number": 1,
            "scryfall_id": scryfall_id,
            "set_code": set_code,
            "collector_number": collector_number,
            "finish": normalize_finish(finish),
            "quantity": max(1, quantity),
            "location": "",
            "name": "",
            "language": normalize_language(language),
        }
    ]

    placed_in = None
    placed_in_url = "/pending"
    placed_in_kind = None
    moved_count = 0
    stale_match_rows: list[dict] = []

    deck = _deck_for_storage_location(session, current_user.id, target_location_id)
    has_reconciliation = any(reconcile_action)
    skipped_count = 0

    if deck is not None and has_reconciliation:
        result = _commit_deck_import_with_reconciliation(
            session=session,
            user_id=current_user.id,
            deck=deck,
            parsed_rows=parsed_rows,
            actions=reconcile_action,
            move_qtys=[int(q or 0) for q in reconcile_move_qty],
            new_qtys=[int(q or 0) for q in reconcile_new_qty],
            filename="manual import",
        )
        moved_count = result["moved_count"]
        merged_count = result.get("merged_count", 0)
        stale_match_rows = result["stale_match_rows"]
        placed_in = deck.name
        placed_in_url = f"/locations/{target_location_id}"
        placed_in_kind = "deck"
    elif deck is None and has_reconciliation:
        result = _commit_collection_import_with_reconciliation(
            session=session,
            user_id=current_user.id,
            target_location_id=target_location_id,
            parsed_rows=parsed_rows,
            actions=reconcile_action,
            new_qtys=[int(q or 0) for q in reconcile_new_qty],
            filename="manual import",
        )
        merged_count = 0
        skipped_count = result.get("skipped_count", 0)
        stale_match_rows = result["stale_match_rows"]
        row_ids = result.get("imported_row_ids", [])
        if target_location_id:
            loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
            placed_in = loc.name if loc else None
            placed_in_url = f"/locations/{target_location_id}" if loc else "/pending"
            placed_in_kind = ("deck" if loc.type == "deck" else "location") if loc else None
            # No resort here: an explicit destination (box/binder/other or a
            # deck) was chosen, so the cards belong THERE. Running the drawer
            # sorter would yank them straight back out into the drawers. The
            # sorter only runs on the "Auto-sort to drawers" path (the elif
            # below, where no target_location_id was selected).
        elif row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
            # v3.38.0 intake routing: divert cheap non-staple surplus to Bulk
            # BEFORE the sorter runs, so the sorter only ever places keepers.
            route_intake_to_bulk(session, current_user.id, row_ids)
            resort_collection(session, user_id=current_user.id)
    else:
        result = persist_import_rows(
            session, parsed_rows, filename="manual import", user_id=current_user.id
        )
        merged_count = 0
        row_ids = result.get("imported_row_ids", [])
        if row_ids and target_location_id:
            place_imported_rows(
                session, row_ids, user_id=current_user.id, location_id=target_location_id
            )
            loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
            placed_in = loc.name if loc else None
            placed_in_url = f"/locations/{target_location_id}" if loc else "/pending"
            placed_in_kind = ("deck" if loc.type == "deck" else "location") if loc else None
            # No resort here: an explicit destination (box/binder/other or a
            # deck) was chosen, so the cards belong THERE. Running the drawer
            # sorter would yank them straight back out into the drawers. The
            # sorter only runs on the "Auto-sort to drawers" path (the elif
            # below, where no target_location_id was selected).
        elif row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
            # v3.38.0 intake routing: divert cheap non-staple surplus to Bulk
            # BEFORE the sorter runs, so the sorter only ever places keepers.
            route_intake_to_bulk(session, current_user.id, row_ids)
            resort_collection(session, user_id=current_user.id)

    return render(
        request,
        "import_result.html",
        {
            "title": "Import Results",
            "imported_count": result["imported_count"],
            "total_quantity": result.get("total_quantity", result["imported_count"]),
            "moved_count": moved_count,
            "merged_count": merged_count,
            "shared_count": result.get("shared_count", 0),
            "skipped_count": skipped_count,
            "stale_match_rows": stale_match_rows,
            "failed_rows": result["failed_rows"],
            "batch_id": result["batch_id"],
            "placed_in": placed_in,
            "placed_in_url": placed_in_url,
            "placed_in_kind": placed_in_kind,
            "current_user": current_user,
        },
    )
