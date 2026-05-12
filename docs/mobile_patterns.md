# Mana Archive — Mobile UI Patterns

Living reference for mobile-friendly UI primitives. Add patterns here as they
get adopted across the app; don't reinvent them per-page.

For the original audit (which drove the v3.16.3 mobile sweep), see
[mobile_audit.md](mobile_audit.md).

Breakpoint conventions are documented in [app/static/style.css](../app/static/style.css)
at the top of the file:

- **`--bp-tablet` (768px)** — main mobile/desktop divide. Below: mobile layout.
- **`--bp-phone` (480px)** — narrow phones; extra-aggressive collapses.
- **`--bp-desktop-narrow` (980px)** — laptop / desktop-narrow.

---

## Popovers

The app has two flavors of small floating UI: **inline absolute popovers**
(desktop-friendly, anchored to a trigger), and the **centered modal popover**
(mobile-friendly, full-screen-ish with a backdrop). The same DOM element
serves both — opt into the dual behavior by adding the `.mobile-popover` class
alongside its existing presentation class (`.edit-popout`, `.bracket-popout`,
etc.).

### When to use absolute popovers (desktop-only)

Use a plain `.edit-popout` / `.bracket-popout` / similar **without**
`.mobile-popover` only when:

- The popover is **never reachable on mobile** (e.g. an admin-only desktop tool
  page).
- Or it's part of the **landscape-only game tracker** (`game_detail.html`)
  where the layout assumes ≥1024px and tablet landscape orientation.

For anything else on a page a mobile user might land on, add `.mobile-popover`.

### When to use `.mobile-popover` (default for everything else)

Any popover that's anchored to a button or table cell on a page mobile users
visit. That covers basically every Edit popout, every bracket popout, the
import-wizard "Create new deck/location" popouts, image previews, etc.

Behavior summary:

| Above 768px | Below 768px |
|---|---|
| Element keeps its existing `position: absolute` (or whatever the desktop CSS sets). | `position: fixed`, centered via `top: 50%; left: 50%; transform: translate(-50%, -50%)`. |
| No backdrop. | Semi-transparent backdrop (`rgba(0,0,0,0.4)`) injected behind it. |
| No close button. | × button auto-injected top-right (44×44 tap target). |
| Width unconstrained beyond what desktop CSS already sets. | `width: min(90vw, 400px)`. |
| Page scrolls normally. | `body` scroll locked while open. |
| Closes via the trigger only. | Closes on backdrop tap, Escape key, ×-button tap, or trigger tap. |

### How to add a new popover

For a `<details>`-based popover (the standard pattern in this codebase):

```jinja
<details class="inline-details">
  <summary class="btn-like">Edit</summary>
  <div class="edit-popout mobile-popover">
    {# ... form fields ... #}
  </div>
</details>
```

That's it. No JS to write — [base.html](../app/templates/base.html) wires
the toggle behavior on `DOMContentLoaded` by finding every `.mobile-popover`
inside a `<details>`, binding a `toggle` listener directly to the parent
`<details>`, and managing backdrop / body-lock / close-button injection from
there. Each `<details>` is tagged `data-mobile-popover-wired="1"` to prevent
double-binding on re-scans.

#### Dynamically-injected popovers

If you load a popover after `DOMContentLoaded` (e.g. via `fetch()` →
`replaceWith`), dispatch the scan event so the new element gets wired:

```js
document.dispatchEvent(new Event('mobilepopover:scan'));
```

[deck_detail.html](../app/templates/deck_detail.html) does this after the
lazy `/decks/{id}/panels` fragment is injected — the bracket popover lives in
that fragment.

#### Non-`<details>` popovers

If you build a popover that isn't wrapped in `<details>` (e.g. you toggle
visibility via a button + JS class), use the imperative API exposed on `window`:

```js
window.MobilePopover.open(el);   // attach backdrop, lock body, inject × — mobile only
window.MobilePopover.close(el);  // detach everything
window.MobilePopover.isMobile(); // -> bool, viewport ≤ 768px
```

The element still needs the `.mobile-popover` class for the CSS to apply. The
imperative API exists today but isn't used by any live template — every
in-codebase popover is `<details>`-based. Use it only when you have a real
reason not to wrap the popover in `<details>`.

### CSS contract

The `.mobile-popover` class only kicks in below 768px. Above that breakpoint
it's inert — your existing `.edit-popout` (or sibling) CSS controls everything.

Below 768px the class overrides positioning with `!important` so it wins over
the more-specific desktop selectors like `.inline-details .edit-popout` and
`.table-wrap .edit-popout`. The `!important` is intentional and scoped to the
media query — don't try to fight it with even-more-specific selectors.

### Avoid

- **Don't set `min-width` so large it exceeds `90vw`.** The class caps
  `width` at `min(90vw, 400px)`, but a `min-width: 500px` will still
  blow the layout out. The inline-create popovers in `_inline_create_destination.html`
  use `min-width: 260px` which is safe even on iPhone SE (320px).
- **Don't put a `.mobile-popover` inside a `transform`-ed ancestor.**
  `position: fixed` becomes relative to the nearest transformed ancestor, not
  the viewport. None of the current popover call sites hit this, but if you
  add one inside a card that uses `transform: translate(...)` for animation,
  the popover will be anchored to that card instead of centered on screen.
- **Don't nest two `.mobile-popover` elements.** The JS tracks an `openSet`
  of all currently-open popovers and only releases body scroll lock when the
  set is empty. Nesting works mechanically (Escape closes them all), but the
  UX of stacked modals is bad.

---

## Cache-busting static assets

`static_v(path)` in [app/dependencies.py](../app/dependencies.py) returns the
file's mtime as a cache-buster query parameter:

```jinja
<link rel="stylesheet" href="/static/style.css?v={{ static_v('style.css') }}">
```

Use this for any static asset where browser cache invalidation matters during
development (CSS, JS, large images). `dev-{git-sha}` doesn't change for
working-tree edits, which means phones serve stale assets until you commit.
The mtime approach busts on every edit.

For deploys the same mechanism works fine — the file mtime changes when the
container is rebuilt, so cache busts cleanly there too.

---

## Tap targets

44×44 px floor applies to all buttons via [app/static/style.css](../app/static/style.css)
at the bottom of the file (`body:not(.game-mode) button:not(...)`). The
`not(.game-mode)` exemption preserves the game tracker's denser tap targets
(20-30px buttons) for landscape tablet use; everywhere else, buttons get a
`min-height: 44px`.

Don't manually set `min-height` smaller than 44px on a button outside the
game tracker. If you find a button that's getting compressed by a flex
parent, fix the parent — don't override the floor.

---

## Bottom navigation

Below 768px the top-bar nav hides and a 5-tab bottom nav takes over
(Home / Collection / Decks / Games / More). All secondary destinations
(Import, Pending, Locations, Tokens, Sets, Drawers, Audit, Admin, Account)
live behind "More" which opens a full-screen overlay.

Don't add a sixth bottom tab. The right place for new top-level destinations
is the More overlay. If you have a deep-linked secondary page that mobile
users hit often, surface a tile or shortcut on the relevant primary page
(Home, Collection, Decks) — not as a tab.

For path-based "active" indication, the tab uses Jinja path-prefix matching
against `request.url.path`. The More tab is active when the path matches any
secondary destination. See [base.html](../app/templates/base.html).
