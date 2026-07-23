"""Persistent local storage — atomic JSON files, one deckbook per directory.

Three files per deckbook (Section 10 / revised Section structure):
  deckbook.json   — identity + edition (rarely changes)
  decisions.json  — the card list + per-card decision/acquisition state
  revisions.json  — append-only decision-change log

Writes are atomic (temp file + os.replace) so a crash mid-write can't leave a
truncated, valid-looking JSON file (Section 22 file-safety). Paths are resolved
strictly under the configured data dir — a deckbook_id can't escape it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from deckbooks.config import DATA_DIR, deckbook_dir


def _safe_dir(deckbook_id: str) -> Path:
    """Resolve the deckbook dir and confirm it stays under DATA_DIR (no
    ../ escape from an untrusted id)."""
    root = DATA_DIR.resolve()
    target = deckbook_dir(deckbook_id).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"deckbook_id {deckbook_id!r} escapes the data directory")
    return target


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt file shouldn't crash the app — surface the default and let
        # the caller/UI show a repair path (Section 21 graceful degradation).
        return default


def _write_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_deckbook(deckbook_id: str) -> dict:
    return _read(_safe_dir(deckbook_id) / "deckbook.json", {})


def save_deckbook(deckbook_id: str, data: dict) -> None:
    _write_atomic(_safe_dir(deckbook_id) / "deckbook.json", data)


def load_cards(deckbook_id: str) -> list[dict]:
    return _read(_safe_dir(deckbook_id) / "decisions.json", [])


def save_cards(deckbook_id: str, cards: list[dict]) -> None:
    _write_atomic(_safe_dir(deckbook_id) / "decisions.json", cards)


def load_revisions(deckbook_id: str) -> list[dict]:
    return _read(_safe_dir(deckbook_id) / "revisions.json", [])


def append_revision(deckbook_id: str, revision: dict) -> None:
    revs = load_revisions(deckbook_id)
    revs.append(revision)
    _write_atomic(_safe_dir(deckbook_id) / "revisions.json", revs)


def exists(deckbook_id: str) -> bool:
    return (_safe_dir(deckbook_id) / "deckbook.json").exists()
