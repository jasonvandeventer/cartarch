# Mana Archive — Mobile Responsiveness Audit

> **Note:** this is the original audit document (pre-v3.16.3). For the
> living reference covering current mobile UI primitives — popover pattern,
> tap targets, bottom-nav rules, cache-busting — see
> [mobile_patterns.md](mobile_patterns.md).

**Audit scope:** read-only review of `app/templates/*.html` (10 priority templates) and `app/static/style.css` (2966 lines). Target viewports: 375px (iPhone SE / mini), 390px (typical phone), 768px (tablet portrait). Tap-target target: 44×44 px (Apple HIG) / 48×48 (Android M3).

**TL;DR:** the desktop UI works, but the app is materially desktop-first. The two biggest pain points are (1) the global nav — 9–12 unwrapped text links that wrap to 3+ rows on a phone — and (2) `inventory-card` and `pending-item`, which only collapse to single-column at 720px and don't reduce thumb size below ~170px, eating most of the viewport on a 375px screen. There are also six unwrapped `<table>` elements with no `.table-wrap` that will horizontal-scroll the entire page (instead of just the table) on a phone. Existing media queries are inconsistent (980 / 760 / 720) and only cover ~15% of components.

---

## 1. base.html — header, nav, overall shell

### Layout
- `.header-shell` and `.page-shell` use `width: min(96vw, 1800px)` with `padding-left/right: 1rem` — content area sizing is fine on mobile.
- `.header-shell` is `display: flex; justify-content: space-between` between `.header-left` (wordmark + nav) and `.header-right` (version pill + logout button). At `max-width: 720px` it flex-wraps and `.header-right` becomes full-width row-aligned (style.css:532–540). OK in principle, but everything else inside the header is uncontrolled.

### Issues

