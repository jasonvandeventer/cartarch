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
    image.onload = () => {
      if (pending[property] === request) {
        element.style.setProperty(property, `url(${JSON.stringify(url)})`);
      }
    };
    image.onerror = next;
    image.src = url;
  }
  next();
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-background-art]').forEach(element => {
    setBackgroundArt(element, '--cmp-art', JSON.parse(element.dataset.backgroundArt));
  });
});
