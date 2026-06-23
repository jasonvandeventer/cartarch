# Firefox Intermittent Session-Logout — Investigation

**Date:** 2026-06-22
**Status:** 🔎 **OPEN — root cause NOT confirmed; no code fix applied (would be a guess).** This note records what the code review found and the diagnostic steps needed to confirm a cause before changing anything.
**Symptom:** Users on Firefox occasionally get bounced to `/login` mid-session — they navigate to a page and are suddenly logged out. Intermittent. Not reproduced on Chrome.

## How the logout actually fires

The session is a **stateless, signed cookie** (Starlette `SessionMiddleware`). The whole session dict (`user_id` + `csrf_token`) lives in the cookie; the server holds no session store. `get_current_user` (`app/dependencies.py`) redirects `303 -> /login` whenever `request.session.get("user_id")` is falsy. So the logout is produced by **anything that makes the session read as empty** — the cookie itself is never explicitly deleted mid-session by the app. A cookie reads as empty when:

1. its itsdangerous signature fails to verify (wrong / mismatched `SESSION_SECRET_KEY`), or
2. the signature timestamp is older than `max_age` (14-day Starlette default), or
3. the cookie isn't sent for the host being navigated (host-only cookie, no `Domain`).

## Current session/cookie configuration (`app/main.py`)

```python
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "dev-only-change-me"),
    same_site="lax",
    https_only=os.getenv("DEV_MODE", "false").lower() != "true",
)
```

Resolved attributes (Starlette defaults fill the rest): name `session`, `Max-Age` 14 days (default — not set here), `SameSite=Lax`, `Secure=True` in prod, `HttpOnly` (hard-coded), **no `Domain`** (host-only), `Path=/`.

## What was RULED OUT

- **CSRF rotation — NOT the cause.** `get_csrf_token` (`app/dependencies.py`) mints the token once (`if "csrf_token" not in request.session`) and **never rotates** it — not per-request, not per-POST, not on login. The premise that "Firefox handles CSRF rotation differently" does not apply; there is no rotation. (Note: rendering a page *does* write `csrf_token` into the session on first contact, so a fresh `Set-Cookie` is re-emitted each render — this slides the 14-day window forward on every page view, so item (2) above is unlikely to be the mid-session trigger for active users.)
- **Cookie size / chunking** — session holds only `user_id` + a 64-char token; far under the 4 KB limit.
- **14-day expiry** — slides forward on every render; an *active* user won't hit it mid-session.

## Leading hypotheses (ranked)

### H1 — `SESSION_SECRET_KEY` mismatch across the two clusters (MOST LIKELY)

The platform is mid blue-green migration. Both clusters run the app, each with its own **per-cluster sealed secret**:
- blue/k3s: `vanfreckle-platform/k8s/apps/mana-archive/base/deployment.yaml`
- green/Talos: `vanfreckle-platform/clusters/talos/manifests/mana-archive/deployment.yaml`

A signed cookie minted by cluster A is **garbage to cluster B unless both share the exact same `SESSION_SECRET_KEY` plaintext**. SealedSecret ciphertext differs per cluster by design (different sealing cert), so you **cannot** compare the sealed YAML — you must compare the decrypted values. If the Cloudflare tunnel fans some requests to blue and some to green during cutover, a user oscillates between "valid" and "empty" sessions → exactly this intermittent logout. **"Firefox-only" is most plausibly observation bias** (connection-reuse / happy-eyeballs differences sending Firefox to a different backend), not a cookie-attribute bug.

**This is deployment-level, NOT a cartarch code bug** — hence no app change here.

**Confirm:** decrypt both clusters' `SESSION_SECRET_KEY` and diff them; and check whether the tunnel currently serves both clusters concurrently (recent platform commits: "Phase D prep: NodePort 30080 for tunnel re-route"). If they differ and both take traffic, that is the bug. Fix = seal the *same* plaintext key into both clusters (or route 100% to one).

### H2 — Split-brain with the legacy Unraid Docker container

Project memory records a stale legacy Mana Archive container on Unraid (`:8501`) as a "split-brain foot-gun," and that `cloudflared` runs on Unraid. If any fraction of traffic still lands on the legacy container (different/no `SESSION_SECRET_KEY`, possibly different cookie name/attrs), the same oscillating-empty-session logout results. **Confirm:** verify the tunnel ingress maps `cartarch.com` to exactly one live backend and the legacy container is not in rotation.

### H3 — Multiple replicas with a per-pod default secret

If `SESSION_SECRET_KEY` were ever unset on a replica, the app would (in prod) refuse to boot — but confirm no path injects the `dev-only-change-me` default and that all pods of a Deployment mount the *same* Secret. Single-worker per pod is confirmed (no `--workers` in the Dockerfile), so within one correctly-configured Deployment this is unlikely; it matters only across clusters/containers (H1/H2).

## Latent hardening found (correct, but NOT the confirmed fix — do separately, not under a guess)

These are genuine gaps surfaced during the review. None is proven to cause the logout, so they are **not** being applied as part of this bugfix:

- **uvicorn lacks `--proxy-headers` / `--forwarded-allow-ips`** (`Dockerfile`). The app is blind to the real request scheme; `Secure` is set from `DEV_MODE`, not `X-Forwarded-Proto`. Works today by luck of the env var. Worth adding for correctness, but it does not change cookie validity, so it won't fix the logout.
- **No explicit `Domain` on the session cookie** (host-only). Fine as long as users only ever hit one exact host; becomes a bug if `www.` vs apex or a subdomain is ever introduced. Setting a `Domain` is itself risky (can strand existing cookies) — do not change blind.

## Recommended next step

Do **not** ship a cookie-attribute change speculatively. First capture evidence:
1. In Firefox DevTools (a user who can reproduce): watch Network → the request that 303s to `/login`. Inspect the `Cookie` sent and any `Set-Cookie` on the prior response. Empty/garbled `session` cookie → signature/host problem (H1–H3). Missing cookie entirely → host/`Domain`/Secure problem.
2. Decrypt and diff `SESSION_SECRET_KEY` across blue + green (+ legacy Unraid). This single check most likely settles it.
3. Confirm the Cloudflare tunnel's current backend(s) for `cartarch.com` during the cutover window.

Once a cause is confirmed, the fix is almost certainly **platform-side** (key parity / single backend), not a cartarch app change.
