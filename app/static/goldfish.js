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
    libraryBrowseOpen: false,
    modalContext: null,
    longPressTimer: null,
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
    found.list.splice(found.idx, 1);
    const dest = ZONE_LISTS[targetZone]();
    found.inst.tapped = false;
    if (targetZone === "library") {
      if (opts.position === "bottom") dest.push(found.inst);
      else dest.unshift(found.inst);
    } else {
      dest.push(found.inst);
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
    drawN(1);
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
    Array.from(row.querySelectorAll(".gf-card")).forEach((n) => n.remove());
    if (list.length === 0) row.classList.add("gf-bf-region-empty");
    else row.classList.remove("gf-bf-region-empty");
    for (const inst of list) row.appendChild(buildCardEl(inst, zoneKey));
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
    attachCardHandlers(el, inst, zone);
    return el;
  }

  function renderFallback(parent, inst) {
    const f = document.createElement("div");
    f.className = "gf-card-fallback";
    const name = document.createElement("div");
    name.className = "gf-card-fb-name";
    name.textContent = inst.card.name || "(no name)";
    const cost = document.createElement("div");
    cost.className = "gf-card-fb-cost";
    cost.textContent = inst.card.mana_cost || "";
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
    el.addEventListener("mouseenter", () => setPreview(inst));
    el.addEventListener("focus", () => setPreview(inst));

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
        it.action();
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
    let x = e.clientX || 100;
    let y = e.clientY || 100;
    if (x + mw > window.innerWidth - 12) x = window.innerWidth - mw - 12;
    if (y + mh > window.innerHeight - 12) y = window.innerHeight - mh - 12;
    if (x < 12) x = 12;
    if (y < 12) y = 12;
    m.style.left = x + "px";
    m.style.top = y + "px";
    state.activeMenu = m;
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
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeContextMenu();
      closeManaPicker();
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

  // ── Boot ──────────────────────────────────────────────────────
  buildManaPoolWidget();
  newGame();
})();
