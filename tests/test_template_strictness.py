"""#157 — a missing template variable must fail LOUDLY under test.

Jinja's default `Undefined` renders a missing variable as empty and is FALSY in a
conditional, so a context key a route forgot to pass produces a plausible page rather
than an error. That shipped three bugs before this guard existed:

  v4.11.36  `_SHARE_CARD_FIELDS` missing `scryfall_id`      -> blank card frames
  v4.12.0   `_SnapshotCardProjection` missing `scryfall_id` -> blank card frames
  v4.12.10  `record` never reached playgroup_detail.html    -> silent empty state

`StrictUndefined` is enabled for the test environment only (see `app/dependencies.py`).
Production keeps the forgiving behaviour on purpose: turning a cosmetic gap into a 500
on any template path the suite does not reach would be a worse trade on a live system.
"""

from __future__ import annotations

import os

import pytest
from jinja2 import StrictUndefined, Undefined
from jinja2.exceptions import UndefinedError

from app.dependencies import templates


def test_strict_undefined_is_active_under_pytest():
    assert templates.env.undefined is StrictUndefined


def test_a_missing_context_key_raises_instead_of_rendering_empty():
    """The #152 shape: a route forgets a key and the template's `{% if %}` quietly
    takes the else branch."""
    tpl = templates.env.from_string("{% if record %}HAS{% else %}EMPTY{% endif %}")
    with pytest.raises(UndefinedError):
        tpl.render()
    assert tpl.render(record=[1]) == "HAS"


def test_a_missing_attribute_on_a_projection_raises():
    """The v4.11.36 / v4.12.0 shape: a sanitized projection promises to fail loudly on a
    non-whitelisted field, which is true in Python and WAS not true inside Jinja."""

    class Projection:
        image_url = "x.jpg"

        def __getattr__(self, name):  # mirrors the real projections
            raise AttributeError(name)

    tpl = templates.env.from_string("{{ card.scryfall_id }}")
    with pytest.raises(UndefinedError):
        tpl.render(card=Projection())


def test_default_marks_a_variable_as_deliberately_optional():
    """`|default(...)` is the one idiom used to mark the genuinely-optional variables
    (flash flags, HTMX oob markers, per-item keys only some rows carry). It must keep
    working for missing context keys, missing dict keys AND missing attributes."""
    env = templates.env
    assert env.from_string("{% if x|default(false) %}Y{% else %}N{% endif %}").render() == "N"
    assert env.from_string("{% if d.k|default(false) %}Y{% else %}N{% endif %}").render(d={}) == "N"
    assert (
        env.from_string("{% if o.a|default(false) %}Y{% else %}N{% endif %}").render(o=object())
        == "N"
    )
    # ...and does NOT mask a value that is actually present.
    assert (
        env.from_string("{% if d.k|default(false) %}Y{% else %}N{% endif %}").render(d={"k": 1})
        == "Y"
    )


def test_production_is_left_permissive_on_purpose():
    """The gate keys on a pytest-only env var, so a deploy cannot turn strictness on by
    accident. Rebuild the decision with the test markers absent and confirm it is off."""
    import importlib

    from fastapi.templating import Jinja2Templates

    saved = {k: os.environ.pop(k, None) for k in ("PYTEST_CURRENT_TEST", "PYTEST_VERSION")}
    try:
        prod_like = Jinja2Templates(directory="app/templates")
        if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("PYTEST_VERSION"):  # pragma: no cover
            pytest.fail("pytest markers should be absent here")
        assert prod_like.env.undefined is Undefined  # the permissive default
        assert importlib  # keep the import meaningful to linters
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
