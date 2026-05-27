/* Goldfish playtester — v3.30.1 fullscreen surface + type-based regions.
 *
 * v3.30.1 changes (this release):
 *   Change A — Fullscreen play surface. The page now wraps its content in
 *              a `.gf-app` overlay (`position: fixed; inset: 0; z-index:
 *              500`) that covers the standard chrome — exact pattern the
 *              v3.28.13 game tracker uses. `document.body.classList.add(
 *              "gf-mode")` on boot triggers `body.gf-mode { overflow:
 *              hidden }` to lock page scrolling; cleared on `pagehide`
 *              (which fires on browser-tab close, history navigation,
 *              and reload). The masthead carries a ← Back-to-deck chip
 *              and a ⛶ fullscreen toggle (browser Fullscreen API).
 *   Change B — Type-based battlefield regions. The v3.30.0 two-row Lands
 *              / Permanents split is replaced by FIVE regions driven by
 *              `classifyRegion(card.type_line)` with strict priority
 *              Creature → Planeswalker/Battle → Artifact/Enchantment →
 *              Land → Other. An artifact-creature lands in Creatures
 *              (creature wins — how a player thinks of it in play). A
 *              mana rock stays in Artifacts & Enchantments — region is
 *              by card *type*, not by function; the app has no
 *              produced-mana data (`Card.produced_mana` is the deferred
 *              v3.30.0 follow-up). `placeOnBattlefield(id)` and every
 *              battlefield drop converge through `classifyRegion` —
 *              same single-source-of-truth discipline as v3.30.0 Fix 1,
 *              just more buckets.
 *
 * Carried forward from v3.30.0 (unchanged in v3.30.1):
 */
/* Goldfish playtester — v3.30.0 (post-dev-feedback revision pass).
 *
 * Six feedback items folded into v3.30.0 itself (no -N suffix; v3.30.0 has
 * not shipped yet — one tag, one set of release notes, written against the
 * final revised surface).
 *
 *   Fix 1 — Battlefield placement is driven by kindOf(type_line), not by
 *           the drop sub-element. One placement function, used by both
 *           click-move (playFromHand) and drag-drop. Lands land in Lands;
 *           non-lands land in Permanents; cannot diverge.
 *   Fix 2 — Context menu is fully removed from the DOM on close. Old
 *           shape: a static <div id="gf-context-menu"> toggled visible
 *           via the hidden attribute, then innerHTML-cleared on close —
 *           but the rule .gf-context-menu { display: flex } overrode the
 *           hidden attribute's default display:none, leaving an empty
 *           shell on the table. New shape: a fresh `.gf-ctx` node is
 *           appended to <body> on open and `.remove()`-d on close. No
 *           toggleable singleton, no husk.
 *   Fix 3 — `.gf-ctx` lives at z-index 1200, above the modal's 1100, so
 *           menus opened from inside the browse modal sit on top of the
 *           overlay. Menu clicks stopPropagation; modal overlay's click-
 *           to-dismiss only fires when event.target IS the modal itself
 *           (the menu is body-level and never has the modal as ancestor,
 *           so menu-item clicks naturally never reach modal's handler).
 *   Add 4 — Left-click on a battlefield card now toggles tap/untap
 *           directly (no menu trip). Right-click opens the context menu
 *           (the browser's native menu is suppressed via preventDefault).
 *           Touch fallback: long-press (500ms) opens the menu, AND a
 *           small kebab in the card's top-right opens it. Click semantics
 *           by zone are explicit (see attachCardHandlers).
 *   Add 5 — Semi-automated mana pool. A floating widget with W/U/B/R/G/C
 *           pips, +/− steppers, and a Clear control. Tapping a land (the
 *           untapped→tapped transition only — never on untap) pops a
 *           color picker; the user picks the color the land taps for,
 *           and that color is +1 in the pool. Pool resets on New Turn
 *           and New Game. A FULLY automated pool — one that knows what
 *           each land taps for — is deferred; it requires a new
 *           `Card.produced_mana` column populated by the v3.25.0 bulk-
 *           data daemon (Scryfall's `produced_mana` field), which is a
 *           schema + daemon change, out of scope for this revision pass.
 *   Add 6 — Browse works uniformly for every pile zone (Library,
 *           Graveyard, Exile, Command). Each pile's zone-head opens its
 *           full contents in the modal. Cards in the modal support BOTH
 *           click → context menu (modal stays open) AND drag → drop
 *           onto any zone (overlay closes on dragstart so the drop
 *           targets underneath are reachable). LIBRARY browse is the
 *           tutor path: it shows the library sorted by mana value then
 *           name — NOT in draw order, so the user can't read the deck
 *           top — and closing the library browse RESHUFFLES the library
 *           (tied to a `libraryBrowseOpen` flag so it cannot double-
 *           fire). Look-at-top-N (in the controls bar) stays separate
 *           and preserves draw order — scry/surveil-style, no shuffle.
 *
 * Two settled UX calls from the original spec carry forward unchanged:
 * (1) mulligan-bottoming uses the per-card menu's "Send to library
 * bottom" rather than an explicit keep/bottom prompt (London with
 * post-draw bottoming); (2) battlefield auto-splits to Lands /
 * Permanents on entry (render hint, not a rules claim — user can drag
 * between rows freely).
 *
 * Constraint compliance: pure client-side; no schema, no migration, no
 * server-side changes, no new request-path network calls (card art
 * still fetched browser-side from `Card.image_url` per v3.26.1). The
 * surface remains read-only against InventoryRow.
 */
