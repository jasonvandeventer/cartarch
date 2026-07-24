"""Request-scoped 'current deckbook' — so the read/write services don't each
need a deckbook_id threaded through every call.

A ContextVar is per-request-safe: each sync endpoint runs in its own copied
context (Starlette's threadpool), so a route setting the book can't bleed into
another request. Scripts/tests that don't set it get the default book.
"""

from __future__ import annotations

from contextvars import ContextVar

from deckbooks.config import DECKBOOK_ID

_current_book: ContextVar[str] = ContextVar("deckbook_id", default=DECKBOOK_ID)


def get_book() -> str:
    return _current_book.get()


def use_book(deckbook_id: str) -> None:
    _current_book.set(deckbook_id)
