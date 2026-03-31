/**
 * SSE for /api/manager/stream — queue + tournament refresh hints.
 *
 * @typedef {Object} AttachManagerStreamOptions
 * @property {string} [url="/api/manager/stream"]
 * @property {(evt: { seq: number, queue: boolean, tournament_ids: number[], series_ids: number[] }) => void} onEvent
 * @property {() => void} [fallbackPoll] — when EventSource stays closed (proxies)
 * @property {number} [fallbackMs=5000]
 */

(function () {
  function attachManagerStream(opts) {
    if (!opts || typeof opts.onEvent !== "function") {
      throw new Error("attachManagerStream: onEvent is required");
    }
    var url = opts.url || "/api/manager/stream";
    var fallbackMs = opts.fallbackMs != null ? opts.fallbackMs : 5000;
    var fallbackTimer = null;

    function startFallback() {
      if (fallbackTimer !== null || typeof opts.fallbackPoll !== "function")
        return;
      fallbackTimer = setInterval(opts.fallbackPoll, fallbackMs);
    }

    function stopFallback() {
      if (fallbackTimer !== null) {
        clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
    }

    var es = new EventSource(url);
    es.onmessage = function (ev) {
      try {
        var d = JSON.parse(ev.data);
        if (d && typeof d.seq === "number") opts.onEvent(d);
      } catch (_e) {}
    };
    es.onopen = function () {
      stopFallback();
    };
    es.onerror = function () {
      if (es.readyState === EventSource.CLOSED) startFallback();
    };
  }

  window.attachManagerStream = attachManagerStream;
})();