(function () {
  "use strict";

  // ── State ──────────────────────────────────────────────────────
  // v3.30.1 — battlefield is now five type-based regions (Change B).
  // The legacy aliases `battlefield-lands` and `battlefield-permanents`
  // from v3.30.0 still resolve through `classifyRegion()` if a drop
  // handler reports them; new drops use `data-zone-drop="battlefield"`
  // and classify by card type.
  const state = {
    deckId: null,
    deckName: "",
    library: [],
    hand: [],
    bfCreatures: [],
    bfLands: [],
    bfArtEnc: [],
    bfPwBattle: [],
    bfOther: [],
    graveyard: [],
    exile: [],
    command: [],
    life: 40,
    turn: 1,
    instanceSeq: 1,
    manaPool: { W: 0, U: 0, B: 0, R: 0, G: 0, C: 0 },
    activeMenu: null,
    activeManaPicker: null,
    activeCounterPanel: null, // v3.30.5 — { node, body, instId } when open
    libraryBrowseOpen: false,
    modalContext: null,
    longPressTimer: null,
    // v3.30.3 — opt-in "Draw on new turn" preference. Default ON
    // (preserves v3.30.0–v3.30.2 behavior). The running source of
    // truth: newTurn() reads state.autoDrawOnTurn, never localStorage,
    // so the per-turn path is allocation-free. Persisted to
    // localStorage under `cartarch-goldfish-autodraw` ("true"/"false")
    // by the change handler in the boot sequence below. NOT reset by
    // newGame() — this is a preference, not a per-session value.
    autoDrawOnTurn: true,
  };

  // ── Boot: parse payload ───────────────────────────────────────
  let payload = null;
  try {
    payload = JSON.parse(document.getElementById("gf-deck-data").textContent);
  } catch (e) {
    payload = { deck_id: null, deck_name: "", cards: [] };
  }

  function buildInstances() {
    state.library = [];
    state.command = [];
    state.hand = [];
    state.bfCreatures = [];
    state.bfLands = [];
    state.bfArtEnc = [];
    state.bfPwBattle = [];
    state.bfOther = [];
    state.graveyard = [];
    state.exile = [];
    state.instanceSeq = 1;
    for (const c of payload.cards || []) {
      for (let i = 0; i < (c.quantity || 0); i++) {
        const inst = {
          id: "gf-" + state.instanceSeq++,
          card: c,
          tapped: false,
          // v3.30.5 — counters: { label → integer count }. Free-form
          // annotations the user manages by hand; goldfish is not a
          // rules engine. Battlefield-only — cleared by moveTo when
          // the card leaves the battlefield. Negative counts allowed
          // (no clamp). A label dropping to exactly 0 is removed
          // from the map (no zero-count entries linger). Every
          // mutation goes through adjustCounter() — single source
          // of truth. Reads use `inst.counters || {}` for backward-
          // safe access against any pre-v3.30.5 instance shape that
          // a stale browser tab might still be running.
          counters: {},
          // v3.30.6 — attachedTo: instance id string or null. A
          // RENDER POINTER, not a state-array move — an attached
          // card stays in its own classifyRegion array; only the
          // render nests it beneath the host. ONE-LEVEL rule:
          // attachInstance rejects nested or self-referential
          // attachments. Cleared by moveTo on ANY leave-host path
          // (move to non-battlefield, move to a different
          // battlefield region). Backward-safe: reads use
          // `inst.attachedTo` directly with `null` semantics, so a
          // missing field on a pre-v3.30.6 instance is treated as
          // unattached.
          attachedTo: null,
        };
        if (c.is_commander) {
          state.command.push(inst);
        } else {
          state.library.push(inst);
        }
      }
    }
    shuffle(state.library);
  }

  function shuffle(arr) {
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
  }

  // ── Zone helpers ──────────────────────────────────────────────
  // v3.30.1 — zone list registry. Battlefield expands to five regions.
  // Legacy `battlefield-lands` / `battlefield-permanents` keys from v3.30.0
  // are no longer first-class zones; they re-route through classifyRegion
  // when they appear in drop targets or move-to actions.
  const ZONE_LISTS = {
    library: () => state.library,
    hand: () => state.hand,
    "bf-creatures": () => state.bfCreatures,
    "bf-lands": () => state.bfLands,
    "bf-artenc": () => state.bfArtEnc,
    "bf-pwbattle": () => state.bfPwBattle,
    "bf-other": () => state.bfOther,
    graveyard: () => state.graveyard,
    exile: () => state.exile,
    command: () => state.command,
  };

  const BATTLEFIELD_REGION_KEYS = new Set([
    "bf-creatures",
    "bf-lands",
    "bf-artenc",
    "bf-pwbattle",
    "bf-other",
  ]);

  /**
   * Classify a card into a battlefield region by type_line, strict
   * priority Creature → Planeswalker/Battle → Artifact/Enchantment →
   * Land → Other. First match wins. An artifact-creature returns
   * "bf-creatures" (creature wins — the player's mental model in play).
   * A mana rock returns "bf-artenc" — regions are by card *type*, not
   * by function; the function-aware path needs the deferred
   * Card.produced_mana migration.
   */
  function classifyRegion(card) {
    const tl = ((card && card.type_line) || "").toLowerCase();
    if (tl.includes("creature")) return "bf-creatures";
    if (tl.includes("planeswalker") || tl.includes("battle")) return "bf-pwbattle";
    if (tl.includes("artifact") || tl.includes("enchantment")) return "bf-artenc";
    if (tl.includes("land")) return "bf-lands";
    return "bf-other";
  }

  function findInstance(id) {
    for (const zone of Object.keys(ZONE_LISTS)) {
      const list = ZONE_LISTS[zone]();
      const idx = list.findIndex((x) => x.id === id);
      if (idx !== -1) return { zone, list, idx, inst: list[idx] };
    }
    return null;
  }

  /** kindOf — "land" or "nonland". Sole authority on battlefield row routing. */
  function kindOf(card) {
    return ((card && card.type_line) || "").toLowerCase().includes("land")
      ? "land"
      : "nonland";
  }
  function isLand(inst) {
    return kindOf(inst.card) === "land";
  }

  function moveTo(id, targetZone, opts = {}) {
    const found = findInstance(id);
    if (!found) return;
    // v3.30.6 — attachment discipline. Two rules, applied BEFORE the
    // splice so the source-list state stays coherent for childrenOf
    // lookups. Children stay in their own battlefield arrays —
    // attachedTo is a render pointer, not a state-array move — so
    // detaching is a plain field assignment, no array bookkeeping.
    // Internal mutation; bypasses detachInstance to avoid N redundant
    // per-child renders before this move's own render fires at the
    // end. Single-source-of-truth discipline is about UI writers;
    // moveTo is an internal primitive applying the leave-host
    // semantics.
    if (!isBattlefieldZone(targetZone)) {
      // (1) Host moved to non-battlefield → detach every child. The
      //     children stay on the battlefield in their own regions,
      //     now top-level (childrenOf() will return [] for the host
      //     after the loop). They do NOT follow the host off the
      //     battlefield.
      const kids = childrenOf(found.inst.id);
      for (const child of kids) child.attachedTo = null;
    }
    // (2) Attached card moved ANYWHERE (including between
    //     battlefield regions) → clear its own attachedTo. Leaving
    //     the host, by any path, detaches.
    if (found.inst.attachedTo) {
      found.inst.attachedTo = null;
    }
    found.list.splice(found.idx, 1);
    const dest = ZONE_LISTS[targetZone]();
    found.inst.tapped = false;
    // v3.30.5 — counters are a battlefield-only annotation; they do
    // not travel off the battlefield. Single clear-on-leave site,
    // matching the v3.30.0 single-placement / single-transition
    // discipline. Battlefield-to-battlefield moves (e.g. a card
    // re-classified by classifyRegion after a transform) keep their
    // counters intact.
    if (!isBattlefieldZone(targetZone)) {
      found.inst.counters = {};
    }
    if (targetZone === "library") {
      if (opts.position === "bottom") dest.push(found.inst);
      else dest.unshift(found.inst);
    } else {
      dest.push(found.inst);
    }
    // v3.30.7 — token lifecycle. A token moved to any NON-battlefield
    // zone vanishes from state entirely; no dead token cards sit in
    // piles, in hand, or in the library. The vanish runs AFTER the
    // push so any earlier discipline (v3.30.6 child-detach when a
    // host leaves the battlefield; v3.30.5 counters clear) fires
    // first on a clean source-list state — child-detach reads
    // childrenOf(found.inst.id) which finds children still in their
    // own arrays, unaffected by where the host is now sitting; the
    // host is then removed from dest cleanly.
    if (found.inst.isToken && !isBattlefieldZone(targetZone)) {
      const idx = dest.indexOf(found.inst);
      if (idx !== -1) dest.splice(idx, 1);
    }
    render();
    refreshModalIfOpen();
  }

  /**
   * Single placement function — every battlefield-bound path (click-move
   * via menu, drag-drop onto any battlefield surface, the `Move →
   * Battlefield` menu action) converges here and routes the card to one
   * of five regions via `classifyRegion`. v3.30.1 expansion of v3.30.0
   * Fix 1's single-placement discipline.
   */
  function placeOnBattlefield(id) {
    const found = findInstance(id);
    if (!found) return;
    moveTo(id, classifyRegion(found.inst.card));
  }

  function isBattlefieldZone(zone) {
    return BATTLEFIELD_REGION_KEYS.has(zone);
  }

  function allBattlefieldInstances() {
    return [
      ...state.bfCreatures,
      ...state.bfLands,
      ...state.bfArtEnc,
      ...state.bfPwBattle,
      ...state.bfOther,
    ];
  }

  function drawN(n) {
    for (let i = 0; i < n; i++) {
      if (state.library.length === 0) return;
      state.hand.push(state.library.shift());
    }
    render();
  }

  function newGame() {
    buildInstances();
    state.life = 40;
    state.turn = 1;
    clearManaPool();
    drawN(7);
    refreshOpeningHandReadout();
    render();
  }

  function newTurn() {
    state.turn += 1;
    for (const inst of allBattlefieldInstances()) inst.tapped = false;
    clearManaPool();
    // v3.30.3 — draw step is opt-in. When state.autoDrawOnTurn is false
    // the turn still advances and the battlefield + mana pool still
    // reset; the user draws manually via the Draw button. No pending-
    // draw indicator, no "you forgot to draw" prompt — consistent with
    // the no-rules-enforcement principle.
    if (state.autoDrawOnTurn) {
      drawN(1);
    }
  }

  function mulligan() {
    // London: scoop hand, reshuffle, redraw 7. User bottoms N via per-card
    // "Send to library bottom" after each redraw.
    for (const inst of state.hand) state.library.push(inst);
    state.hand = [];
    shuffle(state.library);
    drawN(7);
    refreshOpeningHandReadout();
  }

  function millN(n) {
    for (let i = 0; i < n; i++) {
      if (state.library.length === 0) return;
      state.graveyard.push(state.library.shift());
    }
    render();
  }

  function untapAll() {
    for (const inst of allBattlefieldInstances()) inst.tapped = false;
    render();
  }

  /**
   * Toggle tap on a battlefield card. The untapped→tapped transition on a
   * LAND opens the mana-picker (Add 5); untap never prompts.
   */
  function toggleTap(id, anchorEl) {
    const found = findInstance(id);
    if (!found) return;
    if (!isBattlefieldZone(found.zone)) return;
    const wasUntapped = !found.inst.tapped;
    found.inst.tapped = !found.inst.tapped;
    render();
    if (wasUntapped && isLand(found.inst)) {
      // Re-find the card element after re-render — buildCardEl recreated it.
      const cardEl =
        document.querySelector(`.gf-card[data-inst-id="${id}"]`) || anchorEl;
      openManaPicker(cardEl);
    }
  }

  function playFromHand(id) {
    const found = findInstance(id);
    if (!found || found.zone !== "hand") return;
    placeOnBattlefield(id);
  }

  // ── Library look + browse (Addition 6) ────────────────────────
  function lookAtTopN(n) {
    // Order preserved — scry/surveil-style. No shuffle.
    const taken = state.library.slice(0, n);
    state.modalContext = { kind: "look" };
    openModal("Top " + taken.length + " of library", taken);
  }

  /**
   * Library Browse — tutor path. Display sorted (mana value, then name) so
   * the user can't read draw order. Closing the modal RESHUFFLES, whether
   * a card was moved or not. The flag prevents double-fire on a path that
   * calls closeModal twice (e.g. drag-out closes overlay + modal Close).
   */
  function browseLibrary() {
    state.libraryBrowseOpen = true;
    state.modalContext = { kind: "browse-library" };
    openModal("Library — closes reshuffles", libraryBrowseInstances());
  }

  function libraryBrowseInstances() {
    return state.library.slice().sort((a, b) => {
      const av = typeof a.card.cmc === "number" ? a.card.cmc : 99;
      const bv = typeof b.card.cmc === "number" ? b.card.cmc : 99;
      if (av !== bv) return av - bv;
      return (a.card.name || "").localeCompare(b.card.name || "");
    });
  }

  function browseZone(zone, title) {
    state.modalContext = { kind: "browse", zone };
    openModal(title, ZONE_LISTS[zone]());
  }

  // ── Mana pool widget (Addition 5) ─────────────────────────────
  const MANA_COLORS = ["W", "U", "B", "R", "G", "C"];
  const MANA_LABELS = {
    W: "White",
    U: "Blue",
    B: "Black",
    R: "Red",
    G: "Green",
    C: "Colorless",
  };

  function clearManaPool() {
    state.manaPool = { W: 0, U: 0, B: 0, R: 0, G: 0, C: 0 };
    renderManaPool();
  }
  function adjustMana(color, delta) {
    if (!(color in state.manaPool)) return;
    state.manaPool[color] = Math.max(0, state.manaPool[color] + delta);
    renderManaPool();
  }

  function buildManaPoolWidget() {
    const root = document.createElement("div");
    root.className = "gf-mana-pool";
    root.id = "gf-mana-pool";
    root.innerHTML =
      '<div class="gf-mp-head">' +
      '  <span class="gf-mp-title">Mana pool</span>' +
      '  <button type="button" class="gf-mp-clear" title="Clear pool">Clear</button>' +
      "</div>" +
      '<div class="gf-mp-pips" id="gf-mp-pips"></div>';
    document.body.appendChild(root);
    document.getElementById("gf-mp-pips").innerHTML = MANA_COLORS.map(
      (c) =>
        '<div class="gf-mp-pip gf-mp-' +
        c.toLowerCase() +
        '" data-color="' +
        c +
        '">' +
        '<span class="gf-mp-sym">' +
        c +
        "</span>" +
        '<div class="gf-mp-btns">' +
        '<button type="button" class="gf-mp-dec" aria-label="Decrement ' +
        MANA_LABELS[c] +
        '">−</button>' +
        '<span class="gf-mp-val" id="gf-mp-val-' +
        c +
        '">0</span>' +
        '<button type="button" class="gf-mp-inc" aria-label="Increment ' +
        MANA_LABELS[c] +
        '">+</button>' +
        "</div>" +
        "</div>"
    ).join("");
    root.querySelector(".gf-mp-clear").addEventListener("click", clearManaPool);
    root.querySelectorAll(".gf-mp-pip").forEach((pip) => {
      const color = pip.dataset.color;
      pip.querySelector(".gf-mp-dec").addEventListener("click", () => adjustMana(color, -1));
      pip.querySelector(".gf-mp-inc").addEventListener("click", () => adjustMana(color, 1));
    });
    renderManaPool();
  }

  function renderManaPool() {
    for (const c of MANA_COLORS) {
      const el = document.getElementById("gf-mp-val-" + c);
      if (el) el.textContent = String(state.manaPool[c]);
    }
  }

  function openManaPicker(anchor) {
    closeManaPicker();
    const picker = document.createElement("div");
    picker.className = "gf-mana-picker";
    picker.innerHTML =
      '<div class="gf-mana-picker-prompt">Add mana:</div>' +
      MANA_COLORS.map(
        (c) =>
          '<button type="button" class="gf-mana-picker-btn gf-mp-' +
          c.toLowerCase() +
          '" data-color="' +
          c +
          '">' +
          c +
          "</button>"
      ).join("") +
      '<button type="button" class="gf-mana-picker-cancel" aria-label="Cancel">×</button>';
    document.body.appendChild(picker);
    // Position near anchor; clamp inside viewport.
    const rect = anchor
      ? anchor.getBoundingClientRect()
      : { left: 100, top: 100, width: 0, height: 0 };
    const pw = picker.offsetWidth;
    const ph = picker.offsetHeight;
    let x = rect.left;
    let y = rect.top + rect.height + 6;
    if (x + pw > window.innerWidth - 12) x = window.innerWidth - pw - 12;
    if (y + ph > window.innerHeight - 12) y = rect.top - ph - 6;
    if (y < 12) y = 12;
    if (x < 12) x = 12;
    picker.style.left = x + "px";
    picker.style.top = y + "px";
    picker.addEventListener("click", (e) => {
      e.stopPropagation();
      const btn = e.target.closest("button");
      if (!btn) return;
      if (btn.classList.contains("gf-mana-picker-cancel")) {
        closeManaPicker();
        return;
      }
      const color = btn.dataset.color;
      if (color) {
        adjustMana(color, 1);
        closeManaPicker();
      }
    });
    state.activeManaPicker = picker;
  }

  function closeManaPicker() {
    if (state.activeManaPicker) {
      state.activeManaPicker.remove();
      state.activeManaPicker = null;
    }
  }

  // ── Opening hand readout ──────────────────────────────────────
  function refreshOpeningHandReadout() {
    const hand = state.hand;
    const count = hand.length;
    let lands = 0;
    let spellCmcSum = 0;
    let spellCmcN = 0;
    for (const inst of hand) {
      if (isLand(inst)) {
        lands++;
      } else if (typeof inst.card.cmc === "number") {
        spellCmcSum += inst.card.cmc;
        spellCmcN++;
      }
    }
    document.getElementById("gf-readout-hand").textContent = count + " cards";
    document.getElementById("gf-readout-lands").textContent = String(lands);
    document.getElementById("gf-readout-avgmv").textContent =
      spellCmcN > 0 ? (spellCmcSum / spellCmcN).toFixed(2) : "—";
  }

  // ── Render ─────────────────────────────────────────────────────
  function render() {
    // v3.30.4 — hide the hand-hover mana-cost overlay defensively at
    // the top of each render(). A card that moves out of the hand
    // mid-hover (drag, click-to-move, mulligan) would otherwise leave
    // the floating pill anchored to a card position that no longer
    // contains that card. Function-declaration hoisting inside this
    // IIFE makes the call safe even when render() runs from boot
    // before the helper's source line is reached.
    hideHandManaCostOverlay();
    document.getElementById("gf-stat-life").textContent = String(state.life);
    document.getElementById("gf-stat-turn").textContent = String(state.turn);
    document.getElementById("gf-count-library").textContent = String(state.library.length);
    document.getElementById("gf-count-hand").textContent = String(state.hand.length);
    document.getElementById("gf-count-graveyard").textContent = String(state.graveyard.length);
    document.getElementById("gf-count-exile").textContent = String(state.exile.length);
    document.getElementById("gf-count-command").textContent = String(state.command.length);

    renderHand();
    renderBattlefieldRegion("creatures", "bf-creatures", state.bfCreatures);
    renderBattlefieldRegion("lands", "bf-lands", state.bfLands);
    renderBattlefieldRegion("artenc", "bf-artenc", state.bfArtEnc);
    renderBattlefieldRegion("pwbattle", "bf-pwbattle", state.bfPwBattle);
    renderBattlefieldRegion("other", "bf-other", state.bfOther);
    renderPile("graveyard", state.graveyard);
    renderPile("exile", state.exile);
    renderPile("command", state.command);
  }

  function renderHand() {
    const strip = document.getElementById("gf-hand-strip");
    strip.innerHTML = "";
    for (const inst of state.hand) strip.appendChild(buildCardEl(inst, "hand"));
  }

  function renderBattlefieldRegion(regionId, zoneKey, list) {
    const row = document.getElementById("gf-region-" + regionId);
    if (!row) return;
    // v3.30.6 — strip BOTH lone .gf-card children and the
    // .gf-card-group wrappers that hosts-with-children produce.
    Array.from(row.querySelectorAll(".gf-card, .gf-card-group")).forEach((n) =>
      n.remove()
    );
    // v3.30.6 — render only TOP-LEVEL cards (`attachedTo` falsy).
    // An attached card is rendered fanned beneath its host (looked
    // up across all five battlefield arrays by childrenOf), so it
    // must be SKIPPED in its own region's top-level iteration. The
    // empty-region class hook tracks top-level count, not raw list
    // length — a region with only attached cards (whose hosts live
    // in a different region) reads as empty here, which is correct:
    // none of its cards render in this region's top level.
    const topLevel = list.filter((inst) => !inst.attachedTo);
    if (topLevel.length === 0) row.classList.add("gf-bf-region-empty");
    else row.classList.remove("gf-bf-region-empty");
    for (const inst of topLevel) {
      const children = childrenOf(inst.id);
      if (children.length === 0) {
        // No attached cards — render the host directly, no group
        // wrapper. Keeps the DOM for a card with no fan
        // byte-equivalent to v3.30.0–v3.30.5 (purely additive
        // change for unattached cards).
        row.appendChild(buildCardEl(inst, zoneKey));
      } else {
        // Has children — wrap host + fan in a .gf-card-group. The
        // group is the flex item; the host sits at the top of the
        // group, children stack beneath with CSS calc()-driven
        // offsets keyed off --gf-attach-index. Host z-index 10;
        // children descend (9, 8, …) so each child's bottom strip
        // peeks out below the next layer up — classic Moxfield /
        // Cockatrice fan.
        const group = document.createElement("div");
        group.className = "gf-card-group";
        group.style.setProperty("--gf-attach-count", String(children.length));
        const hostEl = buildCardEl(inst, zoneKey);
        hostEl.classList.add("gf-card-host");
        group.appendChild(hostEl);
        for (let i = 0; i < children.length; i++) {
          const childEl = buildCardEl(children[i], zoneKey);
          childEl.classList.add("gf-attached-child");
          childEl.style.setProperty("--gf-attach-index", String(i));
          // z-index decreases from 9 → so each subsequent child sits
          // a layer further back. Visual cap: ~9 children fit before
          // z-stack inversion — well above any realistic equipment
          // / aura load for goldfishing.
          childEl.style.zIndex = String(9 - i);
          group.appendChild(childEl);
        }
        row.appendChild(group);
      }
    }
  }

  function renderPile(zoneKey, list) {
    const pile = document.getElementById("gf-pile-" + zoneKey);
    pile.innerHTML = "";
    if (list.length === 0) {
      const empty = document.createElement("div");
      empty.className = "gf-pile-empty";
      empty.textContent = "(empty)";
      pile.appendChild(empty);
    } else {
      const top = list[list.length - 1];
      const el = buildCardEl(top, zoneKey);
      el.classList.add("gf-pile-top");
      pile.appendChild(el);
    }
    const browseBtn = document.createElement("button");
    browseBtn.type = "button";
    browseBtn.className = "gf-btn gf-btn-small gf-pile-browse";
    browseBtn.textContent = "Browse";
    browseBtn.addEventListener("click", () => browseZone(zoneKey, capitalize(zoneKey)));
    pile.appendChild(browseBtn);
  }

  function capitalize(s) {
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function buildCardEl(inst, zone) {
    const el = document.createElement("div");
    el.className = "gf-card";
    el.dataset.instId = inst.id;
    el.dataset.zone = zone;
    if (inst.tapped) el.classList.add("gf-tapped");
    el.setAttribute("tabindex", "0");
    el.setAttribute("draggable", "true");
    const small = (inst.card.image_url || "").replace("/normal/", "/small/");
    if (small) {
      const img = document.createElement("img");
      img.className = "gf-card-img";
      img.loading = "lazy";
      img.alt = inst.card.name || "";
      img.src = small;
      img.onerror = function () {
        img.remove();
        renderFallback(el, inst);
      };
      el.appendChild(img);
    } else {
      renderFallback(el, inst);
    }
    // Touch-friendly kebab path to the menu. Visible always on touch
    // viewports; hover-revealed on pointer-hover viewports.
    const kebab = document.createElement("button");
    kebab.type = "button";
    kebab.className = "gf-kebab";
    kebab.setAttribute("aria-label", "Card actions");
    kebab.textContent = "⋮";
    kebab.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const r = kebab.getBoundingClientRect();
      openContextMenu({ clientX: r.right, clientY: r.bottom }, inst, zone);
    });
    el.appendChild(kebab);
    // v3.30.7 — Token P/T badge. Tokens only, and only when at
    // least one of power / toughness is non-empty. renderFallback
    // does NOT render P/T (out of scope of this release to add it
    // there for non-token cards). The badge sits absolute in the
    // card's top-left so it doesn't collide with the kebab (top-
    // right) or the bottom counter cluster (v3.30.5).
    if (inst.isToken && inst.card && (inst.card.power || inst.card.toughness)) {
      const pt = document.createElement("span");
      pt.className = "gf-pt-badge";
      pt.textContent = (inst.card.power || "") + "/" + (inst.card.toughness || "");
      el.appendChild(pt);
    }
    // v3.30.5 — counter pill cluster, battlefield-only and gated on
    // non-empty counters so cards without annotations render exactly
    // as they did in v3.30.0–v3.30.4. The cluster is a child of
    // .gf-card; CSS positions it absolute at the bottom of the card
    // with a translucent backdrop. Reads use `inst.counters || {}`
    // via buildCounterCluster() for backward-safe access.
    if (isBattlefieldZone(zone)) {
      const cluster = buildCounterCluster(inst);
      if (cluster) el.appendChild(cluster);
    }
    attachCardHandlers(el, inst, zone);
    return el;
  }

  function renderFallback(parent, inst) {
    const f = document.createElement("div");
    f.className = "gf-card-fallback";
    const name = document.createElement("div");
    name.className = "gf-card-fb-name";
    name.textContent = inst.card.name || "(no name)";
    // v3.30.4 — renderManaCost is the ONE cost parser. The text
    // card-face's cost line is now styled pips matching the hand-
    // hover overlay; the prior `textContent = mana_cost` was plain
    // text in v3.30.0–v3.30.3.
    const cost = document.createElement("div");
    cost.className = "gf-card-fb-cost";
    if (inst.card.mana_cost) cost.appendChild(renderManaCost(inst.card.mana_cost));
    const type = document.createElement("div");
    type.className = "gf-card-fb-type";
    type.textContent = inst.card.type_line || "";
    const oracle = document.createElement("div");
    oracle.className = "gf-card-fb-oracle";
    oracle.textContent = inst.card.oracle_text || "";
    f.appendChild(name);
    if (inst.card.mana_cost) f.appendChild(cost);
    f.appendChild(type);
    if (inst.card.oracle_text) f.appendChild(oracle);
    parent.appendChild(f);
  }

  // ── Card interaction (Addition 4 + Fix 1 routing) ─────────────
  /*
   * Left-click semantics — explicit by zone (Add 4):
   *   - battlefield-lands / battlefield-permanents: tap/untap directly
   *     (untapped→tapped on a land also fires the mana picker, Add 5)
   *   - hand: open the context menu (Play is the typical action)
   *   - graveyard / exile / command pile-top: open the context menu
   *   - modal card: open the context menu (modal stays open — Fix 3)
   *   - library card-back: handled separately (left-click draws 1)
   * Right-click any card → preventDefault + open the menu.
   * Touch long-press (≥500ms) → open the menu. Kebab also opens the menu.
   * Drag-start works from every zone; from `zone === "modal"` it ALSO
   * closes the overlay so the drop targets underneath are reachable
   * (Addition 6 drag-out).
   */
  function attachCardHandlers(el, inst, zone) {
    // v3.30.4 — hand-zone cards also fire the mana-cost overlay on
    // hover/focus alongside the side preview. The overlay is a
    // single body-level element repositioned via the card's
    // bounding rect; scoped to the HAND zone only. mouseleave +
    // blur listeners are added only for hand cards since the
    // overlay only ever shows there.
    el.addEventListener("mouseenter", () => {
      setPreview(inst);
      if (zone === "hand") showHandManaCostOverlay(inst, el);
    });
    el.addEventListener("focus", () => {
      setPreview(inst);
      if (zone === "hand") showHandManaCostOverlay(inst, el);
    });
    if (zone === "hand") {
      el.addEventListener("mouseleave", hideHandManaCostOverlay);
      el.addEventListener("blur", hideHandManaCostOverlay);
    }

    el.addEventListener("click", (e) => {
      if (e.target.closest(".gf-kebab")) return; // kebab handles its own click
      e.preventDefault();
      e.stopPropagation();
      setPreview(inst);
      if (isBattlefieldZone(zone)) {
        toggleTap(inst.id, el);
      } else {
        openContextMenu(e, inst, zone);
      }
    });

    el.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      setPreview(inst);
      openContextMenu(e, inst, zone);
    });

    // Touch long-press → menu. (iOS Safari also fires `contextmenu` on
    // long-press, which the handler above catches; the explicit timer
    // covers Android and any other touch path that doesn't synthesize
    // contextmenu.)
    el.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "touch") return;
      state.longPressTimer = setTimeout(() => {
        state.longPressTimer = null;
        const r = el.getBoundingClientRect();
        openContextMenu(
          { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 },
          inst,
          zone
        );
      }, 500);
    });
    const clearLP = () => {
      if (state.longPressTimer) {
        clearTimeout(state.longPressTimer);
        state.longPressTimer = null;
      }
    };
    el.addEventListener("pointerup", clearLP);
    el.addEventListener("pointercancel", clearLP);
    el.addEventListener("pointermove", (e) => {
      if (e.pointerType === "touch") clearLP();
    });

    el.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", inst.id);
      e.dataTransfer.effectAllowed = "move";
      el.classList.add("gf-dragging");
      // Drag-out from modal: close the overlay so drop targets underneath
      // are reachable. The drag operation itself continues — drop fires
      // on the table below.
      if (zone === "modal") closeModal();
    });
    el.addEventListener("dragend", () => el.classList.remove("gf-dragging"));
  }

  function setPreview(inst) {
    const frame = document.getElementById("gf-preview-frame");
    frame.innerHTML = "";
    const large = inst.card.image_url || "";
    if (large) {
      const img = document.createElement("img");
      img.className = "gf-preview-img";
      img.alt = inst.card.name || "";
      img.src = large;
      img.onerror = function () {
        img.remove();
        renderFallback(frame, inst);
      };
      frame.appendChild(img);
    } else {
      renderFallback(frame, inst);
    }
  }

  // ── Mana-cost rendering (v3.30.4) ─────────────────────────────
  // Single parser used by both:
  //   - the hand-hover overlay (showHandManaCostOverlay below — the
  //     Moxfield nicety the tester asked for)
  //   - renderFallback's text card-face cost line (was plain text in
  //     v3.30.0–v3.30.3; now styled pips for consistency)
  // ONE parser, not two — `cost.textContent = inst.card.mana_cost`
  // is GONE from renderFallback as of v3.30.4.
  //
  // Parses `{2}{W}{U}` → three pips in order. Unparseable segments
  // render as a neutral pip carrying the raw text; never crashes.
  // Empty / null mana_cost → empty fragment (caller hides the
  // overlay when the fragment has no children).
  function renderManaCost(manaCostString) {
    const frag = document.createDocumentFragment();
    if (!manaCostString) return frag;
    // Match every {…} segment; ignore anything outside the braces.
    const re = /\{([^}]+)\}/g;
    let m;
    while ((m = re.exec(manaCostString)) !== null) {
      frag.appendChild(buildCostPip(m[1]));
    }
    return frag;
  }

  function buildCostPip(symbol) {
    const pip = document.createElement("span");
    pip.className = "gf-cost-pip";
    const s = String(symbol || "").trim();
    const upper = s.toUpperCase();
    // Numeric generic (any number of digits): 0, 1, 2, ..., 15, 20.
    if (/^\d+$/.test(s)) {
      pip.classList.add("gf-cost-generic");
      pip.textContent = s;
      return pip;
    }
    // Variable cost — X, Y, Z all render in the generic slot.
    if (upper === "X" || upper === "Y" || upper === "Z") {
      pip.classList.add("gf-cost-generic");
      pip.textContent = upper;
      return pip;
    }
    // Single-letter colored — W/U/B/R/G/C.
    if (upper === "W" || upper === "U" || upper === "B" || upper === "R" || upper === "G" || upper === "C") {
      pip.classList.add("gf-cost-" + upper.toLowerCase());
      pip.textContent = upper;
      return pip;
    }
    // Trivial hybrid — `{W/U}` or `{2/W}` etc. Render as one pip with
    // the raw text. Do NOT over-engineer rare symbols (phyrexian,
    // snow, etc.) — they fall through to the neutral pip below.
    if (/^[WUBRGC0-9XYZ]\/[WUBRGC]$/.test(upper)) {
      pip.classList.add("gf-cost-hybrid");
      pip.textContent = upper;
      return pip;
    }
    // Unrecognized — neutral pip carrying the raw text. Catches
    // {S} (snow), {W/P} (phyrexian), {HW} (half-mana), etc.
    pip.classList.add("gf-cost-n");
    pip.textContent = upper;
    return pip;
  }

  // Hand-hover overlay — single body-level element repositioned on each
  // hover via the anchor card's bounding rect. Scoped to the HAND zone
  // only; the side preview pane carries every other zone.
  function showHandManaCostOverlay(inst, anchorEl) {
    if (!inst || !inst.card || !inst.card.mana_cost) {
      hideHandManaCostOverlay();
      return;
    }
    let overlay = document.getElementById("gf-hand-cost-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "gf-hand-cost-overlay";
      overlay.className = "gf-hand-cost-overlay";
      overlay.setAttribute("aria-hidden", "true");
      document.body.appendChild(overlay);
    }
    overlay.innerHTML = "";
    const frag = renderManaCost(inst.card.mana_cost);
    if (!frag.childNodes.length) {
      // mana_cost was a malformed string with no parseable segments —
      // hide the overlay rather than show an empty box.
      hideHandManaCostOverlay();
      return;
    }
    overlay.appendChild(frag);
    const r = anchorEl.getBoundingClientRect();
    // (left, top) anchors at the card's top-center; the CSS rule
    // translate(-50%, -100%) lifts the pill above the card with a
    // small offset.
    overlay.style.left = r.left + r.width / 2 + "px";
    overlay.style.top = r.top - 6 + "px";
    overlay.classList.add("gf-hand-cost-show");
  }

  function hideHandManaCostOverlay() {
    const overlay = document.getElementById("gf-hand-cost-overlay");
    if (overlay) {
      overlay.classList.remove("gf-hand-cost-show");
      overlay.innerHTML = "";
    }
  }

  // ── Counters on battlefield permanents (v3.30.5) ──────────────
  // Free-form annotations the user manages by hand; goldfish is not a
  // rules engine. Six curated labels + a Custom… path covering the
  // typical Commander needs (+1/+1 / -1/-1 / Loyalty / Charge /
  // Experience / Quest). Single mutation site: adjustCounter() is the
  // ONLY function that touches a counters map. Same single-source-of-
  // truth discipline as moveTo / placeOnBattlefield. Touching an
  // instance's counters anywhere else (render code, menu builder, the
  // panel UI) is a regression.
  //
  // The v3.26.5 game-tracker extra-counters subsystem is a styling /
  // UX REFERENCE only — the goldfish surface does NOT import or share
  // tracker code; the two state shapes stay independent. Tracker
  // uses an `extraCounters[]` array per seat; goldfish uses a per-
  // instance `counters: {label → count}` object map. Different
  // problems, different shapes.
  const COUNTER_LABELS = ["+1/+1", "-1/-1", "Loyalty", "Charge", "Experience", "Quest"];

  /**
   * Single mutation site for ANY counters map. Resolves the instance
   * via findInstance, applies the delta (label whitespace-stripped;
   * empty label → no-op), deletes the key at exactly 0 so no zero-
   * count entries linger in the map. Negative counts ARE allowed
   * (the user may want -1/-1 visible as such; this is not a rules
   * engine). Backward-safe: an instance built before v3.30.5 with no
   * `counters` key gets one assigned on first mutation. Re-renders
   * and refreshes the counter panel if it's open against this
   * instance.
   */
  function adjustCounter(instId, label, delta) {
    const found = findInstance(instId);
    if (!found) return;
    const cleanLabel = String(label || "").trim();
    if (!cleanLabel) return;
    const inst = found.inst;
    if (!inst.counters) inst.counters = {};
    const next = (inst.counters[cleanLabel] || 0) + Number(delta || 0);
    if (next === 0) {
      delete inst.counters[cleanLabel];
    } else {
      inst.counters[cleanLabel] = next;
    }
    render();
    // If the editor panel is open against the same instance, refresh
    // its rows so the displayed counts match the new state without
    // forcing a close/reopen.
    if (state.activeCounterPanel && state.activeCounterPanel.instId === instId) {
      refreshCounterPanelBody(state.activeCounterPanel);
    }
  }

  function buildCounterCluster(inst) {
    // Returns null when the instance has no counters — caller skips
    // appending. Battlefield-only check is done at the caller site
    // (buildCardEl) so this helper is reusable if later code wants
    // to render a non-battlefield variant (it currently doesn't).
    const counters = inst.counters || {};
    const keys = Object.keys(counters);
    if (keys.length === 0) return null;
    const cluster = document.createElement("div");
    cluster.className = "gf-counter-cluster";
    cluster.setAttribute("aria-hidden", "true");
    for (const label of keys) {
      const chip = document.createElement("span");
      chip.className = "gf-counter-chip";
      // Tag well-known labels for a tighter color treatment.
      if (label === "+1/+1") chip.classList.add("gf-counter-plus");
      else if (label === "-1/-1") chip.classList.add("gf-counter-minus");
      else if (label === "Loyalty") chip.classList.add("gf-counter-loy");
      chip.textContent = label + " " + counters[label];
      cluster.appendChild(chip);
    }
    return cluster;
  }

  // ── Counter editor panel (v3.30.5) ────────────────────────────
  // Floating panel anchored near a battlefield card, opened via the
  // "Counters…" item in the per-card context menu. Single panel state:
  // state.activeCounterPanel = { node, instId } — set on open, cleared
  // on close. adjustCounter() refreshes the panel body when an update
  // targets the instance the panel is bound to, so the displayed
  // counts stay in sync without requiring the user to close/reopen.
  function openCounterPanel(inst, anchorEl) {
    closeCounterPanel();
    const panel = document.createElement("div");
    panel.className = "gf-counter-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Counters editor");
    // Stop clicks INSIDE the panel from bubbling to the document
    // click-away listener (defined down with the context menu's
    // dismissals). Same shape as .gf-ctx and .gf-mana-picker.
    panel.addEventListener("click", (ev) => ev.stopPropagation());
    document.body.appendChild(panel);
    const head = document.createElement("div");
    head.className = "gf-counter-panel-head";
    head.textContent = "Counters";
    panel.appendChild(head);
    const body = document.createElement("div");
    body.className = "gf-counter-panel-body";
    panel.appendChild(body);
    state.activeCounterPanel = { node: panel, body: body, instId: inst.id };
    refreshCounterPanelBody(state.activeCounterPanel);
    // Position near the anchor card; clamp inside viewport. The panel
    // floats above the card.
    const r = anchorEl.getBoundingClientRect();
    let x = r.left;
    let y = r.bottom + 6;
    const pw = panel.offsetWidth;
    const ph = panel.offsetHeight;
    if (x + pw > window.innerWidth - 12) x = window.innerWidth - pw - 12;
    if (y + ph > window.innerHeight - 12) y = r.top - ph - 6;
    if (x < 12) x = 12;
    if (y < 12) y = 12;
    panel.style.left = x + "px";
    panel.style.top = y + "px";
  }

  function closeCounterPanel() {
    if (state.activeCounterPanel) {
      state.activeCounterPanel.node.remove();
      state.activeCounterPanel = null;
    }
  }

  function refreshCounterPanelBody(panelState) {
    const body = panelState.body;
    const inst = (findInstance(panelState.instId) || {}).inst;
    if (!inst) {
      // Bound instance has vanished (moved zone + card no longer
      // exists, or some other unexpected case). Close gracefully.
      closeCounterPanel();
      return;
    }
    body.innerHTML = "";
    const counters = inst.counters || {};
    // Curated labels first, in their stable display order. Any
    // custom-label counters already on the instance render after
    // the curated rows so the user can still adjust them.
    const customLabels = Object.keys(counters).filter(
      (l) => !COUNTER_LABELS.includes(l)
    );
    const allRows = COUNTER_LABELS.concat(customLabels);
    for (const label of allRows) {
      body.appendChild(buildCounterRow(panelState.instId, label, counters[label] || 0));
    }
    // Custom-counter add affordance.
    const customBtn = document.createElement("button");
    customBtn.type = "button";
    customBtn.className = "gf-counter-custom-btn";
    customBtn.textContent = "+ Custom counter…";
    customBtn.addEventListener("click", () => {
      const raw = window.prompt("New counter label:", "");
      if (raw === null) return; // user cancelled
      const clean = String(raw).trim();
      if (!clean) return; // empty/whitespace ignored per spec
      adjustCounter(panelState.instId, clean, 1);
    });
    body.appendChild(customBtn);
  }

  function buildCounterRow(instId, label, count) {
    const row = document.createElement("div");
    row.className = "gf-counter-row";
    const dec = document.createElement("button");
    dec.type = "button";
    dec.className = "gf-counter-btn gf-counter-dec";
    dec.setAttribute("aria-label", "Decrement " + label);
    dec.textContent = "−";
    dec.addEventListener("click", () => adjustCounter(instId, label, -1));
    const lab = document.createElement("span");
    lab.className = "gf-counter-row-label";
    lab.textContent = label;
    const val = document.createElement("span");
    val.className = "gf-counter-row-val";
    val.textContent = String(count);
    const inc = document.createElement("button");
    inc.type = "button";
    inc.className = "gf-counter-btn gf-counter-inc";
    inc.setAttribute("aria-label", "Increment " + label);
    inc.textContent = "+";
    inc.addEventListener("click", () => adjustCounter(instId, label, 1));
    row.appendChild(dec);
    row.appendChild(lab);
    row.appendChild(val);
    row.appendChild(inc);
    return row;
  }

  // ── Equipment / aura attachment (v3.30.6) ─────────────────────
  // `attachedTo` is a RENDER POINTER, not a state-array move. An
  // attached card stays in its OWN classifyRegion array; only the
  // render nests it beneath the host. Moving a host between regions
  // (via placeOnBattlefield, classifyRegion drift) needs ZERO child
  // bookkeeping — childrenOf scans across all five battlefield
  // arrays and the render follows.
  //
  // ONE-LEVEL rule: attachInstance REJECTS nested or self-referential
  // attachments. A child cannot have children of its own; a host
  // cannot itself be attached. The "Attach to…" picker only lists
  // eligible hosts (via eligibleHostsFor), but attachInstance
  // re-validates defensively so any caller path is safe.
  //
  // Single mutation site — attachInstance + detachInstance are the
  // ONLY user-facing writers of `attachedTo`. Same single-source-of-
  // truth discipline as moveTo / placeOnBattlefield / adjustCounter.
  // moveTo's internal detach-on-leave is the one internal exception:
  // it writes attachedTo plainly inside the same transaction without
  // going through detachInstance (would force redundant per-child
  // renders before the move's own render fires). That's an internal
  // mutation, not a UI path.
  //
  // Backward-safe: a missing `attachedTo` (pre-v3.30.6 instance held
  // by a stale browser tab) reads as falsy, which is the same as
  // null — every read uses truthy-test semantics.

  /** Scan all five battlefield arrays for instances whose
   *  attachedTo matches the given host id. Preserves the
   *  classifyRegion display order (Creatures → Lands → Art/Ench →
   *  PW/Battle → Other). */
  function childrenOf(hostId) {
    const out = [];
    const arrs = [
      state.bfCreatures,
      state.bfLands,
      state.bfArtEnc,
      state.bfPwBattle,
      state.bfOther,
    ];
    for (const list of arrs) {
      for (const inst of list) {
        if (inst.attachedTo === hostId) out.push(inst);
      }
    }
    return out;
  }

  /** Single mutation site for attaching a child to a host. Defensive
   *  validation here — the "Attach to…" picker only shows eligible
   *  hosts but ANY caller path goes through this. Silent no-op on
   *  rejection (not a rules engine; no error UI). */
  function attachInstance(childId, hostId) {
    if (!childId || !hostId || childId === hostId) return;
    const childFound = findInstance(childId);
    const hostFound = findInstance(hostId);
    if (!childFound || !hostFound) return;
    if (!isBattlefieldZone(childFound.zone)) return;
    if (!isBattlefieldZone(hostFound.zone)) return;
    // One-level rule:
    if (childFound.inst.attachedTo) return; // child already attached
    if (hostFound.inst.attachedTo) return; // host itself attached
    if (childrenOf(childId).length > 0) return; // child has children
    childFound.inst.attachedTo = hostId;
    render();
  }

  /** Single mutation site for detaching. Idempotent — no-op if the
   *  instance is already unattached. */
  function detachInstance(childId) {
    const found = findInstance(childId);
    if (!found) return;
    if (!found.inst.attachedTo) return;
    found.inst.attachedTo = null;
    render();
  }

  /** Returns the list of battlefield instances that are eligible to
   *  serve as a host for `childInst`. Empty list → no valid host
   *  available (caller should suppress the "Attach to…" item). */
  function eligibleHostsFor(childInst) {
    if (!childInst) return [];
    // A card with its own children can't itself become attached
    // (one-level rule).
    if (childrenOf(childInst.id).length > 0) return [];
    const out = [];
    const arrs = [
      state.bfCreatures,
      state.bfLands,
      state.bfArtEnc,
      state.bfPwBattle,
      state.bfOther,
    ];
    for (const list of arrs) {
      for (const host of list) {
        if (host.id === childInst.id) continue;
        if (host.attachedTo) continue; // host already attached itself
        out.push(host);
      }
    }
    return out;
  }

  // ── Custom token creation (v3.30.7) ───────────────────────────
  // Tokens reuse the SAME instance shape as payload-built instances
  // — {id, card, tapped, counters, attachedTo, ...} — plus `isToken:
  // true`. Tap/untap (v3.30.0), counters (v3.30.5), and attachment
  // (v3.30.6) all work with ZERO special-casing because nothing in
  // those systems consults `isToken`. This is the whole reason
  // tokens were sequenced last in the v3.30.x followups series; the
  // four prior releases built the substrate.
  //
  // The card object on a token is SYNTHETIC — invented in JS, never
  // touches the payload or InventoryRow. createToken is the ONLY
  // construction site; placeOnBattlefield routes the new instance
  // via classifyRegion so a "Token Creature" lands in Creatures, a
  // "Token Artifact — Treasure" in Artifacts & Enchantments, etc.
  // No parallel placement path.
  //
  // Token ids use the distinct `gf-tok-N` prefix so they never
  // collide with the `gf-N` payload-instance ids. findInstance is
  // fully id-agnostic (scans by `x.id === id` strict equality
  // across all zones), so the prefix split is purely for human
  // readability — no code conditions on it.
  //
  // Lifecycle: a token moved to ANY non-battlefield zone vanishes
  // (the v3.30.7 moveTo extension below). Goldfish convention,
  // matches the MTG game-state rule that tokens cease to exist
  // when they leave the battlefield — except we apply it as a
  // pure storage policy, not as a rules engine; the user moves
  // the token wherever they like, and if it lands off the
  // battlefield it's removed from state.
  let tokenSeq = 1;

  /** Single construction site for token instances. spec carries:
   *  - name (string)
   *  - typeLine (string; default "Token Creature")
   *  - power, toughness (free-text strings; "" if not given)
   *  - colors (string of W/U/B/R/G/C letters, space-separated)
   *  - oracleText (optional, default "")
   *  Builds the synthetic card, constructs the instance with the
   *  full v3.30.5/.6 shape + isToken: true, appends to the library
   *  array (so placeOnBattlefield can find it via findInstance),
   *  then immediately calls placeOnBattlefield which moves it to
   *  the classifyRegion-determined battlefield region via the
   *  existing moveTo path. */
  function createToken(spec) {
    const name = String((spec && spec.name) || "").trim() || "Token";
    const typeLine = String((spec && spec.typeLine) || "").trim() || "Token Creature";
    const power = String((spec && spec.power) || "").trim();
    const toughness = String((spec && spec.toughness) || "").trim();
    const colors = String((spec && spec.colors) || "").trim();
    const oracleText = String((spec && spec.oracleText) || "");
    // v3.30.8 — optional Scryfall-cached image URL. The v3.30.7
    // custom-token path leaves this undefined → falls through to null
    // (the v3.30.7 fallback that paints renderFallback's text card-
    // face). The v3.30.8 quick-add path passes spec.imageUrl pulled
    // from the joined TokenInventory row in the payload, giving the
    // token full Scryfall art via the browser-side image fetch (the
    // v3.26.1 precedent). Single construction site is preserved —
    // this is a NEW SPEC FIELD, not a new createToken variant.
    const imageUrl = (spec && spec.imageUrl) || null;
    const card = {
      name: name,
      // type_line drives classifyRegion + isLand + renderFallback.
      // Default "Token Creature" routes to Creatures via the v3.30.1
      // priority Creature → PW/Battle → Art/Ench → Land → Other.
      type_line: typeLine,
      // Tokens have no cost. Empty mana_cost is handled gracefully
      // by renderManaCost (returns empty fragment, the hand-hover
      // overlay short-circuits, the text card-face hides the cost
      // line) and by classifyRegion (doesn't read mana_cost).
      mana_cost: "",
      cmc: 0,
      // image_url: null → renderFallback paints the text card-face
      // (v3.30.7 default behavior). v3.30.8 quick-add passes a
      // Scryfall-cached URL via spec.imageUrl so detected tokens
      // get full art via the browser-side image fetch. Either way
      // is fine; the existing buildCardEl image-vs-fallback
      // dispatch consumes the field uniformly.
      image_url: imageUrl,
      oracle_text: oracleText,
      colors: colors,
      // color_identity isn't read by any goldfish render path today,
      // but include it for symmetry with the payload card shape so
      // future code that touches it doesn't have to special-case
      // tokens.
      color_identity: colors,
      // P/T fields — token-only, rendered by buildCardEl's
      // .gf-pt-badge below when isToken AND (power || toughness)
      // is non-empty. renderFallback does NOT consume these today;
      // adding P/T to the fallback card face is out of scope.
      power: power,
      toughness: toughness,
      // Backward-compat fields a non-token payload card carries —
      // tokens fill with safe defaults so any future code that
      // reads them won't crash.
      set_code: "",
      collector_number: "",
    };
    const inst = {
      id: "gf-tok-" + tokenSeq++,
      card: card,
      tapped: false,
      counters: {},
      attachedTo: null,
      // v3.30.7 — isToken flag. The lifecycle rule in moveTo reads
      // this; render code does NOT need to. Counters / attachment /
      // tap / drag-drop / context menu all work without consulting
      // it (that's the design: tokens inherit the substrate).
      isToken: true,
    };
    // Land in library temporarily so findInstance / placeOnBattlefield
    // can resolve the instance. placeOnBattlefield → moveTo → the
    // classifyRegion-determined battlefield region in a single
    // synchronous transition. The library momentarily holds the
    // token between push and place; no render fires between the two
    // (placeOnBattlefield's moveTo call is the first render).
    state.library.push(inst);
    placeOnBattlefield(inst.id);
    return inst;
  }

  // ── Deck-token quick-add (v3.30.8) ────────────────────────────
  // Surfaces the user's curated DeckTokenRequirement rows for this
  // deck (read in the payload builder from a local SQLite query) as
  // one-click quick-add buttons near the v3.30.7 Create token
  // control. Clicking a button feeds a v3.30.7-shaped spec into the
  // EXISTING createToken() — single construction site preserved.
  // The ONLY difference from a custom token is the source of the
  // spec (the curated payload entry instead of the form).
  //
  // Truthful framing: this surfaces USER-CURATED requirements
  // (added via the deck's existing token-requirements UI), NOT
  // oracle-text auto-extracted ones. The "auto-detect" working
  // name in the v3.30.x series referred to the playtester
  // automatically surfacing what the user had already declared,
  // not to oracle-text parsing.

  /** Thin wrapper — builds a v3.30.7-shaped spec from a detected-
   *  token payload entry and calls createToken. Each click creates
   *  ONE token instance; a quantity_needed of 10 means the button
   *  surfaces "Add Pest × 10" but each click adds one — the user
   *  clicks N times as they need them. (Loop-on-click is an option
   *  but tester feedback can request it as a separate item; this
   *  release ships the one-click-one-token shape.) */
  function quickAddDetectedToken(detected) {
    if (!detected) return null;
    const spec = {
      name: detected.token_name,
      // TokenInventory.type_line drives the classifyRegion bucket;
      // a loose name-only requirement (no token_inventory link)
      // falls back to "Token Creature" — same default as the
      // v3.30.7 custom-token form.
      typeLine: detected.type_line || "Token Creature",
      // TokenInventory does not store P/T or colors today. Empty
      // strings → no P/T badge renders (v3.30.7's gating), no
      // colors are stored.
      power: "",
      toughness: "",
      colors: "",
      oracleText: "",
      // v3.30.8 — the new spec field. Scryfall-cached image URL
      // pulled from the joined TokenInventory.image_url. null when
      // loose name-only requirement → renderFallback text card-face.
      imageUrl: detected.image_url || null,
    };
    return createToken(spec);
  }

  /** Render the quick-add panel from payload.tokens. Empty list →
   *  no panel renders at all (clean empty state; no empty box).
   *  Runs once at boot from the payload; not re-rendered on state
   *  changes since the requirements list is immutable for the
   *  session. */
  function renderQuickAddTokensPanel() {
    const container = document.getElementById("gf-quick-tokens");
    if (!container) return;
    const tokens = (payload && payload.tokens) || [];
    if (tokens.length === 0) {
      container.style.display = "none";
      return;
    }
    container.innerHTML = "";
    const label = document.createElement("span");
    label.className = "gf-quick-tokens-label";
    label.textContent = "Quick-add:";
    container.appendChild(label);
    for (const detected of tokens) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "gf-btn gf-quick-token-btn";
      const qty = Number(detected.quantity_needed || 1);
      btn.textContent =
        "+ " + (detected.token_name || "Token") + (qty > 1 ? " × " + qty : "");
      btn.title =
        "Create a " +
        (detected.token_name || "Token") +
        " token. Click again to add another.";
      btn.addEventListener("click", () => quickAddDetectedToken(detected));
      container.appendChild(btn);
    }
  }

  // ── Context menu (Fix 2 + Fix 3) ──────────────────────────────
  function closeContextMenu() {
    if (state.activeMenu) {
      state.activeMenu.remove();
      state.activeMenu = null;
    }
  }
  // Spec name alias.
  const closeMenus = closeContextMenu; // eslint-disable-line no-unused-vars

  function openContextMenu(e, inst, zone) {
    closeContextMenu();
    const items = buildMenuItems(inst, zone);
    if (items.length === 0) return;
    openMenuFromItems(e, items);
  }

  // v3.30.6 — extracted from openContextMenu so other menu paths
  // (e.g. the host-picker spawned by "Attach to…") can reuse the
  // exact same .gf-ctx render + dismissal contract without
  // duplicating the construction code. Item actions receive a
  // `coord` argument carrying the click event's clientX/clientY —
  // pre-v3.30.6 items ignore the arg, v3.30.6 sub-flows use it to
  // anchor the spawned sub-menu near the parent.
  function openMenuFromItems(coord, items) {
    closeContextMenu();
    const m = document.createElement("div");
    m.className = "gf-ctx";
    m.setAttribute("role", "menu");
    for (const it of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "gf-ctx-item";
      b.textContent = it.label;
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        closeContextMenu();
        // Pass the click coord through so sub-flows (Attach to…)
        // can anchor their spawned picker near the parent menu's
        // click position. Old-style items that take no argument
        // ignore it harmlessly.
        it.action({ clientX: ev.clientX, clientY: ev.clientY });
      });
      m.appendChild(b);
    }
    // Fix 3: clicks INSIDE the menu must not bubble to the document click-
    // away handler (which would close the menu immediately) and must not
    // reach the modal-overlay click handler (which would dismiss the
    // modal). stopPropagation on the menu element catches both.
    m.addEventListener("click", (ev) => ev.stopPropagation());
    document.body.appendChild(m);
    const mw = m.offsetWidth;
    const mh = m.offsetHeight;
    let x = (coord && coord.clientX) || 100;
    let y = (coord && coord.clientY) || 100;
    if (x + mw > window.innerWidth - 12) x = window.innerWidth - mw - 12;
    if (y + mh > window.innerHeight - 12) y = window.innerHeight - mh - 12;
    if (x < 12) x = 12;
    if (y < 12) y = 12;
    m.style.left = x + "px";
    m.style.top = y + "px";
    state.activeMenu = m;
  }

  // v3.30.6 — "Attach to…" sub-flow. Closes the current per-card menu
  // (already done by the openMenuFromItems → closeContextMenu chain)
  // and opens a second .gf-ctx listing eligible hosts. Picking one
  // calls attachInstance which re-validates defensively.
  function openHostPicker(childInst, coord) {
    const hosts = eligibleHostsFor(childInst);
    if (hosts.length === 0) return;
    const items = hosts.map((host) => ({
      label: (host.card && host.card.name) || "(unnamed)",
      action: () => attachInstance(childInst.id, host.id),
    }));
    openMenuFromItems(coord, items);
  }

  function buildMenuItems(inst, zone) {
    const items = [];
    if (zone === "hand") {
      items.push({
        label: isLand(inst) ? "Play (to lands)" : "Play",
        action: () => playFromHand(inst.id),
      });
    }
    if (isBattlefieldZone(zone)) {
      items.push({
        label: inst.tapped ? "Untap" : "Tap",
        action: () => toggleTap(inst.id),
      });
      // v3.30.5 — Counters… opens the floating editor panel anchored
      // to the same card. Battlefield-only — counters render and
      // edit only for cards in the five battlefield regions, never
      // for hand / library / piles / modal.
      items.push({
        label: "Counters…",
        action: () => {
          const cardEl = document.querySelector(
            '.gf-card[data-inst-id="' + inst.id + '"]'
          );
          openCounterPanel(inst, cardEl || document.body);
        },
      });
      // v3.30.6 — attachment paths. An attached card gets a Detach
      // item; an unattached card with eligible hosts gets an Attach
      // to… item that spawns a sub-menu listing the hosts. The
      // "no eligible hosts" case (lone card on battlefield, or a
      // card with its own children — one-level rule) suppresses
      // the Attach item entirely so the menu doesn't carry a dead
      // option.
      if (inst.attachedTo) {
        items.push({
          label: "Detach",
          action: () => detachInstance(inst.id),
        });
      } else if (eligibleHostsFor(inst).length > 0) {
        items.push({
          label: "Attach to…",
          action: (coord) => openHostPicker(inst, coord),
        });
      }
    }
    // Single "Move → Battlefield" — placement routes via classifyRegion,
    // so the user never picks a battlefield region directly. v3.30.1
    // expansion of v3.30.0 Fix 1's single-placement discipline.
    const moveTargets = [
      { z: "hand", label: "Move → Hand" },
      { z: "battlefield", label: "Move → Battlefield" },
      { z: "graveyard", label: "Move → Graveyard" },
      { z: "exile", label: "Move → Exile" },
      { z: "command", label: "Move → Command" },
      { z: "library", label: "Move → Library (top)" },
    ];
    for (const t of moveTargets) {
      if (t.z === "battlefield" && isBattlefieldZone(zone)) continue;
      if (t.z === zone) continue;
      items.push({
        label: t.label,
        action: () => {
          if (t.z === "battlefield") placeOnBattlefield(inst.id);
          else moveTo(inst.id, t.z, t.z === "library" ? { position: "top" } : {});
        },
      });
    }
    items.push({
      label: "Send to library bottom",
      action: () => moveTo(inst.id, "library", { position: "bottom" }),
    });
    return items;
  }

  // Document-level dismissals.
  document.addEventListener("click", (e) => {
    if (state.activeMenu && !state.activeMenu.contains(e.target)) {
      closeContextMenu();
    }
    if (state.activeManaPicker && !state.activeManaPicker.contains(e.target)) {
      closeManaPicker();
    }
    if (
      state.activeCounterPanel &&
      !state.activeCounterPanel.node.contains(e.target)
    ) {
      closeCounterPanel();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeContextMenu();
      closeManaPicker();
      closeCounterPanel();
      closeModal();
    }
  });

  // ── Drag/drop onto zones (Fix 1 — one placement function) ─────
  function wireDropTargets() {
    document.querySelectorAll("[data-zone-drop]").forEach((t) => {
      t.addEventListener("dragover", (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        t.classList.add("gf-drop-hover");
      });
      t.addEventListener("dragleave", () => t.classList.remove("gf-drop-hover"));
      t.addEventListener("drop", (e) => {
        e.preventDefault();
        t.classList.remove("gf-drop-hover");
        const id = e.dataTransfer.getData("text/plain");
        if (!id) return;
        const target = t.dataset.zoneDrop;
        // Any battlefield drop — lands row, permanents row, or a
        // generic "battlefield" zone — routes via kindOf. Visual sub-
        // element does NOT override the card's type.
        // v3.30.1 — any battlefield drop converges through placeOnBattlefield
        // → classifyRegion. The "battlefield" key is the canonical drop-
        // target for the whole battlefield container and every region row.
        // Legacy "battlefield-lands" / "battlefield-permanents" keys from
        // v3.30.0 markup re-route through the same classifier so stale
        // tabs running against a fresh deploy degrade cleanly.
        if (
          target === "battlefield" ||
          target === "battlefield-lands" ||
          target === "battlefield-permanents"
        ) {
          placeOnBattlefield(id);
        } else {
          moveTo(id, target);
        }
      });
    });
  }
  wireDropTargets();

  // Library card-back: left-click draws 1. Browse opens via the zone-head.
  const libBack = document.getElementById("gf-card-back-library");
  libBack.addEventListener("click", () => drawN(1));
  libBack.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      drawN(1);
    }
  });

  // ── Modal (Fix 3 + Addition 6) ────────────────────────────────
  const modal = document.getElementById("gf-modal");
  const modalBody = document.getElementById("gf-modal-body");
  const modalTitle = document.getElementById("gf-modal-title");
  document.getElementById("gf-modal-close").addEventListener("click", closeModal);
  // Overlay click-to-dismiss. event.target === modal is the overlay-only
  // surface (not the modal card content); the body-level .gf-ctx menu is
  // never a descendant of `modal`, so menu-item clicks naturally never
  // reach this handler — no ambiguity, no special case needed.
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeModal();
  });

  function closeModal() {
    if (modal.hidden) return;
    modal.hidden = true;
    modalBody.innerHTML = "";
    state.modalContext = null;
    // Library Browse closing → reshuffle. The flag prevents double-fire
    // on paths that call closeModal twice (drag-out + later modal Close).
    if (state.libraryBrowseOpen) {
      state.libraryBrowseOpen = false;
      shuffle(state.library);
      render();
    }
  }

  function openModal(title, instances) {
    modalTitle.textContent = title;
    modalBody.innerHTML = "";
    if (instances.length === 0) {
      const e = document.createElement("div");
      e.className = "gf-modal-empty";
      e.textContent = "(empty)";
      modalBody.appendChild(e);
    } else {
      const grid = document.createElement("div");
      grid.className = "gf-modal-grid";
      for (const inst of instances) grid.appendChild(buildCardEl(inst, "modal"));
      modalBody.appendChild(grid);
    }
    modal.hidden = false;
  }

  /**
   * Re-render the modal body from the current zone state. Called after
   * every moveTo so a card moved via a menu action visually leaves the
   * modal. The look-at-top-N modal is a deliberate snapshot and is NOT
   * refreshed.
   */
  function refreshModalIfOpen() {
    if (modal.hidden) return;
    if (!state.modalContext) return;
    const { kind, zone } = state.modalContext;
    let instances;
    if (kind === "browse-library") {
      instances = libraryBrowseInstances();
    } else if (kind === "browse" && zone) {
      instances = ZONE_LISTS[zone]();
    } else {
      return;
    }
    modalBody.innerHTML = "";
    if (instances.length === 0) {
      const e = document.createElement("div");
      e.className = "gf-modal-empty";
      e.textContent = "(empty)";
      modalBody.appendChild(e);
    } else {
      const grid = document.createElement("div");
      grid.className = "gf-modal-grid";
      for (const inst of instances) grid.appendChild(buildCardEl(inst, "modal"));
      modalBody.appendChild(grid);
    }
  }

  // ── Create-token form (v3.30.7) ───────────────────────────────
  // Reuses the existing .gf-modal as a lightweight popover by
  // setting state.modalContext.kind = "create-token". closeModal
  // handles the close + state-clear; refreshModalIfOpen early-
  // returns on the "create-token" kind (it only knows browse /
  // browse-library / look). No new modal system invented.
  //
  // The form is JS-built (consistent with how the v3.30.5
  // .gf-counter-panel and v3.30.6 .gf-ctx menus are built) — no
  // hardcoded markup in goldfish.html beyond the trigger button.
  function openCreateTokenForm() {
    closeContextMenu();
    closeCounterPanel();
    closeManaPicker();
    state.modalContext = { kind: "create-token" };
    modalTitle.textContent = "Create token";
    modalBody.innerHTML = "";
    const form = document.createElement("div");
    form.className = "gf-create-token-form";
    form.innerHTML = ""
      + '<label class="gf-ctf-row"><span class="gf-ctf-label">Name</span>'
      + '<input type="text" id="gf-ctf-name" class="gf-ctf-input" placeholder="Token"></label>'
      + '<label class="gf-ctf-row"><span class="gf-ctf-label">Type line</span>'
      + '<input type="text" id="gf-ctf-type" class="gf-ctf-input" value="Token Creature"></label>'
      + '<div class="gf-ctf-row gf-ctf-pt-row">'
      + '<span class="gf-ctf-label">P / T</span>'
      + '<input type="text" id="gf-ctf-power" class="gf-ctf-input gf-ctf-pt" placeholder="1">'
      + '<span class="gf-ctf-pt-sep">/</span>'
      + '<input type="text" id="gf-ctf-toughness" class="gf-ctf-input gf-ctf-pt" placeholder="1">'
      + '</div>'
      + '<div class="gf-ctf-row gf-ctf-colors-row">'
      + '<span class="gf-ctf-label">Colors</span>'
      + '<div class="gf-ctf-colors" id="gf-ctf-colors">'
      + ["W", "U", "B", "R", "G", "C"].map(
          (c) =>
            '<button type="button" class="gf-ctf-color gf-mp-' +
            c.toLowerCase() +
            '" data-color="' +
            c +
            '" aria-pressed="false">' +
            c +
            "</button>"
        ).join("")
      + '</div></div>'
      + '<label class="gf-ctf-row gf-ctf-oracle-row">'
      + '<span class="gf-ctf-label">Oracle text</span>'
      + '<textarea id="gf-ctf-oracle" class="gf-ctf-input gf-ctf-oracle" rows="2" placeholder="Optional"></textarea>'
      + '</label>'
      + '<div class="gf-ctf-row gf-ctf-actions">'
      + '<button type="button" id="gf-ctf-cancel" class="gf-btn">Cancel</button>'
      + '<button type="button" id="gf-ctf-create" class="gf-btn gf-btn-primary">Create</button>'
      + '</div>';
    modalBody.appendChild(form);
    modal.hidden = false;
    // Color toggle handlers — aria-pressed reflects state, .selected
    // class drives the CSS lit-up treatment.
    form.querySelectorAll(".gf-ctf-color").forEach((btn) => {
      btn.addEventListener("click", () => {
        const pressed = btn.getAttribute("aria-pressed") === "true";
        btn.setAttribute("aria-pressed", String(!pressed));
        btn.classList.toggle("gf-ctf-color-on", !pressed);
      });
    });
    document.getElementById("gf-ctf-cancel").addEventListener("click", closeModal);
    document.getElementById("gf-ctf-create").addEventListener("click", () => {
      const colorButtons = form.querySelectorAll('.gf-ctf-color[aria-pressed="true"]');
      const colorLetters = Array.from(colorButtons).map((b) => b.dataset.color);
      const spec = {
        name: document.getElementById("gf-ctf-name").value,
        typeLine: document.getElementById("gf-ctf-type").value,
        power: document.getElementById("gf-ctf-power").value,
        toughness: document.getElementById("gf-ctf-toughness").value,
        colors: colorLetters.join(" "),
        oracleText: document.getElementById("gf-ctf-oracle").value,
      };
      createToken(spec);
      closeModal();
    });
    // Auto-focus Name so the user can start typing immediately.
    const nameInput = document.getElementById("gf-ctf-name");
    if (nameInput) nameInput.focus();
  }

  // ── Controls wiring ───────────────────────────────────────────
  document.getElementById("gf-btn-new-game").addEventListener("click", newGame);
  document.getElementById("gf-btn-new-turn").addEventListener("click", newTurn);
  document.getElementById("gf-btn-draw").addEventListener("click", () => drawN(1));
  document.getElementById("gf-btn-untap-all").addEventListener("click", untapAll);
  document.getElementById("gf-btn-mulligan").addEventListener("click", mulligan);
  document.getElementById("gf-btn-shuffle").addEventListener("click", () => {
    shuffle(state.library);
    render();
  });
  // v3.30.7 — Create token opens the in-modal form. The button is
  // a flat sibling of New game / New turn / Draw / etc.; the form
  // body is JS-built inside the existing .gf-modal.
  document.getElementById("gf-btn-create-token").addEventListener("click", openCreateTokenForm);
  document.getElementById("gf-btn-mill").addEventListener("click", () => {
    const n = parseInt(document.getElementById("gf-input-mill").value, 10) || 1;
    millN(Math.max(1, Math.min(100, n)));
  });
  document.getElementById("gf-btn-look").addEventListener("click", () => {
    const n = parseInt(document.getElementById("gf-input-look").value, 10) || 3;
    lookAtTopN(Math.max(1, Math.min(20, n)));
  });
  document.querySelectorAll("[data-life-delta]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const d = parseInt(btn.dataset.lifeDelta, 10) || 0;
      state.life += d;
      render();
    });
  });

  // Pile zone-heads — click to browse. Library uses the special browseLibrary
  // (sorted, reshuffles on close); others use the simple browse.
  document.querySelectorAll(".gf-zone-pile .gf-zone-head").forEach((h) => {
    h.style.cursor = "pointer";
    h.addEventListener("click", () => {
      const z = h.closest(".gf-zone").dataset.zone;
      if (z === "library") browseLibrary();
      else browseZone(z, capitalize(z));
    });
  });

  // ── Fullscreen shell wiring (Change A) ────────────────────────
  // Lock page scroll; the .gf-app overlay covers sidebar+topbar via
  // position:fixed inset:0 z-index:500. Cleared on pagehide so a
  // browser back-nav / tab-close / reload doesn't leave the body in
  // gf-mode for any later in-tab navigation.
  document.body.classList.add("gf-mode");
  window.addEventListener("pagehide", () => {
    document.body.classList.remove("gf-mode");
  });

  // Fullscreen toggle — Fullscreen API on the .gf-app element so the
  // playtester goes truly edge-to-edge. Mirrors the v3.28.13 tracker
  // ⛶ button shape.
  const fsBtn = document.getElementById("gf-btn-fullscreen");
  const appEl = document.getElementById("gf-app");
  function isFullscreen() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }
  function refreshFsBtnGlyph() {
    if (fsBtn) fsBtn.textContent = isFullscreen() ? "⊠" : "⛶";
  }
  if (fsBtn && appEl) {
    fsBtn.addEventListener("click", () => {
      if (isFullscreen()) {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      } else {
        (appEl.requestFullscreen || appEl.webkitRequestFullscreen).call(appEl);
      }
    });
    document.addEventListener("fullscreenchange", refreshFsBtnGlyph);
    document.addEventListener("webkitfullscreenchange", refreshFsBtnGlyph);
  }

  // ── Draw-on-new-turn preference (v3.30.3) ─────────────────────
  // Read the persisted preference and sync the checkbox + state.
  // Boot only UNCHECKS the box when localStorage explicitly holds
  // "false"; otherwise the box stays checked (which matches the
  // template's `checked` default, so no-JS / pre-boot state already
  // matches the runtime default). Wrapped in try/catch — private-
  // browsing localStorage can throw on read; degrade to default-on
  // with an in-session-only toggle, never crash boot.
  const autoDrawToggle = document.getElementById("gf-autodraw-toggle");
  try {
    if (window.localStorage && localStorage.getItem("cartarch-goldfish-autodraw") === "false") {
      state.autoDrawOnTurn = false;
      if (autoDrawToggle) autoDrawToggle.checked = false;
    }
  } catch (e) {
    /* localStorage disabled (private mode) — fall back to default-on */
  }
  if (autoDrawToggle) {
    autoDrawToggle.addEventListener("change", function () {
      state.autoDrawOnTurn = !!autoDrawToggle.checked;
      try {
        if (window.localStorage) {
          localStorage.setItem(
            "cartarch-goldfish-autodraw",
            state.autoDrawOnTurn ? "true" : "false"
          );
        }
      } catch (e) {
        /* localStorage disabled — toggle still works for the session */
      }
    });
  }

  // ── Boot ──────────────────────────────────────────────────────
  buildManaPoolWidget();
  newGame();
  // v3.30.8 — render the deck-token quick-add buttons from
  // payload.tokens. Runs after newGame() (which has no dependency
  // on the quick-add panel); the buttons live in the controls row
  // and are independent of zone state, so a single boot-time
  // render is enough — they don't refresh on state changes.
  renderQuickAddTokensPanel();
})();
