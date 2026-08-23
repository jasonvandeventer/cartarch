// Generic instant list filter (v3.32.x; colour/type facets added v4.12.1).
//
// Wires any `input[data-list-filter]` to instantly show/hide elements
// matching its `data-list-filter-target` selector, by case-insensitive
// substring match against each element's `data-filter-text` (falling back
// to its textContent). Purely client-side — the lists it filters
// (showcase/share names, curated cards, wishlist rows, trade pickers) are
// bounded, so there's no need for a server round-trip or query param.
// Optional attributes:
//   data-list-filter-empty="<selector>"  — a "no matches" element to toggle
//
// Colour/type facets (v4.12.1): a `select[data-list-facet="colors"|"types"]`
// carrying the SAME `data-list-filter-target` joins that target's filter as an
// additional criterion. Criteria COMPOSE — one engine evaluates all of them per
// element, because two independent scripts each setting `el.hidden` would
// clobber each other (a name match would un-hide a colour-filtered-out row).
// The values come from app/card_filters.py via `data-colors` / `data-types`,
// so the filter SEMANTICS live once in Python; this only compares.
//
// Used by: showcases.html, shares.html, showcase.html, share_view.html,
// watchlist.html, wishlist_public.html, playgroup_detail.html,
// commander_picker.html, sets.html (v4.13.28 — name OR code, both in
// data-filter-text).
//
// NOT trade_new.html any more (#184): the picker is paged, so a client-side
// filter could only ever have hidden the fifty cards on screen. Its search is
// server-side in the app's query language instead.
(function () {
  function colorMatches(token, want) {
    if (!want) return true;
    if (want === "C") return token === ""; // colourless / unfetched identity
    if (want === "M") return token.length > 1; // two or more colours
    return token.indexOf(want) !== -1;
  }

  function typeMatches(token, want) {
    if (!want) return true;
    return token.split(" ").indexOf(want) !== -1;
  }

  // Every control pointing at the same target selector filters it together.
  function controlsFor(targetSel) {
    return Array.prototype.slice.call(
      document.querySelectorAll('[data-list-filter-target="' + targetSel + '"]')
    );
  }

  function applyFilter(targetSel) {
    var q = "";
    var color = "";
    var type = "";
    var emptySel = null;
    controlsFor(targetSel).forEach(function (el) {
      var facet = el.getAttribute("data-list-facet");
      if (facet === "colors") color = el.value;
      else if (facet === "types") type = el.value;
      else q = (el.value || "").trim().toLowerCase();
      emptySel = el.getAttribute("data-list-filter-empty") || emptySel;
    });

    var items = document.querySelectorAll(targetSel);
    var shown = 0;
    var filtering = q !== "" || color !== "" || type !== "";
    items.forEach(function (el) {
      var hay = (el.getAttribute("data-filter-text") || el.textContent || "").toLowerCase();
      var match =
        (q === "" || hay.indexOf(q) !== -1) &&
        colorMatches(el.getAttribute("data-colors") || "", color) &&
        typeMatches(el.getAttribute("data-types") || "", type);
      el.hidden = !match;
      if (match) shown++;
    });

    if (emptySel) {
      var empty = document.querySelector(emptySel);
      if (empty) empty.hidden = !(filtering && shown === 0);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var targets = {};
    document
      .querySelectorAll("input[data-list-filter], select[data-list-facet]")
      .forEach(function (control) {
        var targetSel = control.getAttribute("data-list-filter-target");
        if (!targetSel) return;
        control.addEventListener("input", function () {
          applyFilter(targetSel);
        });
        control.addEventListener("change", function () {
          applyFilter(targetSel);
        });
        // Don't let Enter do anything surprising if this ever lands in a form.
        control.addEventListener("keydown", function (e) {
          if (e.key === "Enter") e.preventDefault();
        });
        targets[targetSel] = true;
      });
    // Apply once at load: browsers restore <select>/<input> values on reload and
    // back-navigation WITHOUT firing change, so an unapplied restored filter
    // reads as a broken control (the v4.11.37 lesson).
    Object.keys(targets).forEach(applyFilter);
  });
})();
