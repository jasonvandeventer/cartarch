"""Sweep Class 3 fabricated pending real rows (issue #136, follow-up to #134 / #140).

BACKGROUND
    `return_card_from_deck` was `is_proxy`-blind (fixed in #140 / v4.11.19): its
    merge filter omitted `is_proxy` and the pending row it created defaulted
    `is_proxy=FALSE`, so returning a brew *proxy* deleted the proxy and MINTED a
    pending REAL row for a card the user does not own. #140 stops NEW ones (a brew
    proxy is now discarded, not returned); this sweeps the rows already written.

CLASS 3 (the only class present in prod — Class 1 & 2 measured 0)
    A pending, non-proxy InventoryRow targeted by a `return_from_deck` transaction
    whose `source_location` is a BREW deck. These are invisible to classes 1 & 2:
    non-proxy, pending, outside any deck location.

    Correction: **DELETE the fabricated row.** Do NOT re-create the proxy in the
    source deck — the user's removal of the card from the brew was intentional;
    only the row the defect minted is corruption. Deletion is FK-safe via
    `clean_inventory_row_references` and recorded in the transaction log.

    Confidence: `other_real_rows == 0` -> high confidence fabricated (INCLUDED by
    default). `other_real_rows > 0` -> ambiguous, the user may genuinely own copies
    and have placed a real row in the brew deliberately -> **DEFAULT OPT-OUT**
    (skipped unless `--include-ambiguous`). Detection can't recover whether the row
    was a proxy at return time (the flag was destroyed by the defect); other_real_rows
    is the proxy for that judgment and the per-row preview is the backstop.

CLASS 1 & 2
    ENUMERATED in the preview for completeness (their correction is a per-row
    split/flip judgment, not a delete) but this script NEVER auto-corrects them.
    If any appear it prints them and leaves them untouched.

SAFETY
    - **Preview (dry-run) is the DEFAULT.** Pass `--apply` to write.
    - Per-row opt-out via `--exclude <id,id,...>`.
    - Ambiguous rows (other_real_rows > 0) opt-out by default; `--include-ambiguous`
      to sweep them too.
    - All deletions run in ONE transaction (all-or-nothing), each preceded by
      `clean_inventory_row_references` and a `correct_fabricated_return` TransactionLog.

USAGE
    python -m scripts.sweep_class3_fabricated_returns                 # preview (default)
    python -m scripts.sweep_class3_fabricated_returns --apply         # delete high-confidence
    python -m scripts.sweep_class3_fabricated_returns --apply --include-ambiguous
    python -m scripts.sweep_class3_fabricated_returns --apply --exclude 16691,21703

    In-cluster, DATABASE_URL lives only in PID 1's env (see CLAUDE.md -> Telemetry):
      kubectl exec -n cnpg-system deploy/cartarch -- sh -c \\
        'export PYTHONPATH=/app; export $(tr "\\0" "\\n" < /proc/1/environ | grep ^DATABASE_URL=); \\
         python -m scripts.sweep_class3_fabricated_returns'
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import and_, case, func, literal
from sqlalchemy.orm import Session

import app.legacy_tables  # noqa: F401 — registers raw tables on Base.metadata
from app.db import SessionLocal
from app.inventory_service import clean_inventory_row_references
from app.models import Card, Deck, InventoryRow, StorageLocation, TransactionLog

# The audit event written for every applied Class 3 correction.
CORRECTION_EVENT = "correct_fabricated_return"


@dataclass
class Class3Row:
    row_id: int
    user_id: int
    card_id: int
    card_name: str
    finish: str
    quantity: int
    deck_name: str
    other_real_rows: int

    @property
    def ambiguous(self) -> bool:
        return self.other_real_rows > 0


# --------------------------------------------------------------------------- #
# Detection (read-only)
# --------------------------------------------------------------------------- #


def detect_class1(session: Session) -> list[dict]:
    """Proxy rows a pull_to_deck / import_merge transaction points at (absorbed a
    real copy). Engine-agnostic ORM form of the issue's Class 1 SQL."""
    rows = (
        session.query(
            InventoryRow.id,
            InventoryRow.user_id,
            InventoryRow.card_id,
            InventoryRow.finish,
            InventoryRow.quantity,
            StorageLocation.name.label("deck"),
        )
        .join(StorageLocation, StorageLocation.id == InventoryRow.storage_location_id)
        .join(TransactionLog, TransactionLog.inventory_row_id == InventoryRow.id)
        .filter(
            InventoryRow.is_proxy.is_(True),
            StorageLocation.type == "deck",
            TransactionLog.event_type.in_(("pull_to_deck", "import_merge")),
        )
        .distinct()
        .all()
    )
    return [r._asdict() for r in rows]


