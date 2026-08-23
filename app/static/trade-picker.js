/* Shared trade-picker engine (#184).
 *
 * Data-attribute contract — the SERVER decides everything; this file only
 * remembers what you picked and adds it up.
 *
 *   Container (one per side):
 *     data-pick-grid="offered|requested"   which side of the trade this is
 *
 *   Tile (inside a grid):
 *     data-pick-kind="inventory_row_id|showcase_item_id|trade_item_id"
 *     data-pick-id="<int>"        the id, in the space that kind names
 *     data-pick-alt="<kind>:<id>" OPTIONAL second identity sent alongside the
 *                                 first (a curated card carries its showcase
 *                                 item so the trade keeps its provenance link)
 *     data-available="<int>"      cap for the quantity control
 *     data-name / data-price / data-proxy="1|0" / data-meta
 *
 *   Elsewhere on the page:
 *     #<side>-json         hidden input the serialized picks land in
 *     [data-pick-tray="<side>"]   where the picked cards are listed
 *     [data-pick-total="offered|requested|diff"]  balance readouts (optional)
 *
 * WHY A PICK CARRIES ITS OWN DATA. The picker is paged and searched
 * server-side, so the tile you picked from is usually GONE by the time the
 * trade is submitted. The old version totalled the two sides by looking each
 * picked id up in the DOM and reading the price off the tile — correct only
 * while every card was rendered, and silently wrong the moment they were not: a
 * picked card would drop out of the balance while staying in the trade. Every
 * value the engine needs is copied into the selection at pick time.
 *
 * Used by: trade_new.html (construction), trade_counter.html (counter editor).
 */
