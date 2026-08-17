/* card-flip.js — show the other side of a double-faced card.
 *
 * Contract (emitted by the inventory_card macro in _macros.html):
 *   <button data-card-flip
 *           data-front / data-back              displayed thumbnail src
 *           data-front-large / data-back-large  the hover-preview src
 *           data-front-alt / data-back-alt      Scryfall onerror fallback
 *           aria-pressed="false">
 * The button must be a SIBLING of the tile's <a>, inside .inventory-card-media
 * (a <button> inside an <a> is the invalid nesting swept in #174).
 *
 * Used by: every surface rendering the `inventory_card` macro — Collection,
 * deck detail, location detail, Showcase, Share, trade detail, bulk-delete
 * confirm. Keep this list current.
 *
 * SEMANTICS LIVE IN PYTHON. The server decides WHICH cards get a button
 * (dependencies.has_back_face — `layout`, never a "//" in type_line, because
 * adventure/split/prepare/flip all carry the separator and have ONE image) and
 * builds every URL (mirror_image_url). This script only swaps attributes it was
 * handed; it never constructs a URL or infers a rule.
 *
 * ONE delegated listener for the whole document, like card-hover.js — not one
 * per tile, which on a 50-card grid would be 50 listeners for a control most
 * people never touch.
 */
(function () {
  "use strict";

  // The card grids are HTMX-swappable partials, and a page can legitimately
  // load this more than once. One delegated listener is the whole design, so
  // registering a second would flip every card twice and land back on the
  // front — a "the button does nothing" bug that only appears after a swap.
  if (window.__cardFlipWired) return;
  window.__cardFlipWired = true;

  function flip(btn) {
    var media = btn.parentElement;
    if (!media) return;
    // Any <img> inside the wrapper, NOT a specific class: the grid tile uses
    // .inventory-thumb and the card-detail page uses .card-detail-art-img, and
    // a class-specific selector silently made the button inert on the second
    // surface. The wrapper holds exactly the artwork and this button, so there
    // is nothing else to match.
    var img = media.querySelector("img");
    if (!img) return;

    var showingBack = btn.getAttribute("aria-pressed") === "true";
    var next = showingBack ? "front" : "back";

    var src = btn.dataset[next];
    var alt = btn.dataset[next + "Alt"];
    var large = btn.dataset[next + "Large"];
    if (!src) return;

    // Re-arm the onerror fallback for the face we are switching TO. Without
    // this a back image that 404s would fall back to the FRONT's Scryfall URL
    // and silently show the wrong side rather than failing visibly.
    if (alt) {
      img.onerror = function () {
        img.onerror = null;
        img.src = alt;
      };
    }
    img.src = src;
    btn.setAttribute("aria-pressed", showingBack ? "false" : "true");

    // Keep the hover preview on the same face as the tile. The preview reads
    // these off the .inventory-card ancestor, so a flipped tile whose preview
    // still showed the front would read as a bug in the preview.
    var card = media.closest(".inventory-card");
    if (card && large) {
      card.setAttribute("data-card-image", large);
      var altLarge = btn.dataset[next + "Alt"];
      if (altLarge) card.setAttribute("data-card-image-alt", altLarge);
    }
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-card-flip]");
    if (!btn) return;
    // The tile is a link to the card page; flipping must not navigate.
    e.preventDefault();
    e.stopPropagation();
    flip(btn);
  });
})();
