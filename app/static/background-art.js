/* CSS backgrounds have no error event. Probe candidates with Image, then
 * install the first that loads. A deck change cancels stale callbacks. */
function setBackgroundArt(element, property, sources) {
  const pending = element._backgroundArt || (element._backgroundArt = {});
  const key = JSON.stringify(sources);
  if (pending[property]?.key === key) return;
  const request = { key };
  pending[property] = request;
  element.style.removeProperty(property);
  let index = 0;
  function next() {
    if (pending[property] !== request || index >= sources.length) return;
    const url = sources[index++];
    const image = new Image();
    let settled = false;
    function finish(loaded) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (pending[property] !== request) return;
      if (loaded) {
        element.style.setProperty(property, `url(${JSON.stringify(url)})`);
      } else {
        next();
      }
    }
    // A stalled host must not prevent the independent fallback from loading.
    const timer = setTimeout(() => finish(false), 5000);
    image.onload = () => finish(true);
    image.onerror = () => finish(false);
    image.src = url;
  }
  next();
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-background-art]').forEach(element => {
    setBackgroundArt(element, '--cmp-art', JSON.parse(element.dataset.backgroundArt));
  });
});