def detect_class2(session: Session) -> list[dict]:
    """Visible real+proxy sibling pairs of one (card, finish) in one deck location.
    `func.sum(case(...))` replaces PG-only `COUNT(*) FILTER` so this runs on SQLite too."""
    proxy_rows = func.sum(case((InventoryRow.is_proxy, 1), else_=0))
    real_rows = func.sum(case((InventoryRow.is_proxy, 0), else_=1))
    rows = (
        session.query(
            InventoryRow.user_id,
            InventoryRow.card_id,
            InventoryRow.finish,
            InventoryRow.storage_location_id,
            proxy_rows.label("proxy_rows"),
            real_rows.label("real_rows"),
        )
        .join(StorageLocation, StorageLocation.id == InventoryRow.storage_location_id)
        .filter(StorageLocation.type == "deck", InventoryRow.is_pending.is_(False))
        .group_by(
            InventoryRow.user_id,
            InventoryRow.card_id,
            InventoryRow.finish,
            InventoryRow.storage_location_id,
        )
        .having(and_(proxy_rows > 0, real_rows > 0))
        .all()
    )
    return [r._asdict() for r in rows]


def detect_class3(session: Session) -> list[Class3Row]:
    """Fabricated pending real rows minted by the is_proxy-blind return path.

    Joins the return transaction to its source brew deck by name AND user (the
    issue SQL joined on name only — adding user_id guards against two users owning
    a same-named deck; it does not change the verified prod result)."""
    candidates = (
        session.query(InventoryRow, Card.name, Deck.name.label("deck"))
        .join(TransactionLog, TransactionLog.inventory_row_id == InventoryRow.id)
        .join(
            Deck,
            and_(
                TransactionLog.source_location == (literal("deck:") + Deck.name),
                Deck.is_brew.is_(True),
                Deck.user_id == InventoryRow.user_id,
            ),
        )
        .join(Card, Card.id == InventoryRow.card_id)
        .filter(
            TransactionLog.event_type == "return_from_deck",
            InventoryRow.is_pending.is_(True),
            InventoryRow.is_proxy.is_(False),
        )
        .all()
    )

    out: dict[int, Class3Row] = {}
    for row, card_name, deck_name in candidates:
        if row.id in out:  # a row could match >1 return tx; count it once
            continue
        other_real = (
            session.query(func.count(InventoryRow.id))
            .filter(
                InventoryRow.user_id == row.user_id,
                InventoryRow.card_id == row.card_id,
                InventoryRow.id != row.id,
                InventoryRow.is_proxy.is_(False),
                InventoryRow.is_pending.is_(False),
            )
            .scalar()
        )
        out[row.id] = Class3Row(
            row_id=row.id,
            user_id=row.user_id,
            card_id=row.card_id,
            card_name=card_name,
            finish=row.finish,
            quantity=row.quantity,
            deck_name=deck_name,
            other_real_rows=int(other_real or 0),
        )
    # newest-corruption-first is nice for the preview but stable order is enough
    return sorted(out.values(), key=lambda r: r.row_id, reverse=True)


# --------------------------------------------------------------------------- #
# Selection + correction
# --------------------------------------------------------------------------- #