(function () {
  "use strict";

  function moneyText(n) {
    return "$" + (Math.round(n * 100) / 100).toFixed(2);
  }

  function readTile(tile) {
    return {
      kind: tile.getAttribute("data-pick-kind"),
      id: parseInt(tile.getAttribute("data-pick-id"), 10),
      alt: tile.getAttribute("data-pick-alt") || "",
      available: parseInt(tile.getAttribute("data-available"), 10) || 1,
      name: tile.getAttribute("data-name") || "Card",
      price: parseFloat(tile.getAttribute("data-price")) || 0,
      proxy: tile.getAttribute("data-proxy") === "1",
      meta: tile.getAttribute("data-meta") || "",
    };
  }

  function keyOf(kind, id) {
    return kind + ":" + id;
  }

  function Picker(side) {
    this.side = side;
    this.picks = new Map(); // "<kind>:<id>" -> {kind,id,alt,name,price,proxy,meta,quantity,available}
    this.jsonInput = document.getElementById(side + "-json");
    this.tray = document.querySelector('[data-pick-tray="' + side + '"]');
  }

  Picker.prototype.add = function (tile) {
    var d = readTile(tile);
    if (!d.id) return;
    d.quantity = Math.min(1, d.available);
    this.picks.set(keyOf(d.kind, d.id), d);
  };

  Picker.prototype.remove = function (kind, id) {
    this.picks.delete(keyOf(kind, id));
  };

  Picker.prototype.has = function (kind, id) {
    return this.picks.has(keyOf(kind, id));
  };

  Picker.prototype.total = function () {
    var sum = 0;
    this.picks.forEach(function (p) {
      sum += (p.proxy ? 0 : p.price) * (p.quantity || 1);
    });
    return sum;
  };

  Picker.prototype.anyProxy = function () {
    var hit = false;
    this.picks.forEach(function (p) {
      if (p.proxy) hit = true;
    });
    return hit;
  };

  Picker.prototype.serialize = function () {
    var out = [];
    this.picks.forEach(function (p) {
      var entry = { quantity: p.quantity || 1 };
      entry[p.kind] = p.id;
      if (p.alt) {
        var bits = p.alt.split(":");
        if (bits.length === 2 && bits[1]) entry[bits[0]] = parseInt(bits[1], 10);
      }
      out.push(entry);
    });
    return out;
  };

  // The tray is the ONLY place a pick is guaranteed to be visible: its tile may
  // be on another page or behind a different search. Paging a picker without
  // this is how a card stays in a trade nobody can see.
  Picker.prototype.paintTray = function () {
    if (!this.tray) return;
    var self = this;
    this.tray.innerHTML = "";
    if (this.picks.size === 0) {
      var empty = document.createElement("p");
      empty.className = "muted trade-tray-empty";
      empty.textContent = "Nothing picked yet.";
      this.tray.appendChild(empty);
      return;
    }
    this.picks.forEach(function (p, key) {
      var row = document.createElement("div");
      row.className = "trade-tray-row";

      var name = document.createElement("span");
      name.className = "trade-tray-name";
      name.textContent = p.name;
      row.appendChild(name);

      if (p.meta) {
        var meta = document.createElement("span");
        meta.className = "trade-tray-meta";
        meta.textContent = p.meta;
        row.appendChild(meta);
      }

      var qty = document.createElement("input");
      qty.type = "number";
      qty.min = "1";
      qty.max = String(p.available);
      qty.value = String(p.quantity);
      qty.className = "trade-tray-qty";
      qty.setAttribute("aria-label", "Quantity of " + p.name);
      qty.addEventListener("change", function () {
        var v = Math.max(1, Math.min(p.available, parseInt(qty.value, 10) || 1));
        qty.value = String(v);
        p.quantity = v;
        self.refresh();
      });
      row.appendChild(qty);

      var price = document.createElement("span");
      price.className = "trade-tray-price";
      price.textContent = p.proxy ? "$0.00" : moneyText(p.price * p.quantity);
      row.appendChild(price);

      var drop = document.createElement("button");
      drop.type = "button";
      drop.className = "btn-danger-small trade-tray-remove";
      drop.textContent = "Remove";
      drop.addEventListener("click", function () {
        self.picks.delete(key);
        self.refresh();
      });
      row.appendChild(drop);

      self.tray.appendChild(row);
    });
  };

  // Paint the tiles that happen to be on screen. Called after every HTMX swap,
  // because a freshly-fetched page knows nothing about what was already picked.
  Picker.prototype.paintTiles = function () {
    var self = this;
    var grid = document.querySelector('[data-pick-grid="' + this.side + '"]');
    if (!grid) return;
    grid.querySelectorAll(".trade-pick-item").forEach(function (tile) {
      var kind = tile.getAttribute("data-pick-kind");
      var id = parseInt(tile.getAttribute("data-pick-id"), 10);
      var on = self.has(kind, id);
      var btn = tile.querySelector(".js-pick-toggle");
      tile.classList.toggle("is-picked", on);
      if (btn) btn.textContent = on ? "Remove" : "Add";
    });
  };

  Picker.prototype.refresh = function () {
    if (this.jsonInput) this.jsonInput.value = JSON.stringify(this.serialize());
    this.paintTray();
    this.paintTiles();
    if (window.__tradePickerBalance) window.__tradePickerBalance();
  };

  var pickers = {};

  function pickerFor(side) {
    if (!pickers[side]) {
      pickers[side] = new Picker(side);
    }
    return pickers[side];
  }

  function sideOf(el) {
    var grid = el.closest("[data-pick-grid]");
    return grid ? grid.getAttribute("data-pick-grid") : null;
  }

  // ONE delegated listener on the document, so tiles swapped in by HTMX need no
  // re-binding — the same reason card-flip.js and card-hover.js delegate.
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".js-pick-toggle");
    if (!btn) return;
    var tile = btn.closest(".trade-pick-item");
    var side = sideOf(btn);
    if (!tile || !side) return;
    ev.preventDefault();
    var picker = pickerFor(side);
    var kind = tile.getAttribute("data-pick-kind");
    var id = parseInt(tile.getAttribute("data-pick-id"), 10);
    if (picker.has(kind, id)) picker.remove(kind, id);
    else picker.add(tile);
    picker.refresh();
  });

  document.body.addEventListener("htmx:afterSwap", function (ev) {
    var grid = ev.target.closest
      ? ev.target.closest("[data-pick-grid]") || ev.target.querySelector("[data-pick-grid]")
      : null;
    var side = grid ? grid.getAttribute("data-pick-grid") : null;
    if (side) pickerFor(side).paintTiles();
  });

  // Restore: the trade as it stands (counter editor) or the picks a rejected
  // submit came back with. Both arrive as the same blob, so there is one
  // restore path rather than two.
  function restore() {
    var el = document.getElementById("pick-restore");
    if (!el) return;
    var blob;
    try {
      blob = JSON.parse(el.textContent || "{}");
    } catch (err) {
      return; // a malformed blob must never break the picker
    }
    ["offered", "requested"].forEach(function (side) {
      var entries = blob[side] || [];
      var picker = pickerFor(side);
      entries.forEach(function (e) {
        if (!e || !e.kind || !e.id) return;
        picker.picks.set(keyOf(e.kind, e.id), {
          kind: e.kind,
          id: e.id,
          alt: e.alt || "",
          name: e.name || "Card",
          price: parseFloat(e.price) || 0,
          proxy: !!e.proxy,
          meta: e.meta || "",
          quantity: e.quantity || 1,
          available: e.available || e.quantity || 1,
        });
      });
    });
  }

  function boot() {
    restore();
    ["offered", "requested"].forEach(function (side) {
      pickerFor(side).refresh();
    });
    var form = document.querySelector("[data-pick-form]");
    if (form) {
      form.addEventListener("submit", function () {
        ["offered", "requested"].forEach(function (side) {
          var p = pickers[side];
          if (p && p.jsonInput) p.jsonInput.value = JSON.stringify(p.serialize());
        });
      });
    }
  }

  // The balance reads the SELECTION, never the DOM — that is the whole point.
  // The balance is VIEWER-relative, and the two screens disagree about which
  // side that is. "Offered" always means the PROPOSER's cards, so on the
  // construction page (you are the proposer) offered is what you give — but a
  // RECIPIENT countering gives the requested side. The bar carries
  // `data-give-side`, or the numbers come out backwards and "in your favour"
  // says the opposite of the truth, which is what it did on the counter editor
  // until 2026-08-23.
  window.__tradePickerBalance = function () {
    var bar = document.querySelector("[data-give-side]");
    var giveSide = bar ? bar.getAttribute("data-give-side") : "offered";
    var getSide = giveSide === "offered" ? "requested" : "offered";
    var give = pickers[giveSide] ? pickers[giveSide].total() : 0;
    var get = pickers[getSide] ? pickers[getSide].total() : 0;
    var giveEl = document.querySelector('[data-pick-total="give"]');
    var getEl = document.querySelector('[data-pick-total="get"]');
    var diffEl = document.querySelector('[data-pick-total="diff"]');
    if (giveEl) giveEl.textContent = moneyText(give);
    if (getEl) getEl.textContent = moneyText(get);
    var proxyPanel = document.getElementById("trade-proxy-notice-panel");
    if (proxyPanel) {
      var anyProxy =
        (pickers.offered && pickers.offered.anyProxy()) ||
        (pickers.requested && pickers.requested.anyProxy());
      proxyPanel.style.display = anyProxy ? "" : "none";
    }
    if (!diffEl) return;
    var offeredEmpty = !pickers.offered || pickers.offered.picks.size === 0;
    var requestedEmpty = !pickers.requested || pickers.requested.picks.size === 0;
    if (offeredEmpty && requestedEmpty) {
      diffEl.textContent = "Pick cards on both sides to compare.";
      diffEl.className = "trade-balance-diff";
      return;
    }
    // Round to cents first so float noise cannot push an exact $1.00 gap over
    // the threshold; card prices are not precise enough to treat small change
    // as a real difference. The threshold itself comes from the SERVER
    // (`data-even-within`), so the static banner on a proposed trade and this
    // live one cannot disagree about what "even" means.
    var evenWithin = bar ? parseFloat(bar.getAttribute("data-even-within")) : 1;
    if (!(evenWithin >= 0)) evenWithin = 1;
    var delta = Math.round((give - get) * 100) / 100;
    if (Math.abs(delta) <= evenWithin) {
      diffEl.textContent = "Even — within " + moneyText(Math.abs(delta));
      diffEl.className = "trade-balance-diff is-even";
    } else {
      diffEl.textContent =
        delta > 0 ? moneyText(delta) + " in their favour" : moneyText(-delta) + " in your favour";
      diffEl.className = "trade-balance-diff is-uneven";
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
