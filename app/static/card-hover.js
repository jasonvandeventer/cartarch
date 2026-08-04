// Card image hover-preview (v4.12.19, #169).
//
// Wires any container carrying `data-card-hover` so that hovering a descendant
// matching its `data-card-hover-target` selector shows that card's image in a
// single floating preview near the cursor. Moving off the row hides it.
//
// Per-element attributes, read off the hovered target:
//   data-card-image     — the image URL to show (REQUIRED; no element, no preview)
//   data-card-image-alt — the fallback URL used if the first one 404s (optional)
//
// **URL construction lives in Python, not here.** The server emits both URLs from
// `mirror_image_url()` / `scryfall_image_fallback()`, the same division of
// responsibility list-filter.js documents for filter semantics: this file only
// displays what it is given, and never builds an image URL. The fallback mirrors
// the `img_fallback` macro's contract for a printing the mirror has not cached yet.
//
// **ONE preview node and ONE delegated listener**, regardless of list length. The
// commander picker renders 539 rows on the largest account; an <img> per row would
// be hundreds of nodes and requests, lazy or not.
//
// **Desktop only.** Gated on `matchMedia("(hover: hover) and (pointer: fine)")`, the
// same posture _deck_card_list.html documents. Nothing binds on a touch device, so a
// tap still just follows the row's own link — a tap-to-preview would fight it.
//
// **The preview never takes pointer events** (`pointer-events: none`). Without that
// it sits under the cursor, steals `mouseenter` from the row beneath, and flickers.
//
// **Hidden rows.** list-filter.js hides rows with `el.hidden`, and a row hidden while
// its preview is open may never fire `mouseleave` — leaving a stale image floating
// over an unrelated list. Any input/select carrying the same `data-list-filter-target`
// also hides the preview.
//
// Used by: recommendations/commander_picker.html.
(function () {
  // Touch/coarse-pointer devices never bind. Checked once at load: a device does not
  // grow a mouse mid-session, and re-checking per event would cost more than it saves.
  if (!window.matchMedia || !window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
    return;
  }

  var GAP = 16; // cursor-to-preview offset, and the viewport margin for edge flips
  var preview = null;
  var img = null;
  var footer = null;

  function ensurePreview() {
    if (preview) return preview;
    preview = document.createElement("div");
    preview.className = "card-hover-preview";
    preview.setAttribute("aria-hidden", "true"); // decorative: the row's text is the label
    img = document.createElement("img");
    img.alt = "";
    preview.appendChild(img);
    footer = document.createElement("div");
    footer.className = "card-hover-preview-footer";
    footer.hidden = true;
    preview.appendChild(footer);
    document.body.appendChild(preview);
    return preview;
  }

  function hide() {
    if (preview) preview.classList.remove("is-visible");
  }

  function position(x, y) {
    var w = preview.offsetWidth || 240;
    var h = preview.offsetHeight || 340;
    // Flip rather than clip: past the right/bottom edge the preview goes to the
    // other side of the cursor. clamp() guards the case where it fits on neither.
    var left = x + GAP + w > window.innerWidth ? x - GAP - w : x + GAP;
    var top = y + GAP + h > window.innerHeight ? y - GAP - h : y + GAP;
    preview.style.left = Math.max(GAP, Math.min(left, window.innerWidth - w - GAP)) + "px";
    preview.style.top = Math.max(GAP, Math.min(top, window.innerHeight - h - GAP)) + "px";
  }

  function show(el, x, y) {
    var src = el.getAttribute("data-card-image");
    if (!src) return; // nothing to show is not an error — just do nothing
    ensurePreview();
    if (img.getAttribute("src") !== src) {
      var alt = el.getAttribute("data-card-image-alt");
      // Fall back ONCE, then give up — otherwise a broken fallback loops forever.
      img.onerror = alt
        ? function () {
            img.onerror = null;
            img.src = alt;
          }
        : null;
      img.src = src;
    }
    // Optional footer (playgroup request, Aug 2026): set/printing + price.
    // textContent only — this is attribute data, never HTML.
    var info = el.getAttribute("data-card-info") || "";
    footer.textContent = info;
    footer.hidden = !info;
    preview.classList.add("is-visible");
    position(x, y);
  }

  document.querySelectorAll("[data-card-hover]").forEach(function (container) {
    var selector = container.getAttribute("data-card-hover-target");
    if (!selector) return;

    // ONE delegated listener per container, not one per row.
    container.addEventListener("mouseover", function (e) {
      var el = e.target.closest ? e.target.closest(selector) : null;
      if (el && container.contains(el)) show(el, e.clientX, e.clientY);
    });
    container.addEventListener("mousemove", function (e) {
      var el = e.target.closest ? e.target.closest(selector) : null;
      if (el && container.contains(el) && preview && preview.classList.contains("is-visible")) {
        position(e.clientX, e.clientY);
      }
    });
    container.addEventListener("mouseout", function (e) {
      var to = e.relatedTarget;
      // Ignore moves WITHIN the same row; only a real exit hides.
      if (!to || !to.closest || !to.closest(selector)) hide();
    });
    container.addEventListener("mouseleave", hide);
  });

  // A row hidden by the filter under an open preview may never fire mouseleave.
  document
    .querySelectorAll("[data-list-filter], [data-list-facet]")
    .forEach(function (control) {
      control.addEventListener("input", hide);
      control.addEventListener("change", hide);
    });

  // Scrolling moves the row out from under a cursor that never moved.
  window.addEventListener("scroll", hide, { passive: true });
})();
