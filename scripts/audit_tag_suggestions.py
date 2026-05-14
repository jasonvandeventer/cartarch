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
import random
import sys
from pathlib import Path

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
                entry["deck_themes"].append((deck.name, themes))
                for t in get_row_tags(row):
                    if t in valid_tags:
                        entry["user_tags"].add(t)

        if not card_contexts:
            sys.stderr.write("No non-basic cards found across any deck. Audit aborted.\n")
            sys.exit(1)

        sys.stderr.write(
            f"Collected {len(card_contexts)} unique non-basic cards across active decks.\n"
        )

        # ---- Per-card analysis ----
        analyzed: list[dict] = []
        for _card_id, ctx in card_contexts.items():
            card = ctx["card"]
            intrinsic_tags = suggest_card_roles(card, themes=None)
            deck_outputs: list[tuple[str, list[str]]] = []
            commander_ambiguous = False
            for deck_name, themes in ctx["deck_themes"]:
                themed = suggest_card_roles(card, themes=themes)
                deck_outputs.append((deck_name, themed))
                if set(themed) != set(intrinsic_tags):
                    commander_ambiguous = True

            analyzed.append(
                {
                    "card": card,
                    "intrinsic_tags": intrinsic_tags,
                    "deck_outputs": deck_outputs,
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
        lines.append("- **Expected tags:**  ")
        lines.append("- **Mark:**  ")
        lines.append("- **Notes:**  ")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines))
    sys.stderr.write(f"Wrote audit to {output_path} ({len(sample)} cards).\n")


if __name__ == "__main__":
    main()
