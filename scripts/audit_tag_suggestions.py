"""Audit harness for `suggest_card_roles` (Tag System Overhaul §2.2).

Walks a user's active-deck card pool, runs the auto-tagger in three modes
per card (intrinsic-only + themes-aware-per-deck + reads user tags), and
emits a markdown audit table to `docs/tag_audit.md` ready for the
reviewer to mark each row correct / false-pos / false-neg / partial.

This script is the precondition for the rest of the Tag Overhaul work
per docs/tag_system_overhaul.md §2.2. It does not modify any DB rows.

Sample bucketing follows the doc's recommendation: ~100 unique non-basic
cards, prioritized to surface known-bad cases first, then ambiguous and
complex cases, with a random remainder.

Usage
-----

Run from the project root with the same Python env the app uses:

    python -m scripts.audit_tag_suggestions \\
        --user jason@vanfreckle.com \\
        --output docs/tag_audit.md

Against prod (read-only — the script never writes to the DB):

    kubectl exec -n mana-archive deploy/mana-archive -- \\
        python -m scripts.audit_tag_suggestions \\
        --user jason@vanfreckle.com \\
        --output /tmp/tag_audit.md
    kubectl cp -n mana-archive \\
        $(kubectl get pods -n mana-archive -l app=mana-archive \\
            -o jsonpath='{.items[0].metadata.name}'):/tmp/tag_audit.md \\
        docs/tag_audit.md

The script refuses to overwrite an existing output file unless `--force`
is passed, so manual review marks aren't accidentally clobbered on
re-run.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# EDHREC enrichment (Tag Overhaul §2.2 audit-time enrichment, option A).
#
# For each (card, commander) pair in the audit, we look up the card's synergy
# score on the commander's EDHREC page. High synergy with a deck commander is
# strong evidence the Synergy tag is justified for that card in that deck —
# directly addressing the false-positive Synergy problem from §1.4.
#
# EDHREC does NOT expose role-based categorization (Ramp / Removal / Draw)
# via its JSON API, so this enrichment helps Synergy auditing specifically;
# other tag categories still need manual review.
#
# Data flow: fetch each commander's page once (cached to disk), build an
# index of (commander_slug, card_name) → synergy entry, then look up each
# audit card per its deck context.
#
# Cache lives in ``dev-data/edhrec_cache/`` by default. Cache is keyed on
# commander slug only — card-level lookups read from the cached commander
# pages. Re-runs hit the cache; first run is the slow one.
# ----------------------------------------------------------------------------

EDHREC_COMMANDER_URL = "https://json.edhrec.com/pages/commanders/{slug}.json"

_EDHREC_SLUG_RE_NONALNUM = re.compile(r"[^a-z0-9\s-]+")
_EDHREC_SLUG_RE_DASHES = re.compile(r"[\s-]+")


def edhrec_slug(name: str) -> str:
    """Convert a card / commander name to an EDHREC URL slug.

    EDHREC slugs are lowercase ASCII, hyphen-separated, with apostrophes and
    other punctuation stripped. Diacritics get decomposed and stripped
    ("Lim-Dûl" → "lim-dul"). Partner pairs and Backgrounds use the front
    card's name only; callers are responsible for passing the right name.
    """
    decomposed = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    cleaned = _EDHREC_SLUG_RE_NONALNUM.sub("", decomposed.lower())
    return _EDHREC_SLUG_RE_DASHES.sub("-", cleaned).strip("-")


def fetch_edhrec_commander(
    slug: str, cache_dir: Path, throttle_seconds: float = 0.25
) -> dict | None:
    """Fetch EDHREC's commander page JSON, with on-disk caching.

    Returns the parsed JSON dict on success, ``None`` on any failure
    (404, timeout, malformed JSON, network error). The audit treats
    "no EDHREC data" as a graceful fallback rather than an error.

    Throttle: sleep briefly after each live fetch so we don't hammer the
    unofficial API. Cache hits don't sleep.
    """
    if not slug:
        return None
    cache_path = cache_dir / f"commander_{slug}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            try:
                cache_path.unlink()
            except OSError:
                pass

    url = EDHREC_COMMANDER_URL.format(slug=slug)
    try:
        resp = requests.get(url, timeout=10)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cache_path.write_text(json.dumps(data))
    except OSError:
        pass

    if throttle_seconds:
        time.sleep(throttle_seconds)
    return data


def build_commander_card_index(commander_json: dict) -> dict[str, dict]:
    """Index every card on a commander's page by lower-cased name.

    Returns {name_lower: {"synergy": float|None, "num_decks": int,
    "potential_decks": int, "list_tag": str, "list_header": str}}.

    `list_tag` / `list_header` carry the EDHREC cardlist the card appears
    under (e.g. "highsynergycards" / "High Synergy Cards"). A card appearing
    in multiple lists is recorded once with the FIRST list's metadata —
    EDHREC orders the lists by curated relevance so the first hit is the
    "most prominent" surface for that card.
    """
    out: dict[str, dict] = {}
    if not isinstance(commander_json, dict):
        return out
    container = commander_json.get("container", {})
    jd = container.get("json_dict", {}) if isinstance(container, dict) else {}
    cardlists = jd.get("cardlists", []) if isinstance(jd, dict) else []
    if not isinstance(cardlists, list):
        return out

    for entry in cardlists:
        if not isinstance(entry, dict):
            continue
        list_tag = entry.get("tag") or ""
        list_header = entry.get("header") or ""
        for cv in entry.get("cardviews", []) or []:
            if not isinstance(cv, dict):
                continue
            name = cv.get("name") or ""
            key = name.lower()
            if not key or key in out:
                continue
            out[key] = {
                "synergy": cv.get("synergy"),
                "num_decks": cv.get("num_decks"),
                "potential_decks": cv.get("potential_decks"),
                "list_tag": list_tag,
                "list_header": list_header,
            }
    return out


def _format_edhrec_summary(deck_name: str, commander_name: str, entry: dict | None) -> str:
    """Render one deck's EDHREC summary line for the audit row.

    Shows the deck label and the commander name only when they differ,
    to avoid the redundant "Teysa Karlov (Teysa Karlov)" output for
    decks named after their commander.
    """
    if (deck_name or "").strip().lower() == (commander_name or "").strip().lower():
        label = f"**{deck_name}**"
    else:
        label = f"**{deck_name}** ({commander_name})"

    if entry is None:
        return f"{label}: not surfaced on EDHREC"
    synergy = entry.get("synergy")
    num = entry.get("num_decks") or 0
    pot = entry.get("potential_decks") or 0
    incl_pct = (num / pot * 100) if pot else None
    list_header = entry.get("list_header") or ""

    parts: list[str] = []
    if synergy is not None:
        parts.append(f"synergy {synergy:+.2f}")
    if incl_pct is not None:
        parts.append(f"in {incl_pct:.1f}% of decks")
    if list_header:
        parts.append(f"list={list_header}")
    body = "; ".join(parts) or "(no signal)"
    return f"{label}: {body}"


# Hand-curated list of cards we already know are mis-tagged from real
# tagging sessions (per docs/tag_system_overhaul.md §1.4). These go in
# the "known mis-tagged" bucket if present in the user's collection.
KNOWN_MISTAGGED = frozenset(
    {
        "Solemn Simulacrum",  # Ramp but missing Draw
        "Faramir, Field Commander",  # multi-mode: Draw + token
        "Gilded Goose",  # Ramp; should be Ramp+Synergy in Food deck
        "Revive the Shire",  # name suggests ramp, actually recursion + Food
        "Echoing Assault",  # name suggests removal, actually combat trick
        "Hazel of the Rootbloom",  # name suggests beater, actually token-copy engine
        "Diabolic Edict",  # false Synergy in sac deck
        "Feasting Hobbit",  # Devour-Food over-tagged Synergy
        "Leyline of Hope",  # replacement effect missed
        "Mystic Forge",  # cast-from-top edge case
        "Future Sight",  # cast-from-top edge case
        "Sheoldred, the Apocalypse",  # trigger-condition draw class
        "Underworld Dreams",  # opponent-draws punisher
        # Common Edict / mass-edict cards (Synergy false-positive in sac decks)
        "Plaguecrafter",
        "Demon's Disciple",
        "Eldest Reborn",
        "Promise of Loyalty",
        # Mass damage cards (Wipe / Removal disambiguation)
        "Syr Konrad, the Grim",
        "Serrated Scorpion",
    }
)


def _detect_complex(oracle_text: str) -> bool:
    """True for cards whose oracle has multi-mode / replacement / granted
    characteristics that historically trip the auto-tagger up.
    """
    if not oracle_text:
        return False
    lower = oracle_text.lower()
    cues = [
        " // ",  # multi-faced / MDFC / Adventure
        "if you would",  # replacement effects (Leyline of Hope class)
        "instead",  # replacement-effect tail
        "choose one",  # modal
        "choose two",
        "choose up to",
        '"',  # quoted granted ability (Sifter of Skulls class)
        "kicker",
        "escape",
        "flashback",
        "adventure",
        "devour",
    ]
    return any(c in lower for c in cues)


def _fmt_tags(tags: list[str]) -> str:
    return ", ".join(tags) if tags else "—"


def _fmt_deck_themes(deck_outputs: list[tuple[str, list[str]]], intrinsic: list[str]) -> str:
    """Compact rendering of per-deck themes-aware tagger output.

    Only surface deck contexts where the themes-aware output differs from
    the intrinsic output — otherwise it's just noise (every basic gets
    "no Synergy added" repeated across every deck the basic appears in).
    """
    interesting = [
        (deck_name, tags) for deck_name, tags in deck_outputs if set(tags) != set(intrinsic)
    ]
    if not interesting:
        return "(same as intrinsic)"
    parts = []
    for deck_name, tags in interesting:
        parts.append(f"**{deck_name}**: {_fmt_tags(tags)}")
    return " · ".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--user",
        required=True,
        help="Username (or email) whose decks to audit.",
    )
    parser.add_argument(
        "--output",
        default="docs/tag_audit.md",
        help="Output markdown path. Default: docs/tag_audit.md",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=100,
        help="Target sample size (default 100; doc recommends 100).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260514,
        help="Random seed for reproducible sampling (default fixed).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing output file. Default refuses.",
    )
    parser.add_argument(
        "--no-edhrec",
        action="store_true",
        help=(
            "Skip EDHREC enrichment. Default fetches per-commander synergy "
            "data from EDHREC's JSON API (cached under dev-data/edhrec_cache/)."
        ),
    )
    parser.add_argument(
        "--edhrec-cache",
        default="dev-data/edhrec_cache",
        help="Directory for the EDHREC commander-page cache. Default: dev-data/edhrec_cache",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    if output_path.exists() and not args.force:
        sys.stderr.write(
            f"refusing to overwrite existing {output_path} — pass --force to clobber.\n"
        )
        sys.exit(2)

    # Local imports — runs in the same env as the migration scripts, so
    # `app.*` is importable after argparse to avoid surprising the user
    # with an import error before `--help` resolves.
    from sqlalchemy.orm import Session, joinedload

    from app.db import engine
    from app.deck_service import (
        CARD_ROLE_TAGS,
        extract_commander_themes,
        get_row_tags,
        suggest_card_roles,
    )
    from app.models import Deck, InventoryRow, User

    valid_tags = set(CARD_ROLE_TAGS)

    with Session(engine) as session:
        user = (
            session.query(User)
            .filter((User.username == args.user) | (User.display_name == args.user))
            .first()
        )
        if not user:
            sys.stderr.write(f"user not found: {args.user!r}\n")
            sys.exit(1)

        # Active decks: every deck owned by the user. (The doc says "active";
        # in a future revision we could filter by Game presence, but for now
        # "deck exists" is the right signal — the user explicitly curates
        # these.)
        decks = session.query(Deck).filter(Deck.user_id == user.id).all()
        if not decks:
            sys.stderr.write(f"user {args.user!r} has no decks; nothing to audit.\n")
            sys.exit(1)

        sys.stderr.write(f"Auditing {len(decks)} decks for {args.user}...\n")

        # For each deck, fetch commander rows + non-commander rows. Build a
        # map: card_id → list of (deck_name, themes_dict, user_tags_on_row).
        card_contexts: dict[int, dict] = {}
        # Map deck.id → list of commander names (in row order). Used later
        # to build EDHREC commander indexes lazily.
        deck_commanders: dict[int, list[str]] = {}

        for deck in decks:
            if not deck.storage_location_id:
                continue

            commander_rows = (
                session.query(InventoryRow)
                .options(joinedload(InventoryRow.card))
                .filter(
                    InventoryRow.user_id == user.id,
                    InventoryRow.storage_location_id == deck.storage_location_id,
                    InventoryRow.role == "commander",
                )
                .all()
            )
            themes = extract_commander_themes(commander_rows) if commander_rows else None
            commander_names = [r.card.name for r in commander_rows if r.card]
            deck_commanders[deck.id] = commander_names

            deck_rows = (
                session.query(InventoryRow)
                .options(joinedload(InventoryRow.card))
                .filter(
                    InventoryRow.user_id == user.id,
                    InventoryRow.storage_location_id == deck.storage_location_id,
                )
                .all()
            )

            for row in deck_rows:
                if not row.card:
                    continue
                # Skip basic lands — they're not interesting for an
                # auto-tagger audit.
                type_line = (row.card.type_line or "").lower()
                if "basic" in type_line and any(
                    name in type_line
                    for name in ("plains", "island", "swamp", "mountain", "forest", "wastes")
                ):
                    continue

                entry = card_contexts.setdefault(
                    row.card.id,
                    {
                        "card": row.card,
                        "deck_themes": [],
                        "user_tags": set(),
                    },
                )
                entry["deck_themes"].append((deck.name, themes, deck.id))
                for t in get_row_tags(row):
                    if t in valid_tags:
                        entry["user_tags"].add(t)

        if not card_contexts:
            sys.stderr.write("No non-basic cards found across any deck. Audit aborted.\n")
            sys.exit(1)

        sys.stderr.write(
            f"Collected {len(card_contexts)} unique non-basic cards across active decks.\n"
        )

        # ---- EDHREC enrichment: fetch each deck's commander page(s),
        # index by card name, then look up audit cards per-deck-context. ----
        # Map deck.id → list of (commander_name, card_index_dict). Multiple
        # entries when the deck has partner / Background commanders; each
        # commander gets its own EDHREC page lookup. An empty list means
        # EDHREC enrichment is disabled or no commanders found.
        deck_edhrec: dict[int, list[tuple[str, dict[str, dict]]]] = {}
        if not args.no_edhrec:
            cache_dir = Path(args.edhrec_cache)
            cache_dir.mkdir(parents=True, exist_ok=True)
            sys.stderr.write(
                f"Fetching EDHREC commander pages "
                f"(cache: {cache_dir}, ~0.25s throttle per live fetch)...\n"
            )
            for deck_id, commander_names in deck_commanders.items():
                indexes: list[tuple[str, dict[str, dict]]] = []
                for c_name in commander_names:
                    slug = edhrec_slug(c_name)
                    if not slug:
                        continue
                    cj = fetch_edhrec_commander(slug, cache_dir)
                    if cj is None:
                        sys.stderr.write(
                            f"  no EDHREC data for commander {c_name!r} (slug={slug})\n"
                        )
                        continue
                    indexes.append((c_name, build_commander_card_index(cj)))
                deck_edhrec[deck_id] = indexes

        # ---- Per-card analysis ----
        analyzed: list[dict] = []
        for _card_id, ctx in card_contexts.items():
            card = ctx["card"]
            intrinsic_tags = suggest_card_roles(card, themes=None)
            deck_outputs: list[tuple[str, list[str]]] = []
            edhrec_summaries: list[str] = []
            commander_ambiguous = False
            for deck_name, themes, deck_id in ctx["deck_themes"]:
                themed = suggest_card_roles(card, themes=themes)
                deck_outputs.append((deck_name, themed))
                if set(themed) != set(intrinsic_tags):
                    commander_ambiguous = True

                # EDHREC lookup: for each commander on this deck, find this
                # card in the commander's cardlists (by lower-cased name).
                # Record one summary line per commander even if the card
                # isn't surfaced — "not surfaced" is itself a meaningful
                # signal (low synergy with this commander).
                for c_name, idx in deck_edhrec.get(deck_id, []):
                    entry = idx.get(card.name.lower()) if card.name else None
                    edhrec_summaries.append(_format_edhrec_summary(deck_name, c_name, entry))

            analyzed.append(
                {
                    "card": card,
                    "intrinsic_tags": intrinsic_tags,
                    "deck_outputs": deck_outputs,
                    "edhrec_summaries": edhrec_summaries,
                    "user_tags": sorted(ctx["user_tags"]),
                    "known_mistagged": card.name in KNOWN_MISTAGGED,
                    "commander_ambiguous": commander_ambiguous,
                    "complex_oracle": _detect_complex(card.oracle_text or ""),
                }
            )

    # ---- Bucketing per the doc's 30/30/20/20 split ----
    # Stable random sampling so re-running the audit gives the same
    # rows in the same order (modulo db state).
    rng = random.Random(args.seed)
    rng.shuffle(analyzed)

    known = [a for a in analyzed if a["known_mistagged"]]
    ambiguous = [a for a in analyzed if a["commander_ambiguous"] and not a["known_mistagged"]]
    complex_oracle = [
        a
        for a in analyzed
        if a["complex_oracle"] and not a["known_mistagged"] and not a["commander_ambiguous"]
    ]
    remainder = [
        a
        for a in analyzed
        if not a["known_mistagged"] and not a["commander_ambiguous"] and not a["complex_oracle"]
    ]

    sample: list[dict] = []
    sample.extend(known[:30])
    sample.extend(ambiguous[:20])
    sample.extend(complex_oracle[:20])
    # Fill remainder up to the target sample size with random picks.
    remaining_slots = max(0, args.sample_size - len(sample))
    sample.extend(remainder[:remaining_slots])
    # If we still have headroom (small collection, few known/ambiguous/complex),
    # backfill from any unused entries we haven't already included.
    if len(sample) < args.sample_size:
        included = {id(a) for a in sample}
        for a in known + ambiguous + complex_oracle + remainder:
            if len(sample) >= args.sample_size:
                break
            if id(a) not in included:
                sample.append(a)
                included.add(id(a))

    # ---- Markdown emission ----
    lines: list[str] = []
    lines.append("# Tag Auto-Tagger Audit")
    lines.append("")
    lines.append(
        f"Audit run for **{args.user}** across {len(decks)} decks "
        f"({len(card_contexts)} unique non-basic cards total, {len(sample)} sampled)."
    )
    lines.append("")
    lines.append("**How to use this file:**")
    lines.append("")
    lines.append(
        "1. For each row, compare the **Auto-tagger output** to the **Expected tags** "
        "you write in. Use docs/tag_system_overhaul.md §2.1 as the rule reference."
    )
    lines.append(
        "2. Fill in the **Mark** column with one of: `correct`, `false-pos`, "
        "`false-neg`, `partial`."
    )
    lines.append(
        "3. Add brief **Notes** for any case that informs a pattern change "
        "(§2.3) or a theme addition (§2.4)."
    )
    lines.append(
        "4. Findings drive the code changes that follow this audit. Don't skip rows "
        "— a blank Mark is treated as unreviewed."
    )
    lines.append("")
    lines.append("**Buckets:**")
    lines.append(f"- known-mistagged: {sum(1 for a in sample if a['known_mistagged'])}")
    lines.append(
        f"- commander-ambiguous: {sum(1 for a in sample if a['commander_ambiguous'] and not a['known_mistagged'])}"
    )
    lines.append(
        f"- complex-oracle: {sum(1 for a in sample if a['complex_oracle'] and not a['commander_ambiguous'] and not a['known_mistagged'])}"
    )
    lines.append(
        f"- random remainder: {sum(1 for a in sample if not a['known_mistagged'] and not a['commander_ambiguous'] and not a['complex_oracle'])}"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    for idx, a in enumerate(sample, start=1):
        card = a["card"]
        bucket = (
            "known-mistagged"
            if a["known_mistagged"]
            else (
                "commander-ambiguous"
                if a["commander_ambiguous"]
                else "complex-oracle" if a["complex_oracle"] else "random"
            )
        )
        lines.append(
            f"## {idx}. {card.name}"
            f"  · {(card.set_code or '').upper()} #{card.collector_number or '?'}"
            f"  · _{bucket}_"
        )
        lines.append("")
        type_line = card.type_line or "(no type line)"
        lines.append(f"- **Type:** {type_line}")
        lines.append(f"- **Auto-tagger (intrinsic):** {_fmt_tags(a['intrinsic_tags'])}")
        lines.append(
            f"- **Auto-tagger (themes-aware):** {_fmt_deck_themes(a['deck_outputs'], a['intrinsic_tags'])}"
        )
        lines.append(f"- **Current user tags:** {_fmt_tags(a['user_tags'])}")
        edhrec_summaries = a.get("edhrec_summaries") or []
        if edhrec_summaries:
            lines.append(f"- **EDHREC:** {' · '.join(edhrec_summaries)}")
        lines.append("- **Expected tags:**  ")
        lines.append("- **Mark:**  ")
        lines.append("- **Notes:**  ")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines))
    sys.stderr.write(f"Wrote audit to {output_path} ({len(sample)} cards).\n")


if __name__ == "__main__":
    main()