def select_for_deletion(
    class3: list[Class3Row], *, exclude_ids: set[int], include_ambiguous: bool
) -> tuple[list[Class3Row], list[tuple[Class3Row, str]]]:
    """Split Class 3 rows into (to_delete, skipped-with-reason)."""
    to_delete: list[Class3Row] = []
    skipped: list[tuple[Class3Row, str]] = []
    for r in class3:
        if r.row_id in exclude_ids:
            skipped.append((r, "excluded (--exclude)"))
        elif r.ambiguous and not include_ambiguous:
            skipped.append((r, f"ambiguous (other_real_rows={r.other_real_rows}), default opt-out"))
        else:
            to_delete.append(r)
    return to_delete, skipped


def apply_class3(session: Session, rows: list[Class3Row]) -> int:
    """Delete each fabricated row, FK-safe and audited. Caller commits."""
    for r in rows:
        # Audit BEFORE the row is gone (inventory_row_id is a documentary column,
        # not an FK, so it survives the delete as the record of what was removed).
        session.add(
            TransactionLog(
                user_id=r.user_id,
                event_type=CORRECTION_EVENT,
                card_id=r.card_id,
                finish=r.finish,
                quantity_delta=-r.quantity,
                source_location="collection",
                destination_location=None,
                inventory_row_id=r.row_id,
                note=(
                    f"#136 Class 3: deleted fabricated pending real row "
                    f"(was a brew proxy returned via the is_proxy-blind path, #140) "
                    f"— {r.card_name} x{r.quantity} from brew '{r.deck_name}'"
                ),
            )
        )
        clean_inventory_row_references(session, [r.row_id])
        session.delete(session.get(InventoryRow, r.row_id))
    return len(rows)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _print_preview(session: Session, to_delete, skipped) -> None:
    c1, c2 = detect_class1(session), detect_class2(session)
    print("== #136 corruption sweep — PREVIEW (read-only) ==\n")
    print(f"Class 1 (proxy absorbed real copies): {len(c1)}")
    for r in c1:
        print(
            f"  row {r['id']} u{r['user_id']} card={r['card_id']} finish={r['finish']} qty={r['quantity']} deck={r['deck']!r}"
        )
    print(f"Class 2 (real+proxy sibling pairs):   {len(c2)}")
    for r in c2:
        print(
            f"  u{r['user_id']} card={r['card_id']} finish={r['finish']} loc={r['storage_location_id']} proxy={r['proxy_rows']} real={r['real_rows']}"
        )
    if c1 or c2:
        print(
            "  !! Class 1/2 need per-row split/flip judgment — this script does NOT auto-correct them."
        )
    print(f"\nClass 3 (fabricated pending real rows): {len(to_delete) + len(skipped)}")
    print(f"  WILL DELETE: {len(to_delete)}")
    for r in to_delete:
        print(
            f"    row {r.row_id} u{r.user_id} {r.card_name!r} x{r.quantity} ({r.finish}) from '{r.deck_name}' other_real={r.other_real_rows}"
        )
    print(f"  SKIP: {len(skipped)}")
    for r, why in skipped:
        print(f"    row {r.row_id} u{r.user_id} {r.card_name!r} from '{r.deck_name}' — {why}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sweep #136 Class 3 fabricated pending real rows.")
    ap.add_argument("--apply", action="store_true", help="write (default: preview only)")
    ap.add_argument(
        "--include-ambiguous", action="store_true", help="also delete other_real_rows>0 rows"
    )
    ap.add_argument("--exclude", default="", help="comma-separated inventory_row ids to opt out")
    args = ap.parse_args()

    exclude_ids = {int(x) for x in args.exclude.split(",") if x.strip()}
    session = SessionLocal()
    try:
        class3 = detect_class3(session)
        to_delete, skipped = select_for_deletion(
            class3, exclude_ids=exclude_ids, include_ambiguous=args.include_ambiguous
        )
        _print_preview(session, to_delete, skipped)
        if not args.apply:
            print("\n(dry run — pass --apply to delete the WILL DELETE rows.)")
            return
        n = apply_class3(session, to_delete)
        session.commit()
        print(f"\nAPPLIED: deleted {n} fabricated row(s); each recorded as '{CORRECTION_EVENT}'.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
