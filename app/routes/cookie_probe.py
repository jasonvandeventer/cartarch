"""TEMPORARY cookie-storage probe (#63 / Firefox-iOS investigation, v4.1.10).

Isolates WHICH cookie property Firefox-iOS rejects. Sets the same matrix of test
cookies three ways — on a plain 200, on a 303 redirect, and on a POST->303 (the
shape the Starlette session cookie uses) — under five attribute combos, then a
check route reports (and logs) which came back.

No auth, no secrets, no `request.session` use (so it never sets the real session
cookie — only `ct_*` test cookies). DELETE after the investigation: remove this
file and its `include_router` line in app/main.py.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# (suffix, secure, httponly, samesite)  — samesite None => attribute omitted
_VARIANTS = [
    ("sec_ho_lax", True, True, "lax"),  # mirrors the real session cookie
    ("sec_lax", True, False, "lax"),  # drop HttpOnly
    ("plain_lax", False, False, "lax"),  # drop Secure + HttpOnly
    ("sec_ho_none", True, True, "none"),  # SameSite=None
    ("bare", False, False, None),  # no SameSite, no Secure at all
]


def _set(resp, method: str) -> None:
    for suffix, secure, httponly, samesite in _VARIANTS:
        kw = {"value": "1", "path": "/", "max_age": 900, "secure": secure, "httponly": httponly}
        if samesite:
            kw["samesite"] = samesite
        resp.set_cookie(f"ct_{method}_{suffix}", **kw)


_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<h2>Cartarch cookie probe</h2>
<p>This 200 response set the <b>200</b> cookies. Tap each, in order:</p>
<ol>
<li><a href="/cookie-test/check">Check now (200-set)</a></li>
<li><a href="/cookie-test/set-303">Set via 303 redirect &rarr; check</a></li>
<li><form method="post" action="/cookie-test/set-post" style="display:inline">
    <button type="submit">Set via POST&rarr;303 &rarr; check</button></form></li>
<li><a href="/cookie-test/check">Final check (everything that stuck)</a></li>
</ol>"""


@router.get("/cookie-test")
def cookie_test_landing():
    resp = HTMLResponse(_PAGE)
    _set(resp, "200")
    return resp


@router.get("/cookie-test/set-303")
def cookie_test_set_303():
    resp = RedirectResponse("/cookie-test/check", status_code=303)
    _set(resp, "303")
    return resp


@router.post("/cookie-test/set-post")
def cookie_test_set_post():
    resp = RedirectResponse("/cookie-test/check", status_code=303)
    _set(resp, "post")
    return resp


@router.get("/cookie-test/check")
def cookie_test_check(request: Request):
    returned = sorted(n for n in request.cookies if n.startswith("ct_"))
    logger.warning(
        "cookie_probe_check ua=%r returned=%s",
        request.headers.get("user-agent"),
        returned,
    )
    rows = "".join(f"<li>{n}</li>" for n in returned) or "<li><i>(none came back)</i></li>"
    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<h2>Cookies returned</h2><ul>{rows}</ul><p><a href='/cookie-test'>back</a></p>"
    )
