/**
 * Largest font size in [minPx, maxPx] so the element's text fits on one line
 * (scrollWidth <= clientWidth, 1px tolerance). Clears font-size when text is empty.
 *
 * @param {HTMLElement|null|undefined} el
 * @param {number} minPx
 * @param {number} maxPx
 * @param {{ skipIfHidden?: boolean }} [opts]
 */
function fitTextLineFontSize(el, minPx, maxPx, opts) {
  opts = opts || {};
  if (!el) return;
  if (opts.skipIfHidden && el.hidden) return;
  var text = el.textContent || "";
  if (!String(text).trim()) {
    el.style.removeProperty("font-size");
    return;
  }
  if (el.clientWidth <= 0) return;
  var low = minPx;
  var high = maxPx;
  var best = minPx;
  while (low <= high) {
    var mid = (low + high) >> 1;
    el.style.fontSize = mid + "px";
    if (el.scrollWidth <= el.clientWidth + 1) {
      best = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  el.style.fontSize = best + "px";
}

/** Run fn after layout (double requestAnimationFrame). */
function afterLayout(fn) {
  requestAnimationFrame(function () {
    requestAnimationFrame(fn);
  });
}
