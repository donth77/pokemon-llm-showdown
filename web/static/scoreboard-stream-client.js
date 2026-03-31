/**
 * Shared /scoreboard/stream (SSE) subscriber with GET /scoreboard poll fallback
 * when EventSource stays closed (proxies, etc.).
 *
 * @typedef {Object} AttachScoreboardStreamOptions
 * @property {() => number} getSeq
 * @property {(n: number) => void} setSeq
 * @property {(payload: object) => void} applyOrdered - after duplicate-seq check (SSE)
 * @property {(payload: object) => void} applyFallback - poll path (may be same as applyOrdered)
 * @property {(err: unknown) => void} [onFetchError]
 * @property {string} [streamUrl="/scoreboard/stream"]
 * @property {string} [fetchUrl="/scoreboard"]
 * @property {number} [fallbackIntervalMs=2000]
 */

(function () {
  function attachScoreboardStream(opts) {
    var streamUrl = opts.streamUrl || "/scoreboard/stream";
    var fetchUrl = opts.fetchUrl || "/scoreboard";
    var intervalMs =
      opts.fallbackIntervalMs != null ? opts.fallbackIntervalMs : 2000;
    var getSeq = opts.getSeq;
    var setSeq = opts.setSeq;
    var applyOrdered = opts.applyOrdered;
    if (typeof applyOrdered !== "function") {
      throw new Error("attachScoreboardStream: applyOrdered is required");
    }
    var applyFallback =
      typeof opts.applyFallback === "function"
        ? opts.applyFallback
        : applyOrdered;
    var onFetchError = opts.onFetchError;

    var fallbackTimer = null;

    function applyStreamEnvelope(parsed) {
      if (!parsed || typeof parsed.seq !== "number" || !parsed.payload) {
        return;
      }
      /* Drop only exact duplicate seq. ``<=`` wrongly discards all events after the web
       * service restarts (server seq resets to 1 while this tab still has a large lastSeq). */
      if (parsed.seq === getSeq()) return;
      setSeq(parsed.seq);
      applyOrdered(parsed.payload);
    }

    function fallbackTick() {
      fetch(fetchUrl, { cache: "no-store" })
        .then(function (r) {
          return r.ok ? r.json() : null;
        })
        .then(function (data) {
          if (data) applyFallback(data);
        })
        .catch(function (err) {
          if (onFetchError) onFetchError(err);
        });
    }

    function startFallback() {
      if (fallbackTimer !== null) return;
      fallbackTimer = setInterval(fallbackTick, intervalMs);
    }

    function stopFallback() {
      if (fallbackTimer !== null) {
        clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
    }

    var es = new EventSource(streamUrl);
    es.onmessage = function (ev) {
      try {
        applyStreamEnvelope(JSON.parse(ev.data));
      } catch (_e) {}
    };
    es.onopen = function () {
      stopFallback();
    };
    es.onerror = function () {
      if (es.readyState === EventSource.CLOSED) startFallback();
    };
  }

  window.attachScoreboardStream = attachScoreboardStream;
})();