**Issue 1.1 — Nav has 9–12 top-level links and no mobile pattern.**
[base.html:24-40](app/templates/base.html#L24-L40) renders these `<a>` items inside `<nav>`: Home, Import, Collection, Pending, Locations, (Drawers), Decks, Tokens, Games, (Audit), Sets, (Admin), Account. Admins with drawer-sorter privilege see 12 items.
`nav { display: flex; flex-wrap: wrap; gap: 1rem; }` (style.css:132-137) makes the links wrap into 3+ rows on a 375px viewport, stacking ~120px of header above the page hero before content starts. There is no hamburger, no overflow menu, no bottom-tab pattern. Tap targets are bare text links with default `padding` (none) — they fail the 44px target both vertically (~22px from line-height) and rely on horizontal whitespace.
**Severity: blocker.**

**Issue 1.2 — Wordmark image is 70px tall on mobile.**
`.brand-wordmark { height: 70px }` (style.css:93-97). Combined with `nav` wrapping (12 items × ~24px line-height ≈ 3 wrapped rows of 24-32px each), the sticky `.site-header` consumes ~200-260px of vertical space on a phone before the user ever sees content. There is no mobile override.
**Severity: high.**

**Issue 1.3 — Logout button label is verbose.**
[base.html:46-49](app/templates/base.html#L46-L49) renders `Logout ({{ display_name or username }})`. With an email-style username (`jason@vanfreckle.com`), this single button is ~28+ characters wide and forces `.header-right` to wrap awkwardly. No truncation.
**Severity: medium.**

**Issue 1.4 — Sticky header is fine but combined height eats viewport.**
`.site-header { position: sticky; top: 0 }` (style.css:29-35) is sensible, but combined with 1.1+1.2 it occupies ~30% of an iPhone SE screen.
**Severity: derives from 1.1 and 1.2.**

**Issue 1.5 — Active page is not indicated.**
Nav links have no aria-current and no `.active` class. On mobile this matters more because the nav isn't always visible.
**Severity: low.**

---

## 2. home.html

### Layout
- `.feature-grid` uses `grid-template-columns: repeat(auto-fit, minmax(220px, 1fr))` (style.css:571-575) — collapses naturally to a single column at <440px viewport. Good.
- Each `.feature-card` is `display: block` with comfortable padding. Tap target is the whole card, which is large.

### Issues

**Issue 2.1 — No issues blocking adoption on this page.**
The cards are large, hit-target-friendly, and reflow correctly. The only concern is that there are 5-6 cards stacked vertically below an already-tall hero — but that's content, not layout.

**Issue 2.2 — `.feature-card:hover { transform: translateY(-2px) }` (style.css:596) is a desktop tell.**
The hover effect doesn't fire on touch but isn't harmful. Note that the cards have no `:active` style for tap feedback.
**Severity: low (polish).**

---

## 3. collection.html

### Layout
- Hero stat row uses `.stat-grid` with `repeat(auto-fit, minmax(180px, 1fr))` (style.css:251-255) — fine.
- Filter bar (`.filter-row`, style.css:240-249): `min-width: 140px` on every input/select. 6 controls (search, finish, location, sort, direction, Apply, Clear, Export) × 140px = 840px laid out. Wraps onto multiple rows on mobile.
- Card grid: `.inventory-grid { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)) }` (style.css:321-325) — single column at <340px viewport. The `.inventory-card` template is `grid-template-columns: 150px 1fr` (thumb + body, style.css:336-340), which at 720px reduces thumb to 110px (style.css:508-513), and at 720px collapses to single-column-stacked (thumb above body, style.css:546-554) with the thumb growing to 170px wide.

### Issues

**Issue 3.1 — Filter bar has 6 controls with `min-width: 140px` each.**
On a 343px content area, that's at most 2 controls per row with the search input usually getting only one row to itself. Total filter height on a phone is ~6 stacked rows. The search input is wider than the others (no max-width set) and benefits, but the rest of the controls collide.
**Severity: medium.**

**Issue 3.2 — Search input placeholder is huge and unhelpful on mobile.**
[collection.html:84](app/templates/collection.html#L84) sets `placeholder="t:rat -t:pirate · t:creature OR t:artifact · (t:instant OR t:sorcery) · id:gb · is:foil · qty:>1 · price:>=5"`. The placeholder is ~120 characters and gets truncated at the field width, so on a phone the user sees "t:rat -t:pirate · t:cre…". This doesn't help and looks unprofessional.
**Severity: medium.**

**Issue 3.3 — Pagination block has 3 sibling flex children with `justify-content: space-between`.**
[collection.html:136-188](app/templates/collection.html#L136-L188) renders `Page X of Y` + prev/next/page buttons + a "Go to page" form, all in a `.hero-row` with `flex-wrap: wrap`. Page-number buttons can produce 5-10 small buttons (`{{ p }}`). Default `<button>` padding is `0.58rem 0.9rem` (style.css:194-202); the resulting button is ~32-36px tall — **below the 44px tap-target threshold**. The `…` separators between page numbers are unstyled and look like accidental dots on mobile.
**Severity: high (tap targets).**

**Issue 3.4 — Inventory-card per-card action drawer is wide.**
The `.card-actions-drawer` (style.css:795-824) contains 3+ inline-forms (Remove, Add to Deck, Move) plus a row of 4 buttons (Sell, Trade, Delete, Refresh). The quantity input has `style="width: 64px"` (inline, _macros.html:102) — fine — but the Add-to-Deck and Move dropdowns have no width control. On a 320px-wide column they collide with the buttons. The Delete Row button uses `confirm()` which works on mobile but the button label is verbose.
**Severity: medium.**

**Issue 3.5 — Inline `style="width: 80px"` on page-jump input is OK but adjacent number-input + button is a 2-control row on its own.**
[collection.html:180-187](app/templates/collection.html#L180-L187) — fine, just notes that "Go" button is small.
**Severity: low.**

**Issue 3.6 — Onboarding block uses `style="display:flex; gap:1rem; flex-wrap:wrap"` for 3 buttons.**
[collection.html:11-31](app/templates/collection.html#L11-L31) — works but is inline-styled rather than using a named class. Cosmetic.
**Severity: low.**

---

## 4. deck_detail.html

This is the most complex page in the app. It has the deck hero, bracket-v2 panel, analytics panel, health panel, search-collection panel, commanders grid, deck-cards filter+grid, tokens-needed table, bulk-move panel, and lazy-loaded combos/synergy/upgrade panels.

### Layout
- Hero uses `.hero-row` (flex with `space-between` + wrap) and `.hero-stats-inline` (flex with wrap, style.css:179-192).
- `analytics-grid`, `health-grid`, and `bracket-v2-row` all have explicit mobile overrides:
  - `.analytics-grid` collapses to 1 column at 760px (style.css:1023-1038).
  - `.health-grid` collapses to 1 column at 760px (style.css:1304-1315).
  - `.consistency-row` wraps at 760px (style.css:1158-1165).
  - `.bracket-v2-row` becomes `auto 1fr` at 720px (style.css:2856-2866).
- These panels are the **only** parts of the app with intentional mobile breakpoints beyond the global ones.

### Issues

**Issue 4.1 — Hero stats row collides with deck title on narrow widths.**
`.hero-row` wraps the stats below the title (good), but `.hero-stats-inline` has `gap: 1.25rem` and items like `${{ deck_total_value }}`, `XW–XL`, and the bracket badge. On 375px, this typically fits on 2 rows but the bracket badge (which is lazy-loaded and pops in late) reflows the row after page paint — bad UX.
**Severity: medium.**

**Issue 4.2 — Bracket v2 confidence column has `min-width: 220px`.**
[style.css:2798-2803](app/static/style.css#L2798-L2803). Combined with `grid-template-columns: auto 1fr auto` on `.bracket-v2-row`, the 220px constraint forces horizontal scroll until the 720px breakpoint kicks in. The breakpoint correctly drops it to a full-width second row.
**Severity: low (handled, but breakpoint is at 720 not 768).**

**Issue 4.3 — Tokens Needed table has no `.table-wrap`.**
[deck_detail.html:494-538](app/templates/deck_detail.html#L494-L538). Bare `<table>` with 6 columns: Token, Needed, Owned, Location, Status, ×. Will overflow horizontally on a 375px viewport and **scroll the whole page**, not just the table. `.table-wrap { overflow-x: auto }` exists (style.css:495-502) but isn't applied here.
**Severity: high.**

**Issue 4.4 — Add-token-requirement form has 5 controls in a `.filter-row`.**
[deck_detail.html:546-577](app/templates/deck_detail.html#L546-L577): name input + qty (5rem) + inventory dropdown + notes input + Add button. Each has `min-width: 140px`. Will produce a 5-row stack on mobile with no visual hierarchy.
**Severity: medium.**

**Issue 4.5 — Bulk Move panel uses inline-styled scroll container with `max-height: 260px`.**
[deck_detail.html:583-639](app/templates/deck_detail.html#L583-L639). On a phone in landscape this is fine, but in portrait the 260px max-height + the heavy filter-row below means most of the screen is two stacked scrollable regions. The "Select all" label, the per-row checkboxes, and the destination dropdown all stack vertically. The per-row checkbox label has `font-size: 0.875rem` which is **at the 14px floor** (close to too small).
**Severity: medium.**

**Issue 4.6 — Intent survey form uses `grid-template-columns: 1fr 1fr` (style.css:2839).**
Drops to single column at 720px (style.css:2863-2865). At 375-480px the two-column layout cramps the select labels. Breakpoint is correct in spirit, just one of three different "mobile" widths in the same file.
**Severity: low (handled).**

**Issue 4.7 — Deck hero action row has 3 buttons (Export, Retag, Delete) inline.**
[deck_detail.html:15-36](app/templates/deck_detail.html#L15-L36). Inline `display: flex; gap: 0.5rem; flex-wrap: wrap`. Tap targets are OK (~44px from button padding) but Delete sits next to Retag with no danger color (Delete uses default button styling, only the Retag tooltip distinguishes them). Risk of mis-tap.
**Severity: medium (UX, not layout).**

**Issue 4.8 — Cards-in-deck filter row repeats the 6-control problem from collection.html.**
[deck_detail.html:466-488](app/templates/deck_detail.html#L466-L488). Same `.filter-row` pattern as collection.html. Same issue.
**Severity: medium.**

**Issue 4.9 — `.synergy-card-list` and `.dead-cards-list` use `columns: 2` (style.css:1729, 1754).**
2-column flow inside a panel that is already narrow on mobile produces ~140-150px columns of card names — readable but tight. No mobile override.
**Severity: low.**

**Issue 4.10 — Inventory card "Move to Location" dropdown is full-width but next to a Move button.**
Inside the deck-card actions drawer, the select has no `style="width: …"` so it competes with the button. On a 320px column the dropdown text gets truncated and the button label "Move to Location" wraps.
**Severity: medium.**

---

## 5. decks.html

### Layout
- "New Deck" filter-row with name input + format select + Create button — 3 controls, fine.
- Decks table with 6 columns: Name, Format, Total Cards, Bracket, Health, (actions). The actions cell has `text-align: right; white-space: nowrap` (decks.html:87) — an inline-popout Edit `<details>` + Delete form.

### Issues

**Issue 5.1 — Decks table is NOT wrapped in `.table-wrap`.**
[decks.html:42-123](app/templates/decks.html#L42-L123). Bare `<table>` with 6 columns, including a `bracket-popout` that is `position: absolute; min-width: 220px; max-width: 320px` (style.css:1601-1613). On mobile, opening the popout from a table inside a non-scrollable container will produce either clipping or page-level horizontal scroll. The Edit popout uses the same pattern (`.edit-popout { position: absolute; min-width: 260px }`, style.css:782-793).
**Severity: high.**

**Issue 5.2 — `white-space: nowrap` on the actions cell forces it wider than the viewport.**
[decks.html:87](app/templates/decks.html#L87). Even after `flex-wrap` on the form, the inline `<details>` summary + Delete button + `margin-right: 0.4rem` produces a wide cell. With 6 other columns competing for width, the table overflows.
**Severity: high.**

**Issue 5.3 — Bracket badge is a `<details>` whose `<summary>` is the badge.**
The popout sits at `top: calc(100% + 6px); left: 0` (style.css:1601-1613). Inside a table row on mobile, the popout will likely overflow viewport-right, since `max-width: 320px` is wider than the typical 343px content area minus padding.
**Severity: medium.**

---

## 6. card_detail.html

### Layout
- Uses **classes that don't exist in style.css**: `.detail-layout`, `.detail-image-panel`, `.detail-meta-panel`, `.detail-image`, `.inventory-actions` (grep against style.css confirms no matches). The page falls back to default block layout — image and meta panels stack vertically, which on mobile is actually OK. On desktop this is presumably "broken" too but the user didn't flag it, so it's working by accident.
- Inventory table is wrapped in `.table-wrap` (card_detail.html:42). Good.

### Issues

**Issue 6.1 — Inventory table has 7 columns including a 4-form Actions cell.**
[card_detail.html:43-107](app/templates/card_detail.html#L43-L107). `.table-wrap` scrolls horizontally, so this is OK functionally, but the Actions cell stacks 4 forms each containing a number input + button. On mobile, the table-wrap scroll means the user has to scroll right to even see the Actions, and then each form has a tiny qty input + button. The qty input has no `style="width: ..."` so it inherits browser default (~80-120px on iOS, which is fine).
**Severity: medium.**

**Issue 6.2 — `.inventory-actions` is referenced but not styled.**
[card_detail.html:67](app/templates/card_detail.html#L67). The 4 inline-forms inside `.inventory-actions` flow inline because `.inline-form { display: inline-flex }` (style.css:225-230). On a 343px viewport with 4 forms in a single cell, they line-wrap unpredictably.
**Severity: low (functional, just visually noisy).**

**Issue 6.3 — Hero `.stat-grid` is OK because it uses `auto-fit, minmax(180px, 1fr)`.**
Reflows to 1 column. Fine.

---

## 7. game_new.html and game_detail.html

### game_new.html

#### Layout
- Player-count picker is 7 buttons (2-8) in a `.player-count-row` with `flex-wrap: wrap`. Buttons have `padding: 0.3rem 0.8rem` → ~30px tall, **below 44px target**.
- Seat diagram is a 3×3 grid with explicit widths: `.sd-mid-row { grid-template-columns: 80px 1fr 80px }` (style.css:2638-2643). At 343px content width this leaves only 183px for the center "Screen" placeholder and the diagram itself is constrained by `max-width: 420px` (style.css:2625-2631). Should work.
- Each `.seat-row` (style.css:2564-2592) has `gap: 0.6rem; flex-wrap: wrap` with 5 child fields including a hidden input and a seat-pos-pill button. `.seat-row-fields` flex-children include `input { width: 140px }` and `select { flex: 1; min-width: 160px }` — on a 343px content area, 140 + 16 + 160 = 316px is the first row, the deck select (`min-width: 180px`) wraps below. Total seat-row height is ~3-4 rows of controls per player × 4+ players = a long form on mobile.

#### Issues

**Issue 7.1 — Player-count buttons are smaller than 44px.**
[game_new.html:34-37](app/templates/game_new.html#L34-L37) + style.css:2550-2562. `padding: 0.3rem 0.8rem` produces ~28-32px height. Used during game setup on a phone — frequent tap targets.
**Severity: high.**

**Issue 7.2 — Seat diagram tap targets are 44px min-height (good).**
`.sd-seat { min-height: 44px }` (style.css:2644-2660). One of the few correctly-sized tap targets in the app.

**Issue 7.3 — Seat row stacks 4-5 fields per seat with `min-width: 160-180px` on dropdowns.**
[game_new.html:174-186](app/templates/game_new.html#L174-L186). On mobile each seat takes ~5 wrapped rows. With 4 seats that's ~20 stacked rows of controls before the Start Game button. Very long form.
**Severity: medium.**

**Issue 7.4 — `.seat-pos-pill` has small horizontal padding (0.25rem 0.7rem) → ~28px tall.**
Style.css:2595-2613. **Below 44px tap target.**
**Severity: medium.**

### game_detail.html (live tracker)

#### Layout
This page is the only one with deliberate fullscreen + tablet-first design. It has its own coordinate system (`.tracker-grid`, fixed inset, dynamic JS layout) and a portrait-mode media query (style.css:1944-1950) that triggers a 2-column auto-flow layout via JS.

- Tap targets are intentionally sized: `.tc-life-btn { min-height: 52px; min-width: 58px }`, `.tc-elim-btn { min-width: 36px; min-height: 36px }`, `.turn-next-btn { min-height: 44px }`, `.fs-btn` is small but corner-tucked.
- The launch overlay has a `min-height: 60px` CTA.

#### Issues

**Issue 7.5 — `.tc-cpill-btn` and `.tc-ctr-btn` are 20px and 32px respectively — below 44px.**
Style.css:2363-2381 (cpill: 20px), 2400-2416 (ctr-btn: 32px). Poison and experience pill ±, plus commander-damage ±. Small but very frequent during a game. Acceptable for tablet at arm's length but rough on a phone.
**Severity: medium.**

**Issue 7.6 — `.tc-elim-btn` is 36×36 (style.css:2230-2238) — below 44.**
Eliminate/revive button. Tap consequence is high (gray out an entire player card).
**Severity: medium.**

**Issue 7.7 — Game tracker is essentially landscape-only.**
The portrait-mode `@media (orientation: portrait)` block only enables the portrait-turn-row visibility (style.css:1944-1950) — actual layout is built dynamically by `applyLayout()` in the page JS. On a phone in portrait, 4 player cards stacked in a 2-column auto-flow produces tiny cards. Per CLAUDE.md history (v3.14.1+), this was an intentional decision: tracker is tablet-first.
**Severity: known constraint — flag that phone use is degraded.**

**Issue 7.8 — End-game modal has `.end-game-row` with `width: 130px` on the player name and an explicit `width: 100px` final-life input.**
[game_detail.html:200-225](app/templates/game_detail.html#L200-L225) + style.css:2538-2542. On 375px the modal's `max-width: 520px; width: 100%` flexes fine, but the player-name `width: 130px` + select + 100px final-life input wraps awkwardly. The notes textarea has `style="width:100%; max-width:520px"` which is correct.
**Severity: low.**

---

## 8. locations.html and location_detail.html

### locations.html

#### Layout
- Two stacked `filter-row` forms (Add Location, Add Deck) — 3-4 controls each.
- One 7-column table: Name, Type, Parent, Rows, Qty, Value, Actions.
- Actions cell has `text-align: right; white-space: nowrap` with an Open link + Edit `<details>` popout + Delete form.

#### Issues

**Issue 8.1 — Locations table is NOT wrapped in `.table-wrap`.**
[locations.html:51-131](app/templates/locations.html#L51-L131). 7 columns + a third popout. Same problem as decks.html — popout `min-width: 260px` (style.css:782-793) will overflow on mobile. Page-level horizontal scroll.
**Severity: high.**

**Issue 8.2 — Edit popout uses `position: absolute; right: 0` (style.css:782-793).**
When the row is near the right edge of a scrolled-narrow table, the popout sits left-aligned to the trigger. With `min-width: 260px` and a typical 343px content area, the popout will often be clipped by the table-wrap if one is added.
**Severity: medium.**

### location_detail.html

#### Layout
- Stat-grid + filter-row + Bulk Move `<details>` + inventory-grid.
- The Bulk Move pattern duplicates deck_detail.html's pattern, with the same `max-height: 260px` scroll container.

#### Issues

**Issue 8.3 — `<div class="hero-row"><div>title</div><div style="display:flex">…buttons</div></div>` puts Export CSV + Back-to-Locations buttons next to the title.**
[location_detail.html:6-22](app/templates/location_detail.html#L6-L22). On mobile, hero-row wraps fine, but the action buttons end up below the description — workable but adds vertical height.
**Severity: low.**

**Issue 8.4 — Per-card "Pending placement" hint uses `text-align: center` inline.**
[location_detail.html:120](app/templates/location_detail.html#L120). Cosmetic; works fine.
**Severity: none.**

**Issue 8.5 — Same filter-row + bulk-move issues as deck_detail.html.**
See 4.5, 4.8.

---

## 9. pending.html

### Layout
- `.pending-item` is a CSS grid: `grid-template-columns: 96px 1fr auto` (style.css:422-431). At 720px collapses to `72px 1fr` with action moving to `grid-column: 2` (style.css:555-564).
- `.pending-actions` is a flex row with `space-between` and wrap. Inline-forms inside `.pending-action` have `flex-wrap: wrap` (pending.html:91-94).

### Issues

**Issue 9.1 — Pending Confirm form is a select + button inline-flex.**
[pending.html:91-110](app/templates/pending.html#L91-L110). The location select has no width constraint, and the Confirm button is `class="danger-button"` (red). On a 343px viewport with the 72px thumbnail occupying ~80px and the gap consuming ~16px, the body has ~247px left. The select label `<location name> (<type>)` is often >30 characters and gets truncated. The select itself is a default browser control so the touch target is the system-default, but visually it's tight.
**Severity: medium.**

**Issue 9.2 — `.pending-meta` is one long line of "Set · Code · Finish · Qty · Price".**
[pending.html:80-82](app/templates/pending.html#L80-L82). Single-line text with `·` separators; will wrap fine but on very narrow screens may produce 3+ lines.
**Severity: low.**

**Issue 9.3 — `.pending-copy` referenced but not defined in CSS.**
Pending.html:78. Falls through to inherited block layout. Works, but indicates orphan class names.
**Severity: cosmetic (cleanup).**

**Issue 9.4 — Top-of-page action buttons (Confirm ALL, Undo Last Batch) are full-width `<button class="danger-button">` and `<button class="ghost-button">`.**
[pending.html:38-52](app/templates/pending.html#L38-L52). Buttons are ~44px tall via `button { padding: 0.58rem 0.9rem }` global. OK on mobile, but the Confirm-ALL danger-styled button is risky on a phone — easy to fat-finger.
**Severity: medium (UX, not layout).**

---

## 10. import.html

### Layout
- 4 panels: Upload CSV, Paste Card List, Search by Name, Manual Entry.
- Upload CSV and Search panels use `.filter-row` (inline-flex with wrap). Paste-list and Manual use `.stack-form` (column flex).

### Issues

**Issue 10.1 — `<input type="file">` is browser-default, no width control.**
[import.html:30](app/templates/import.html#L30). On iOS this renders as a "Choose File" button that's tap-friendly. On Android it's a button + filename — usually OK. Functional.
**Severity: none.**

**Issue 10.2 — Paste-list `<textarea>` has `rows="10"` but no width constraint.**
[import.html:48-50](app/templates/import.html#L48-L50). The textarea inherits from the input/textarea default styles (style.css:203-214) which have no width — falls back to browser default (usually expands to container). On mobile this is fine since it'll fill the panel. The placeholder shows multi-line examples which is good.
**Severity: low.**

**Issue 10.3 — Manual Entry form is `.stack-form` (column).**
[import.html:66-89](app/templates/import.html#L66-L89). 6 labeled inputs stacked. Inputs have no explicit width, so they fill the panel — good for thumb typing.
**Severity: none.**

**Issue 10.4 — Search-by-name input is `.filter-row` with `min-width: 140px` on input.**
[import.html:54-63](app/templates/import.html#L54-L63). On a 343px viewport with the Search button to the right, the input is fine because `flex-wrap` lets it take the full row. But the layout is inconsistent with Manual Entry (which uses `.stack-form`) and Paste List (also `.stack-form`).
**Severity: low (consistency).**

---

## Global CSS issues

### Media-query inventory

| # | Line | Breakpoint | Targets | Notes |
|---|------|-----------|---------|-------|
| 1 | 504  | `max-width: 980px` | `.inventory-grid`, `.inventory-card`, `.inventory-thumb`, `.compact-form-grid` | First-pass shrink (laptop → tablet). Not a mobile breakpoint. |
| 2 | 522  | `max-width: 720px` | `.header-shell`, `.site-title`, `.header-right`, `.header-tagline`, `.inventory-card`, `.pending-item`, `.pending-thumb`, `.pending-action` | Main "mobile" pass. Hides `.header-tagline`. Collapses inventory card to single column. |
| 3 | 1023 | `max-width: 760px` | `.analytics-grid`, `.analytics-section` | Deck analytics 1-col collapse. |
| 4 | 1158 | `max-width: 760px` | `.consistency-row`, `.consistency-breakdown` | Consistency badge wrap. |
| 5 | 1304 | `max-width: 760px` | `.health-grid`, `.health-pips` | Deck health 1-col collapse. |
| 6 | 1944 | `orientation: portrait` | `.portrait-turn-row` | Game tracker portrait-only nav row. |
| 7 | 2856 | `max-width: 720px` | `.bracket-v2-row`, `.bracket-v2-confidence`, `.bracket-v2-intent-form` | Bracket V2 panel 1-col collapse. |

### Inconsistencies

- **Three breakpoints for "small screen": 720, 760, 980.** No semantic reason for the 40px gap. Likely organic growth: header/inventory were done at 720, analytics/health at 760, bracket-v2 at 720 again.
- **No breakpoint between 720 and 980** — most of the app has no "tablet portrait" pass (768–1024). The card grid stays at `minmax(280px, 1fr)` from 980 down to 720, then collapses inventory card to single-column. Tablet portrait gets the desktop layout slightly squeezed.
- **No `min-width` breakpoints** — purely mobile-shrink (max-width) rather than mobile-first (min-width). All defaults are desktop-sized.
- **No tap-target enforcement.** `<button>` global style is `padding: 0.58rem 0.9rem` ≈ 32-36px tall. Several specific button classes (`tc-cpill-btn`, `tc-ctr-btn`, `tc-elim-btn`, `seat-pos-pill`, `player-count-btn`, `health-cards-link`, page-number buttons) are smaller than 44px.
- **No font-size guard.** Many classes use `0.7rem`-`0.85rem` (~11-14px after 16px root). With viewport scaling at 1.0, these become ~11-14px on the device — at or below the 14px legibility floor. Notably: `.card-tag` (0.65rem ≈ 10.4px), `.curve-count`/`.curve-label` (0.68-0.7rem), `.bracket-v2-conf-row` (0.78rem), `.sd-pos` (0.7rem).

### Desktop-first patterns

- **Fixed pixel widths in flex/grid children:**
  - `.inventory-card { grid-template-columns: 150px 1fr }` (style.css:336-340) — handled at 980/720
  - `.pending-item { grid-template-columns: 96px 1fr auto }` (style.css:422-431) — handled at 720
  - `.analytics-grid { grid-template-columns: 190px 1fr 160px }` (style.css:858-863) — handled at 760
  - `.health-grid { grid-template-columns: 1fr 280px }` (style.css:1168-1173) — handled at 760
  - `.health-row { grid-template-columns: 100px 1fr 72px auto }` (style.css:1185-1191) — **NOT handled at mobile** (will compress badly)
  - `.bracket-v2-conf-row { grid-template-columns: 110px 1fr 36px }` (style.css:2804-2810) — **NOT handled at mobile** (the 110px label cramps the bar)
  - `.bracket-v2-row { grid-template-columns: auto 1fr auto }` redefined as `1fr 1fr` initially then `grid: auto 1fr auto` (style.css:2717, 2784-2789) — duplicated rules, redefined twice
  - `.deck-create-form { grid-template-columns: 200px 180px 1fr auto }` (style.css:687-693) — **NOT handled at mobile**
  - `.cs-sub { grid-template-columns: 58px 1fr 36px }` (style.css:1117-1122) — **NOT handled at mobile**
  - `.analytics-row { grid-template-columns: 88px 1fr 2.2rem }` (style.css:973-979) — **NOT handled at mobile**

- **Large `min-width` on form controls:**
  - `.filter-row input, .filter-row select { min-width: 140px }` (style.css:246-249) — forces wrap on phones, no mobile override
  - `.seat-row-fields select { min-width: 160px }` (style.css:2589-2592)
  - `.bracket-v2-confidence { min-width: 220px }` (style.css:2798-2803) — handled at 720
  - `.consistency-info { min-width: 140px }` (style.css:1096-1101)
  - `.synergy-stat { min-width: 160px }` (style.css:1671-1674)
  - `.combo-card-name`, `.synergy-stat-label`, etc. don't have min-widths but their content makes them wide

- **Side-by-side flex without wrap (or with wrap but no fallback):**
  - `.hero-row` flexes with wrap but children have no widths → variable wrap behavior
  - `.detail-layout` referenced but not defined → falls through, accidentally works
  - `.tc-life-section { display: flex; gap: 0.75rem }` (style.css:2259-2266) — wraps in JS-controlled rotations but not in default flow

- **Tables without `.table-wrap`:**
  Six unwrapped `<table>` elements identified:
  1. [decks.html:42](app/templates/decks.html#L42) — 6 cols
  2. [deck_detail.html:494](app/templates/deck_detail.html#L494) — Tokens Needed, 6 cols
  3. [locations.html:51](app/templates/locations.html#L51) — 7 cols
  4. [drawers.html:28](app/templates/drawers.html#L28) — drawer summary
  5. [games.html:26](app/templates/games.html#L26) — game history
  6. [audit.html:19, :54](app/templates/audit.html#L19) — two audit tables

  Only [card_detail.html:42](app/templates/card_detail.html#L42) correctly uses `.table-wrap`. The global rule `.table-wrap table { min-width: 900px }` (style.css:500-502) shows the design assumed wrapping; the templates just didn't get it applied.

- **Inline `style="…"` proliferation:**
  Many templates use inline `style="display:flex; gap:0.5rem; flex-wrap:wrap"` instead of a named class. Makes mobile overrides impossible without `!important`. Affected: collection.html, decks.html, deck_detail.html, location_detail.html, locations.html, card_detail.html.

- **`position: absolute` popouts with `min-width` ≥ viewport width:**
  - `.edit-popout { min-width: 260px }`
  - `.bracket-popout { min-width: 220px; max-width: 320px }`
  - `.layout-picker-popover { min-width: 200px }`
  - `.inline-details.inline-details-side .edit-popout { max-width: 240px }`

  All position themselves with `position: absolute` and offset from their trigger. On mobile inside a table cell, they will overflow.

---

## Recommended breakpoint strategy

**Proposal: collapse the three existing breakpoints (720 / 760 / 980) to two semantic ones — 768 and 480 — with a third orientation query reserved for the game tracker.**

| Breakpoint | Name | Purpose |
|------------|------|---------|
| `(max-width: 768px)` | `--bp-tablet` | Phone + tablet portrait. Single column for analytics/health/inventory cards. Filter rows stack. Tables wrap. Header nav collapses to a hamburger or scrolling-pill row. |
| `(max-width: 480px)` | `--bp-phone` | True phone. Extra-aggressive: smaller hero, taller tap targets (44px floor enforced), bulk-move scroll containers expand to full height, popouts become bottom-sheet modals instead of absolute. |
| `(orientation: portrait) and (max-width: 768px)` | game-tracker | Already used. Keep. |

**Why 768 and not 720/760?**
- 768 is the iPad Mini portrait width and a near-universal "tablet portrait" threshold in popular frameworks (Bootstrap, Tailwind). Matching it avoids surprises.
- The current 720 / 760 split provides zero functional difference at the 720-760 viewport range — they fire 40px apart for no reason. Picking 768 captures both intents.
- A single primary breakpoint is easier to reason about and audit.

**Why also 480?**
- Several patterns that work at 480-768 (e.g. side-by-side stat cards, 2-column tag editor) break at <400 (iPhone SE width is 375). Having an explicit second tier prevents the "works on most phones, breaks on small ones" failure mode.
- Phone-specific overrides like converting popouts to bottom sheets, enforcing 44px tap targets globally, and stacking the header nav apply at this tier.

**Why not mobile-first (min-width)?**
- The codebase is already 2966 lines of desktop-first CSS. Migrating to mobile-first is a larger refactor than the audit recommends. Two consistent max-width breakpoints achieve 90% of the benefit with 10% of the churn.

**Single-breakpoint variant:**
If the team wants the minimal change: one breakpoint at `768px` solves the majority of cases. The 480px tier is a follow-on if/when tap-target enforcement and popout→sheet conversion are picked up.

---

## Priority of fixes (suggested order, not part of this audit's scope)

1. **Nav collapse to hamburger or pill scroller** (Issue 1.1) — blocks adoption.
2. **Wrap the six unwrapped tables** in `.table-wrap` (Issues 4.3, 5.1, 8.1, audit/games/drawers).
3. **Tap-target floor of 44px** on `.tc-cpill-btn`, `.tc-ctr-btn`, `.tc-elim-btn`, `.player-count-btn`, `.seat-pos-pill`, page-number buttons, `.fs-btn`.
4. **Filter-row mobile pattern** — drop the global `min-width: 140px` on inputs/selects below 768px and let them go full-width.
5. **Popout panels (`.edit-popout`, `.bracket-popout`) become bottom-sheet modals on phones**, or at minimum constrain `max-width` to `90vw`.
6. **Consolidate breakpoints** from 720/760/980 to 768/480.
7. **Inventory-card grid:** consider reducing `minmax(320px, 1fr)` to `minmax(260px, 1fr)` so 2-column layouts appear in tablet portrait.
8. **Strip the desktop-only search placeholder** on collection/deck filters at narrow widths (or shorten it).
9. **Long button labels** ("Logout (jason@vanfreckle.com)") truncate or icon-ize on phones.
10. Game tracker portrait mode: accept as known constraint; document that primary use is tablet landscape.

---

## Notes for the implementation session that follows

- Five class names referenced in templates have no definition in style.css: `.detail-layout`, `.detail-image-panel`, `.detail-meta-panel`, `.detail-image`, `.inventory-actions`, `.pending-copy`, `.small-text`. These should be either defined or removed.
- `.bracket-v2-row` is defined twice (style.css:2717 and :2784) with conflicting `grid-template-columns`. The second declaration wins; cleanup needed.
- The 7 media-query blocks in style.css have no shared variable for breakpoint values. Consider CSS custom property `--bp-tablet: 768px` (though CSS custom properties don't work inside `@media` conditions — would need a build step or a documented constant).
- This audit deliberately did not propose code or modify any source file. The next session can pick fixes off the priority list above.
