"""`SELECT DISTINCT` + `ORDER BY <expression>` is a Postgres error.

    psycopg.errors.InvalidColumnReference:
    for SELECT DISTINCT, ORDER BY expressions must appear in select list

**SQLite accepts it**, so the whole SQLite suite stays green while the page
500s in production. It has now shipped or nearly shipped twice in two days:

* v4.13.27 put a relevance `CASE` into the audit card search's `ORDER BY`.
  That one reached prod and 500'd `/audit/api/card-search` for every user.
* v4.13.30 put `User.player_label_expr()` (a `coalesce`) into
  `co_members_of`'s `ORDER BY`. Caught by the Postgres gate before shipping —
  41 tests, every people-picker surface.

Both had the same shape and the same cure: the join existed only to prove
membership/ownership, its duplicate rows were the only reason for the
`DISTINCT`, and an `EXISTS` removes both. **Prefer EXISTS over join+DISTINCT
whenever the join is a predicate rather than a source of columns.**

This scan is a cheap early warning, not a replacement for running the suite
against Postgres — it reads source text, so it cannot see a `DISTINCT` and an
`ORDER BY` composed across function boundaries. `TEST_DATABASE_URL=… pytest`
remains the real gate.
"""

from __future__ import annotations

import pathlib
import re

APP = pathlib.Path("app")

# A chained query expression: `.distinct()` and a later `.order_by(...)` whose
# argument contains a call — i.e. an expression, not a bare column.
_CHAIN = re.compile(
    r"\.distinct\(\)(?P<between>(?:\s*\.\w+\([^()]*(?:\([^()]*\)[^()]*)*\))*?)"
    r"\s*\.order_by\((?P<args>[^()]*(?:\([^()]*\)[^()]*)*)\)",
    re.S,
)
# A bare column reference like `User.name` or `User.name.asc()` is fine; a call
# such as `func.coalesce(...)`, `case(...)` or `X.player_label_expr()` is not.
_EXPRESSION = re.compile(r"(func\.\w+|case)\s*\(|_expr\s*\(")

# Sites that legitimately order a DISTINCT query by an expression AND include
# that expression in the select list. Empty on purpose: there are none today,
# and adding one should be a deliberate act with a note here.
ALLOWLIST: set[str] = set()


def _offenders() -> list[str]:
    out = []
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text()
        for m in _CHAIN.finditer(text):
            if not _EXPRESSION.search(m.group("args")):
                continue
            line = text[: m.start()].count("\n") + 1
            ref = f"{path}:{line}"
            if ref not in ALLOWLIST:
                out.append(f"{ref} — ORDER BY {m.group('args').strip()[:60]}")
    return out


def test_the_pattern_matches_a_known_bad_shape():
    """Self-check: a guard that silently stops matching looks like a clean repo."""
    bad = """
    stmt = (
        select(User)
        .join(Member, User.id == Member.user_id)
        .distinct()
        .order_by(func.coalesce(User.real_name, User.username))
    )
    """
    m = _CHAIN.search(bad)
    assert m, "the chain regex no longer matches the shape it exists for"
    assert _EXPRESSION.search(m.group("args"))


def test_the_pattern_does_not_flag_a_plain_column_order():
    ok = ".distinct()\n        .order_by(User.display_name, User.username)\n"
    m = _CHAIN.search(ok)
    assert m and not _EXPRESSION.search(m.group("args"))


def test_no_query_orders_a_distinct_by_an_expression():
    offenders = _offenders()
    assert not offenders, (
        "SELECT DISTINCT + ORDER BY <expression> is invalid on Postgres and "
        "silently fine on SQLite:\n  "
        + "\n  ".join(offenders)
        + "\nPrefer EXISTS over join+DISTINCT when the join is only a predicate."
    )
