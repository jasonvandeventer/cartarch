# Escalation: Firefox-for-iOS cannot stay signed in (session cookie not persisted)

**App:** Cartarch (FastAPI + Starlette + Jinja2 + PostgreSQL), in-repo name `mana-archive`.
**Date:** 2026-06-29. **Affected build:** v4.1.8 (current prod).
**Severity:** A real user cannot sign in on **Firefox for iOS**. Other browsers are unaffected.

---

## 1. One-paragraph summary

After a CSRF change (#63), sign-in on **Firefox for iOS (FxiOS 152.1.1, iPhone, iOS 18.7)**
behaves like this: the login `POST` **succeeds** (HTTP 303, the server sets the session and
emits `Set-Cookie`), but the browser **never stores/returns the session cookie**, so the very
next request (`GET /`) is anonymous and the user bounces back to the splash page — an endless
"login → splash → login" loop. The identical flow works on every other browser tested (and a
concurrent desktop user is using the app authenticated the whole time). Server-side cookie
configuration changes (`SameSite=Lax` → `SameSite=None; Secure`) did **not** fix it, and the
user disabling Firefox-iOS Tracking Protection did **not** fix it.

---

## 2. Stack / deployment relevant to cookies

- **App server:** uvicorn, **run WITHOUT `--proxy-headers`**, behind a **Cloudflare Tunnel
  (cloudflared)**. TLS terminates at Cloudflare; cloudflared forwards plain **HTTP** to the pod.
  Consequence: `request.url.scheme` inside the app is `"http"`, while the public URL is
  `https://cartarch.com`. (This is a known separate issue, GitHub #66.)
- **Sessions:** Starlette `SessionMiddleware` (signed-cookie sessions; the session dict is
  serialized into the `session` cookie). Config in `app/main.py`:
  ```python
  _session_https_only = os.getenv("DEV_MODE","false").lower() != "true"   # True in prod
  app.add_middleware(
      SessionMiddleware,
      secret_key=...,
      same_site="none" if _session_https_only else "lax",   # currently None in prod (v4.1.8)
      https_only=_session_https_only,                        # True in prod -> Secure
  )
  ```
  **Set-Cookie the server emits in prod** (Starlette format): `session=<signed>; Path=/; HttpOnly;
  SameSite=none; Secure` — host-only (no `Domain`). Verified valid: all other browsers accept it.
- **Public host:** `cartarch.com` (also `www.cartarch.com`, `mana.vanfreckle.com` via the tunnel).
- **Login flow:** `GET /login` (form) → `POST /login` (credentials + a CSRF token) → on success
  `request.session["user_id"] = user.id` then `RedirectResponse("/", status_code=303)` →
  `GET /` renders the dashboard if authenticated, else the marketing splash.

---

## 3. The diagnostic instrumentation (build v4.1.7, still live)

A log-only helper `log_auth_diagnostic(request, where)` runs at two seams and logs **derived**
classifications (never raw cookie/token/header values):
- `where=login_post` — at the start of `POST /login`.
- `where=home_unauth` — in `GET /` when the request resolved **no** authenticated user (the bounce).

Fields:
- `session_cookie_present` / `cookie_class` — was a `session` cookie sent, and does it decode?
  (`absent` = no `session` cookie on the request at all.)
- `user_id_present` — did the session resolve a logged-in user?
- `origin_present` / `origin_match` — was an `Origin` header sent, and does its host match the
  request host (scheme anchored to https)? `match=None` = header absent or unparsable.
- `referer_present` / `referer_match` — same for `Referer`.

---

## 4. RAW LOGS — Firefox-iOS sign-in attempts (most recent first; kube-probe noise removed)

Client UA on every line:
`Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/152.1.1 Mobile/15E148 Safari/604.1`
Client IP (Cloudflare-forwarded): `2600:1700:ae01:85f:4c45:1fcf:fed7:16ed`

```
# A login attempt (POST /login arrives):
auth_diag where=login_post  session_cookie_present=False cookie_class=absent user_id_present=False \
          origin_present=True  origin_match=False  referer_present=True  referer_match=True

# Immediately after (the 303 -> GET / bounce):
auth_diag where=home_unauth session_cookie_present=False cookie_class=absent user_id_present=False \
          origin_present=False origin_match=None   referer_present=True  referer_match=True
```

This pattern is **100% reproducible** across ~6 attempts spanning three builds (v4.1.6 `SameSite=Lax`
era, v4.1.7, v4.1.8 `SameSite=None`), and persisted after the user **disabled Tracking Protection**.

### Key facts the logs establish
1. **The session cookie is NEVER present** — `cookie_class=absent` on *every* request, including the
   `GET /` immediately after a successful login `POST`. So the browser is not storing and/or not
   returning the `Set-Cookie` from the login 303. (The server *does* emit it — confirmed working on
   other browsers.)
2. **`Origin: ` is sent but does not match** on the `POST` (`origin_present=True, origin_match=False`),
   while **`Referer` matches** (`referer_present=True, referer_match=True`). The mismatched-but-present
   Origin with a valid Referer strongly implies Firefox-iOS is sending **`Origin: null`** (an *opaque*
   origin) for a same-site form POST.
3. The same-site `Referer` is correct (`referer_match=True`), so `request.url.netloc` IS the correct
   public host (`cartarch.com`) — the host is preserved through the tunnel. The problem is **not** a
   host/netloc mismatch.

---

## 5. Everything tried (chronological), and the result

| Build | Change | Result |
|------|--------|--------|
| v4.1.5 | #63: replaced session-based pre-auth CSRF with a **stateless signed-token** CSRF + an **Origin/Referer cross-site gate** comparing `Origin.host == request.url.netloc`. | **Broke login for everyone** (incident): behind cloudflared the gate logic mis-fired and returned 403 `Cross-site request blocked` on every `POST /login`. |
| v4.1.6 | Hotfix: made the cross-site gate **advisory** (accept + log; allowlist `cartarch.com`/`www`/`mana.vanfreckle.com` + request host; fail-open otherwise). Signed-token check still enforced. | Restored login for normal browsers. **Revealed** the FxiOS symptom: login 303s but bounces to splash. |
| v4.1.7 | Added the `log_auth_diagnostic` instrumentation (observability only). | Captured the data in §4 — proved `cookie_class=absent` on the post-login request. |
| v4.1.8 | Session cookie **`SameSite=Lax` → `SameSite=None; Secure`** in prod (hypothesis: FxiOS treats the same-site POST as cross-site and drops a Lax cookie). | **No change** — cookie still `absent` on FxiOS. Hypothesis killed. |
| (user) | Disabled Firefox-iOS **Tracking Protection** and retried. | **No change** — cookie still `absent`. |

### Ruled out
- **Host/netloc mismatch** — `Referer` matches; `request.url.netloc` is the correct public host.
- **`SameSite` attribute** — both `Lax` and `None; Secure` fail identically.
- **Tracking Protection** — disabling it changed nothing.
- **Our navigation / templates** — the sign-in link is relative (`<a href="/login">`), the form
  action is relative (`action="/login"`), and there is **no** `http://` absolute URL, CSP `sandbox`,
  or `Referrer-Policy` in the auth pages that would explain an opaque origin.
- **A general server bug** — the cookie is valid and persists on all other browsers; a concurrent
  desktop user stayed authenticated throughout.

### NOT yet tried (candidate next steps — see §7)

---

## 6. Leading hypothesis

Firefox-iOS is treating the (same-site) login form POST as having an **opaque origin** (`Origin:
null`) and, as a result, **refuses to store the `Set-Cookie`** returned on the 303 redirect (it
classifies it as a cross-site / third-party cookie write). Note FxiOS, like all iOS browsers, is
**WebKit** under the hood (`AppleWebKit/605.1.15`), so this is effectively iOS WebKit cookie/ITP
behavior, not Gecko. Why FxiOS produces an opaque origin here for a clean same-site `https` form
POST is the crux and is **not yet explained** — it may be a Firefox-iOS-specific quirk, an
interaction with the Cloudflare Tunnel's plain-HTTP origin leg, or an iOS cookie-storage policy on
cookies set during a redirect.

---

## 7. Open questions / suggested next steps for whoever picks this up

1. **Capture the RAW headers** (the instrumentation logs only derived values for privacy):
   - The exact `Origin` and `Referer` Firefox-iOS sends on `POST /login` (is `Origin` literally `null`?).
   - The exact `Set-Cookie` header on the 303 response (confirm attributes end-to-end through Cloudflare).
   - **Best tool:** Safari → Develop → [iPhone] → Web Inspector attached to the Firefox-iOS tab during
     login; inspect Network (the 303's `Set-Cookie`, Storage → Cookies, and any console warning about
     a rejected `Set-Cookie`). This is the authoritative way to see *why* WebKit drops the cookie.
2. **Does the cloudflared plain-HTTP origin leg matter?** uvicorn runs without `--proxy-headers`
   (issue #66). Test whether enabling `--proxy-headers` + `--forwarded-allow-ips` (so the app sees
   `https`) changes how the cookie/redirect is treated. The internal `http` scheme is a prime suspect
   for confusing cookie/origin handling on a redirect that sets a `Secure` cookie.
3. **Is the cookie dropped because it's set on a 303 redirect?** Test setting the session cookie on a
   `200` (e.g. land the user on an interstitial that sets the cookie, then JS-redirect) instead of on
   the 303, to isolate "redirect-set cookie" behavior.
4. **Does ANY cookie survive on this Firefox-iOS?** Set a throwaway first-party test cookie
   (`Lax`, no `Secure`, etc.) and check whether it round-trips — to distinguish "this specific cookie"
   from "this browser/config drops all server cookies in this flow."
5. **If WebKit genuinely won't persist a redirect-set session cookie here:** the durable fix is a
   **non-cookie session** — e.g. issue a session token, store it in `localStorage`, and send it as an
   `Authorization`/custom header on each request (requires a client-side fetch layer), or a
   short-lived token in the redirect URL exchanged for a session. This is a **significant
   rearchitecture**, not a config change.

---

## 8. Current production state (so the next person isn't surprised)

- **v4.1.8 is live.** Login works for all browsers **except** Firefox-iOS (and presumably other
  iOS WebKit privacy browsers, e.g. Opera Touch — the original #63 target population).
- The **`log_auth_diagnostic` instrumentation is still active** (logs a WARN on every anonymous
  `GET /` and every `POST /login`). It should be removed once this is resolved.
- The session cookie is currently **`SameSite=None; Secure`** — this provided **no benefit** for the
  FxiOS issue and slightly widens cookie scope; reverting to `Lax` is reasonable. (CSRF on
  authenticated mutations is independently protected by a session double-submit token.)
- The **pre-auth cross-site CSRF gate is "advisory"** (fail-open + log, from the v4.1.6 hotfix) — it
  does not currently *enforce* cross-site rejection. The intended proper re-tighten is **"accept if
  `Origin` matches OR `Referer` matches"** (the logs show FxiOS sends a valid `Referer` even when its
  `Origin` is opaque), which would restore enforcement without breaking FxiOS.

---

## 9. Relevant source (paths, all in the `cartarch` repo)

- `app/main.py` — `SessionMiddleware` config (§2); `GET /` (`home`) splash/dashboard branch + the
  `home_unauth` diagnostic call.
- `app/routes/auth.py` — `POST /login` (the `login_post` diagnostic call; success path:
  `session["user_id"]=...` + `RedirectResponse("/", 303)`).
- `app/dependencies.py` — `log_auth_diagnostic`, `require_preauth_csrf` (stateless CSRF guard),
  `_preauth_cross_site_ok` (the advisory cross-site gate), `mint_preauth_csrf_token`, and the
  `_classify_session_cookie` / `_classify_cross_site` classifiers the diagnostic reuses.
- GitHub issues for context: **#63** (this work — stateless pre-auth CSRF for privacy iOS browsers),
  **#66** (uvicorn without `--proxy-headers` behind cloudflared → wrong internal scheme).
