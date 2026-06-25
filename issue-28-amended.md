# Admin panel: last-active timestamp per user

## Summary

Add a `users.last_active_at` column that records the last time a user made an
authenticated request (not just the last login), updated via a throttled write
on the existing auth dependency, and display it on the admin user list.

## Relationship to the existing `last_signed_in_at` column

This feature is distinct from, and is added alongside, the existing
`User.last_signed_in_at` column (added v3.27.4, set on `POST /login`, shown on
the admin page as "Last Signed In").

- `last_signed_in_at` answers "when did they last authenticate."
- `last_active_at` answers "when did they last use the app." A persistent
  session that never re-logs in leaves `last_signed_in_at` stale while
  `last_active_at` keeps advancing. The gap between the two is the signal.

Keep both. Do NOT remove, rename, or repurpose `last_signed_in_at`, and do not
backfill `last_active_at` from it. Add a new adjacent admin column.

## What the fix should do

- Add `users.last_active_at` (nullable, plain `DateTime`) via Alembic migration.
  No backfill (NULL for existing rows until their next authenticated request),
  consistent with how `last_signed_in_at` was introduced.
- Stamp `last_active_at` from the authentication path on authenticated requests,
  throttled to at most one write per 5 minutes per user.
- Add a "Last Active" column to the admin user list next to "Last Signed In".

## Implementation constraints

These are decisions, not suggestions. They reflect how auth and time already
work in this codebase.

1. **Integration point: the dependency, not middleware.** Auth is resolved by
   the FastAPI dependency `get_current_user` in `app/dependencies.py`
   (session-cookie based, `request.session["user_id"]`). There is no auth
   middleware. Stamp `last_active_at` inside `get_current_user` (and
   `get_optional_current_user`, so landing-page activity counts), which already
   loads the `User` row. Do NOT add ASGI/HTTP middleware: it would fire on
   anonymous and static requests and re-resolve the session.

   Note: there is a second, legacy `get_current_user(request, db)` in
   `app/auth.py` that routes do NOT use. Do not edit that one.

2. **Naive UTC.** `app/timeutil.utc_now()` returns naive UTC
   (`datetime.utcnow()`); existing timestamp columns are plain `DateTime`, not
   `DateTime(timezone=True)`. Define `last_active_at` as plain `DateTime` and use
   `utc_now()` for both the write value and the throttle comparison. Do NOT use
   `datetime.now(UTC)` for the comparison: subtracting it from a naive stored
   value raises `TypeError: can't subtract offset-naive and offset-aware
   datetimes`. (The aware `datetime.now(UTC)` in `login_throttle.py` is fine
   there only because it never touches the DB.)

3. **Stateless DB self-throttle.** Throttle by comparing the stored value:
   write only when `last_active_at IS NULL` or
   `utc_now() - last_active_at >= 5 minutes`. Do NOT add an in-memory throttle
   store. The in-memory pattern in `login_throttle.py` /
   `password_reset_service.py` is justified only by single-pod deployment; the
   DB-self-throttle is correct under restarts and multiple replicas, and the row
   is already loaded.

4. **Isolated transaction for the write.** The auth dependency shares the
   request DB session (`get_db_session`). Do NOT commit the timestamp on that
   shared session: it can commit partial route state early or be rolled back by
   a route that later raises. Perform the update in its own short-lived session
   (follow the `_pending_count_for` precedent in `app/dependencies.py`, which
   opens its own `SessionLocal()`), or as an isolated `UPDATE users SET
   last_active_at = :now WHERE id = :id` committed independently.

5. **Admin display.** Add the column in `_build_user_rows`
   (`app/routes/admin.py`) and `admin.html`, rendered with the existing
   `format_local_datetime` filter (NULL-safe, returns '' so the template's
   `... or '—'` handles new users). Place "Last Active" adjacent to
   "Last Signed In".

## Acceptance criteria

- [ ] `users.last_active_at` column exists (nullable, plain `DateTime`), added by
      an Alembic migration; no backfill.
- [ ] `last_active_at` updates on an authenticated request via
      `get_current_user` / `get_optional_current_user`, using `utc_now()`.
- [ ] Throttling verified by test: with time injected/monkeypatched, a second
      authenticated request inside the 5-minute window does NOT write, and a
      request after the window DOES write. A test that only asserts the value
      gets set is insufficient.
- [ ] The timestamp write does not commit or roll back unrelated route state
      (isolated transaction).
- [ ] `last_signed_in_at` is unchanged (column, login write, and admin column
      all still present and behaving as before).
- [ ] Admin user list shows "Last Active" per user, NULL-safe for new users.
- [ ] pytest passes.

## Changelog

The admin panel now shows when each user was last active, in addition to when
they last signed in.
