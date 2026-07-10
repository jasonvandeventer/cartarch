"""Universal import adapters (issue #81).

Content-sniffing normalizers that turn third-party collection/deck exports into
the canonical import row shape the existing pipeline already understands
(``name`` / ``set_code`` / ``collector_number`` / ``quantity`` / ``finish``,
plus an optional ``scryfall_id`` for faster resolution). Two adapters cover
three trackers:

* ``mtgo_text``     — the ``{qty} {name} ({SET}) {num} {finish}`` line format
                      shared by Moxfield and ManaBox text exports.
* ``archidekt_csv`` — Archidekt's headerless, positional 18-column CSV.

Detection is by content only (``detect_import_format``); there is no user
format picker — the shape is unambiguous from the bytes. The adapters produce
canonical input and nothing more; card resolution and persistence stay in
``import_service`` unchanged.
"""

from __future__ import annotations

import csv
import io
import re

# First non-empty line of an MTGO/Moxfield/ManaBox text export, e.g.
# "1 Sol Ring (FDC) 2 F". Set code is upper-alnum in these exports; the
# trailing collector number is what separates this from a prose line that
# merely happens to contain "(...)".
_MTGO_DETECT_RE = re.compile(r"^\d+ .+ \([A-Z0-9]+\) \d+")

# Full line parse. Case-insensitive on the set so a lower-cased hand edit still
# parses; the name is non-greedy so it stops at the LAST "(SET) num" group.
# ponytail: a card name that itself contains "(XYZ) 123" would mis-split — no
# such name exists in MTG; revisit only if a real card breaks it.
_MTGO_LINE_RE = re.compile(
    r"^\s*(?P<qty>\d+)\s+(?P<name>.+?)\s+\((?P<set>[A-Za-z0-9]+)\)\s+"
    r"(?P<num>\S+?)(?:\s+\*?(?P<finish>[FEfe])\*?)?\s*$"
)

_MTGO_FINISH = {"f": "foil", "e": "etched"}

# Archidekt "Finish" column values -> canonical finish.
_ARCHIDEKT_FINISH = {"normal": "normal", "foil": "foil", "etched": "etched"}

# Archidekt headerless positional columns (0-indexed). Only the ones we consume
# are named; the export carries 18 columns total (…price_weight, colors, cmc,
# rarity, type, price, ownership, oracle_text) that we ignore.
_ARCH_QTY = 0
_ARCH_NAME = 1
_ARCH_SET_CODE = 3
_ARCH_FINISH = 7
_ARCH_COLLECTOR = 8
_ARCH_SCRYFALL_ID = 13
_ARCH_MIN_FIELDS = 15


def _first_nonempty_line(content: str) -> str:
    for line in content.splitlines():
        if line.strip():
            return line.strip()
    return ""


def detect_import_format(content: str) -> str | None:
    """Sniff the export format from raw file content.

    Returns ``"mtgo_text"``, ``"archidekt_csv"``, or ``None`` (let the caller
    fall through to the existing header-based CSV detection).
    """
    first = _first_nonempty_line(content)
    if not first:
        return None
    if _MTGO_DETECT_RE.match(first):
        return "mtgo_text"
    # Archidekt: headerless CSV whose first field is an integer quantity and
    # which carries the full positional column set.
    try:
        fields = next(csv.reader(io.StringIO(first)))
    except (StopIteration, csv.Error):
        return None
    if len(fields) >= _ARCH_MIN_FIELDS and fields[0].strip().isdigit():
        return "archidekt_csv"
    return None


def normalize_mtgo_text(content: str) -> list[dict]:
    """Parse the Moxfield/ManaBox MTGO text format into canonical rows.

    Each line is ``{qty} {name} ({SET}) {collector} {finish?}`` where finish is
    ``F`` (foil), ``E`` (etched), or absent (normal), optionally wrapped in
    asterisks (``*F*``). Lines that don't match are skipped — the caller has
    already confirmed the format from the first line, so a stray blank/comment
    line is not a row.
    """
    rows: list[dict] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        m = _MTGO_LINE_RE.match(line)
        if not m:
            continue
        finish = _MTGO_FINISH.get((m.group("finish") or "").lower(), "normal")
        rows.append(
            {
                "name": m.group("name").strip(),
                "set_code": m.group("set"),
                "collector_number": m.group("num"),
                "quantity": int(m.group("qty")),
                "finish": finish,
            }
        )
    return rows


def normalize_archidekt_csv(content: str) -> list[dict]:
    """Parse Archidekt's headerless positional CSV into canonical rows.

    Uses ``csv.reader`` so quoted fields with embedded commas/quotes (the
    ``oracle_text`` column) are handled correctly. Rows without the full column
    set or a non-integer quantity are skipped. ``scryfall_id`` is carried
    through when present so downstream resolution can skip the set+collector
    lookup.
    """
    rows: list[dict] = []
    for fields in csv.reader(io.StringIO(content)):
        if len(fields) < _ARCH_MIN_FIELDS:
            continue
        qty_raw = fields[_ARCH_QTY].strip()
        if not qty_raw.isdigit():
            continue
        finish = _ARCHIDEKT_FINISH.get(fields[_ARCH_FINISH].strip().lower(), "normal")
        row = {
            "name": fields[_ARCH_NAME].strip(),
            "set_code": fields[_ARCH_SET_CODE].strip(),
            "collector_number": fields[_ARCH_COLLECTOR].strip(),
            "quantity": int(qty_raw),
            "finish": finish,
        }
        scryfall_id = fields[_ARCH_SCRYFALL_ID].strip()
        if scryfall_id:
            row["scryfall_id"] = scryfall_id
        rows.append(row)
    return rows
