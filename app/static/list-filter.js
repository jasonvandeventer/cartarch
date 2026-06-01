// Generic instant list filter (v3.32.x).
//
// Wires any `input[data-list-filter]` to instantly show/hide elements
// matching its `data-list-filter-target` selector, by case-insensitive
// substring match against each element's `data-filter-text` (falling back
// to its textContent). Purely client-side — the lists it filters
// (showcase/share names, curated cards) are bounded, so there's no need
// for a server round-trip or query param. Optional attributes:
//   data-list-filter-empty="<selector>"  — a "no matches" element to toggle
//
// Used by: showcases.html, shares.html, showcase.html, share_view.html.
(function () {
  function applyFilter(input) {
    var targetSel = input.getAttribute("data-list-filter-target");
    if (!targetSel) return;
    var q = input.value.trim().toLowerCase();
    var items = document.querySelectorAll(targetSel);
    var shown = 0;
    items.forEach(function (el) {
      var hay = (el.getAttribute("data-filter-text") || el.textContent || "").toLowerCase();
      var match = q === "" || hay.indexOf(q) !== -1;
      el.hidden = !match;
      if (match) shown++;
    });
    var emptySel = input.getAttribute("data-list-filter-empty");
    if (emptySel) {
      var empty = document.querySelector(emptySel);
      if (empty) empty.hidden = !(q !== "" && shown === 0);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("input[data-list-filter]").forEach(function (input) {
      input.addEventListener("input", function () {
        applyFilter(input);
      });
      // Don't let Enter do anything surprising if this ever lands in a form.
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") e.preventDefault();
      });
    });
  });
})();
