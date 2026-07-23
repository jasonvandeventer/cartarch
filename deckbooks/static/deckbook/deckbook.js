// Checklist name filter — the only JS in the read-only prototype. Standalone
// (this app doesn't share the main app's list-filter.js), instant, no deps.
(function () {
  var input = document.getElementById("db-filter");
  var table = document.getElementById("db-checklist");
  if (!input || !table) return;
  input.addEventListener("input", function () {
    var q = input.value.trim().toLowerCase();
    table.querySelectorAll("tbody tr").forEach(function (tr) {
      var name = tr.getAttribute("data-name") || "";
      tr.hidden = q !== "" && name.indexOf(q) === -1;
    });
  });
})();
