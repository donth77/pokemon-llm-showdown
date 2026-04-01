/**
 * LLM thoughts UI + WebSocket feed; optional callouts via postMessage to battle iframe.
 * Expects window.__THOUGHTS_FEED__ (see broadcast templates).
 */
(function () {
  "use strict";
  const cfg = window.__THOUGHTS_FEED__;
  if (!cfg) return;

  const panels = cfg.panels;
  const thoughtsP1 = panels ? document.getElementById(panels.p1) : null;
  const thoughtsP2 = panels ? document.getElementById(panels.p2) : null;
  const thoughtsTitleP1 = panels
    ? document.getElementById(panels.titleP1)
    : null;
  const thoughtsTitleP2 = panels
    ? document.getElementById(panels.titleP2)
    : null;
  const thoughtsPortraitP1 =
    panels && panels.portraitP1
      ? document.getElementById(panels.portraitP1)
      : null;
  const thoughtsPortraitP2 =
    panels && panels.portraitP2
      ? document.getElementById(panels.portraitP2)
      : null;

  function getCalloutTarget() {
    const id = cfg.calloutIframeId;
    if (!id) return null;
    return document.getElementById(id);
  }

  function syncThoughtPanelHeights() {
    if (!thoughtsP1) return;
    let hideUi = !!cfg.hideBattleUi;
    const syncId = cfg.syncHeightsIframeId;
    if (syncId) {
      try {
        const iframe = document.getElementById(syncId);
        const src = (iframe && iframe.src) || "";
        const searchIndex = src.indexOf("?");
        const searchPart = searchIndex !== -1 ? src.slice(searchIndex + 1) : "";
        const sp = new URLSearchParams(searchPart);
        hideUi = sp.has("hide_battle_ui");
      } catch (_) {
        // ignore
      }
    }

    const panelHeight = hideUi ? 270 : 136;

    document.querySelectorAll(".thoughts-panel").forEach((el) => {
      el.style.height = panelHeight + "px";
    });
    document.querySelectorAll(".thoughts-scroll").forEach((el) => {
      el.style.height = "";
    });
  }

  syncThoughtPanelHeights();
  if (cfg.syncHeightsIframeId) {
    window.addEventListener(
      "broadcast-battle-iframe-load",
      syncThoughtPanelHeights,
    );
  }

  let thoughtPlayer1 = "Player 1";
  let thoughtPlayer2 = "Player 2";

  function setThoughtPlayerNames(p1, p2) {
    thoughtPlayer1 = p1 || thoughtPlayer1;
    thoughtPlayer2 = p2 || thoughtPlayer2;
    if (thoughtsTitleP1) thoughtsTitleP1.textContent = thoughtPlayer1;
    if (thoughtsTitleP2) thoughtsTitleP2.textContent = thoughtPlayer2;
  }

  function pickTrainerArt(portraitUrl, spriteUrl) {
    const u = (portraitUrl || "").trim();
    if (u) return u;
    return (spriteUrl || "").trim();
  }

  function schedulePinPortrait(el) {
    const sc = el && el.closest ? el.closest(".thoughts-scroll") : null;
    if (!sc) return;
    const body = sc.querySelector(".thoughts-scroll-body");
    requestAnimationFrame(function () {
      if (body) scrollThoughtsToBottom(body);
      else pinPortraitToScroll(sc);
    });
  }

  function applyThoughtPortraitImg(el, url, altName) {
    if (!el) return;
    const u = (url || "").trim();
    const alt = altName ? String(altName) : "";
    if (!u) {
      el.onload = null;
      el.onerror = null;
      if (!el.getAttribute("src")) {
        el.alt = "";
        return;
      }
      el.classList.remove("is-visible");
      el.removeAttribute("src");
      el.alt = "";
      schedulePinPortrait(el);
      return;
    }
    const cur = el.getAttribute("src") || "";
    if (cur === u) {
      if (el.alt !== alt) el.alt = alt;
      if (el.classList.contains("is-visible")) return;
      if (el.complete && el.naturalWidth > 0) {
        el.classList.add("is-visible");
        schedulePinPortrait(el);
        return;
      }
      return;
    }
    el.onload = null;
    el.onerror = null;
    el.classList.remove("is-visible");
    el.alt = alt;
    el.onload = function () {
      el.classList.add("is-visible");
      schedulePinPortrait(el);
    };
    el.onerror = function () {
      el.classList.remove("is-visible");
      el.removeAttribute("src");
      schedulePinPortrait(el);
    };
    el.src = u;
  }

  function setThoughtPortraitsFromScoreboard(data) {
    if (!thoughtsPortraitP1 && !thoughtsPortraitP2) return;
    const n1 = data?.player1_name || thoughtPlayer1;
    const n2 = data?.player2_name || thoughtPlayer2;
    const art1 = pickTrainerArt(
      data?.player1_portrait_square_url,
      data?.player1_sprite_url,
    );
    const art2 = pickTrainerArt(
      data?.player2_portrait_square_url,
      data?.player2_sprite_url,
    );
    applyThoughtPortraitImg(thoughtsPortraitP1, art1, n1);
    applyThoughtPortraitImg(thoughtsPortraitP2, art2, n2);
  }

  function applyScoreboardToThoughts(data) {
    if (!data || typeof data !== "object") return;
    const p1 = data.player1_name;
    const p2 = data.player2_name;
    if (p1 && p2) {
      setThoughtPlayerNames(p1, p2);
    } else {
      const wins = data.wins || {};
      const names = Object.keys(wins);
      if (names.length >= 2) setThoughtPlayerNames(names[0], names[1]);
    }
    setThoughtPortraitsFromScoreboard(data);
    requestAnimationFrame(function () {
      syncPortraitPinsForPanels();
    });
  }

  async function refreshThoughtNamesFromScoreboard() {
    try {
      const resp = await fetch("/scoreboard", { cache: "no-store" });
      const data = await resp.json();
      applyScoreboardToThoughts(data);
    } catch (_) {}
  }

  function maxScrollTop(scroller) {
    if (!scroller) return 0;
    return Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  }

  /** Keep float portrait visually at top of viewport; marginTop must be 0 when content fits. */
  function pinPortraitToScroll(scroller) {
    if (!scroller) return;
    const portrait = scroller.querySelector(".thoughts-portrait");
    if (!portrait) return;
    const maxS = maxScrollTop(scroller);
    if (maxS <= 0) {
      portrait.style.marginTop = "0";
      scroller.scrollTop = 0;
      return;
    }
    portrait.style.marginTop = scroller.scrollTop + "px";
  }

  /**
   * Pinning the float portrait changes scrollHeight; scroll max must be re-applied
   * after layout (often needs 2+ passes). Deferred rAF flushes catch innerHTML/fonts.
   */
  function flushScrollToBottom(scroller) {
    if (!scroller) return;
    // #region agent log
    var _fs = performance.now();
    // #endregion
    for (let k = 0; k < 6; k++) {
      scroller.scrollTop = scroller.scrollHeight;
      pinPortraitToScroll(scroller);
    }
    // #region agent log
    var _fd = performance.now() - _fs;
    if (_fd > 16 && window._dbg)
      window._dbg("thoughts:flush", "slow", { ms: Math.round(_fd) }, "H2");
    // #endregion
  }

  function scrollThoughtsToBottom(bodyEl) {
    const scroller =
      bodyEl && bodyEl.closest ? bodyEl.closest(".thoughts-scroll") : null;
    if (!scroller) return;
    flushScrollToBottom(scroller);
    requestAnimationFrame(function () {
      flushScrollToBottom(scroller);
      requestAnimationFrame(function () {
        flushScrollToBottom(scroller);
      });
    });
  }

  function syncPortraitPinsForPanels() {
    [thoughtsP1, thoughtsP2].forEach((body) => {
      if (!body) return;
      const sc = body.closest ? body.closest(".thoughts-scroll") : null;
      if (sc) flushScrollToBottom(sc);
    });
  }

  /** Reasoning longer than this adds class thought-line--long (smaller type on that row only). */
  const THOUGHT_LINE_LONG_CHARS = 300;

  function thoughtHasDisplayReasoning(item) {
    return String(item?.reasoning || "").trim().length > 0;
  }

  function renderThoughtList(target, items) {
    if (!target) return;
    const visible = Array.isArray(items)
      ? items.filter((item) => thoughtHasDisplayReasoning(item))
      : [];
    if (!visible.length) {
      target.innerHTML =
        '<div class="thought-line"><span class="meta">--</span>Waiting for thoughts...</div>';
      scrollThoughtsToBottom(target);
      return;
    }
    const lastItems = visible.slice(-14);
    target.innerHTML = lastItems
      .map((item) => {
        const turn = Number.isFinite(item?.turn) ? `T${item.turn}` : "T?";
        const action = item?.action ? String(item.action) : "action";
        const reasoning = item?.reasoning ? String(item.reasoning) : "";
        const safeReasoning = reasoning
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;");
        const longClass =
          reasoning.length > THOUGHT_LINE_LONG_CHARS
            ? " thought-line--long"
            : "";
        const ek = item?.event_kind ? String(item.event_kind) : "";
        const evClass =
          ek === "parse_failure" || ek === "llm_error"
            ? " thought-line--agent-event"
            : "";
        return `<div class="thought-line${longClass}${evClass}"><span class="meta">${turn} ${action}</span>${safeReasoning}</div>`;
      })
      .join("");
    scrollThoughtsToBottom(target);
  }

  let calloutMessageSeq = 0;
  function postCalloutsToBattleFrame(p1Callout, p2Callout) {
    const battleFrame = getCalloutTarget();
    try {
      if (!battleFrame || !battleFrame.contentWindow) return;
      calloutMessageSeq += 1;
      battleFrame.contentWindow.postMessage(
        {
          type: "llm_callouts",
          p1_callout: p1Callout || "",
          p2_callout: p2Callout || "",
          version: calloutMessageSeq,
        },
        "*",
      );
    } catch (_) {}
  }

  const thoughtItems = {};
  const calloutQueue = [];
  const pendingCalloutsBySide = { p1: [], p2: [] };
  const unreleasedActionTsBySide = { p1: 0, p2: 0 };
  const UNRELEASED_ACTION_MAX_AGE_MS = 4000;
  let calloutPlaying = false;
  const CALLOUT_DISPLAY_MS = 3200;

  function isPlaceholderThoughtName(name) {
    const n = (name || "").trim();
    return !n || n === "Player 1" || n === "Player 2";
  }

  function syncPlayerNamesFromThoughts() {
    const keys = Object.keys(thoughtItems);
    const p1Has = thoughtPlayer1 && keys.includes(thoughtPlayer1);
    const p2Has = thoughtPlayer2 && keys.includes(thoughtPlayer2);
    if (keys.length === 1 && !p1Has) {
      const only = keys[0];
      // If scoreboard already fixed P2's name and the only thought is from P2,
      // do not assign `only` to panel 1 — that would make thoughtPlayer1 ===
      // thoughtPlayer2 and duplicate one stream in both bubbles.
      if (isPlaceholderThoughtName(thoughtPlayer2) || only !== thoughtPlayer2) {
        setThoughtPlayerNames(only, thoughtPlayer2);
      }
    } else if (keys.length >= 2 && (!p1Has || !p2Has)) {
      const sorted = keys.slice().sort();
      setThoughtPlayerNames(sorted[0], sorted[1]);
    }
  }

  function renderAllThoughtPanels() {
    if (!thoughtsP1 || !thoughtsP2) return;
    syncPlayerNamesFromThoughts();
    renderThoughtList(thoughtsP1, thoughtItems[thoughtPlayer1] || []);
    renderThoughtList(thoughtsP2, thoughtItems[thoughtPlayer2] || []);
  }

  function enqueueDisplayCallout(side, text) {
    calloutQueue.push({ side, text });
  }

  function releasePendingCalloutForSide(side) {
    if (side !== "p1" && side !== "p2") return;
    const pending = pendingCalloutsBySide[side];
    if (!pending || !pending.length) return;
    const item = pending.shift();
    if (!item || !item.text) return;
    enqueueDisplayCallout(side, item.text);
    drainCalloutQueue();
  }

  function drainCalloutQueue() {
    if (calloutPlaying || !calloutQueue.length) return;
    calloutPlaying = true;
    const { side, text } = calloutQueue.shift();
    const p1 = side === "p1" ? text : "";
    const p2 = side === "p2" ? text : "";
    postCalloutsToBattleFrame(p1, p2);
    setTimeout(() => {
      calloutPlaying = false;
      drainCalloutQueue();
    }, CALLOUT_DISPLAY_MS);
  }

  const PENDING_CALLOUT_MAX_WAIT_MS = 6000;
  const pendingCalloutTimers = { p1: null, p2: null };

  function schedulePendingCalloutFallback(side) {
    if (side !== "p1" && side !== "p2") return;
    clearPendingCalloutTimer(side);
    pendingCalloutTimers[side] = setTimeout(() => {
      pendingCalloutTimers[side] = null;
      releasePendingCalloutForSide(side);
    }, PENDING_CALLOUT_MAX_WAIT_MS);
  }

  function clearPendingCalloutTimer(side) {
    if (pendingCalloutTimers[side]) {
      clearTimeout(pendingCalloutTimers[side]);
      pendingCalloutTimers[side] = null;
    }
  }

  function handleThoughtsMessage(msg) {
    // #region agent log
    if (window._dbg) {
      var _tc = 0;
      for (var _k in thoughtItems)
        if (thoughtItems[_k]) _tc += thoughtItems[_k].length;
      window._dbg(
        "thoughts:msg",
        msg.type,
        { items: _tc, players: Object.keys(thoughtItems).length },
        "H2",
      );
    }
    // #endregion
    if (msg.type === "history") {
      for (const k of Object.keys(thoughtItems)) delete thoughtItems[k];
      const players = msg.players || {};
      for (const [player, items] of Object.entries(players)) {
        thoughtItems[player] = Array.isArray(items)
          ? items
              .filter((item) => thoughtHasDisplayReasoning(item))
              .map((item) => ({
                timestamp: item.timestamp,
                turn: item.turn,
                action: item.action,
                reasoning: item.reasoning,
                callout: item.callout,
                event_kind: item.event_kind || "",
                detail: item.detail,
              }))
          : [];
      }
      renderAllThoughtPanels();
    } else if (msg.type === "thought") {
      const player = msg.player || "";
      if (!player) return;
      const reasoningTrim = String(msg.reasoning || "").trim();
      if (reasoningTrim) {
        if (!thoughtItems[player]) thoughtItems[player] = [];
        thoughtItems[player].push({
          timestamp: msg.timestamp,
          turn: msg.turn,
          action: msg.action,
          reasoning: msg.reasoning,
          callout: msg.callout,
          event_kind: msg.event_kind || "",
          detail: msg.detail,
        });
        if (thoughtItems[player].length > 80) {
          thoughtItems[player] = thoughtItems[player].slice(-80);
        }
        renderAllThoughtPanels();
      }
      const callout = (msg.callout || "").trim();
      if (callout && getCalloutTarget()) {
        const bs = String(msg.battle_side || "")
          .trim()
          .toLowerCase();
        const side =
          bs === "p1" || bs === "p2"
            ? bs
            : player === thoughtPlayer1
              ? "p1"
              : "p2";
        const actionAge = Date.now() - (unreleasedActionTsBySide[side] || 0);
        if (actionAge < UNRELEASED_ACTION_MAX_AGE_MS) {
          unreleasedActionTsBySide[side] = 0;
          pendingCalloutsBySide[side] = [];
          clearPendingCalloutTimer(side);
          enqueueDisplayCallout(side, callout);
          drainCalloutQueue();
        } else {
          pendingCalloutsBySide[side] = [
            {
              text: callout,
              turn: msg.turn,
              action: msg.action,
              timestamp: msg.timestamp,
            },
          ];
          schedulePendingCalloutFallback(side);
        }
      }
    } else if (msg.type === "clear") {
      for (const k of Object.keys(thoughtItems)) delete thoughtItems[k];
      pendingCalloutsBySide.p1.length = 0;
      pendingCalloutsBySide.p2.length = 0;
      unreleasedActionTsBySide.p1 = 0;
      unreleasedActionTsBySide.p2 = 0;
      clearPendingCalloutTimer("p1");
      clearPendingCalloutTimer("p2");
      calloutQueue.length = 0;
      calloutPlaying = false;
      renderAllThoughtPanels();
      postCalloutsToBattleFrame("", "");
    }
  }

  function isBattleFrameMessage(event) {
    const battleFrame = getCalloutTarget();
    try {
      if (battleFrame && event.source === battleFrame.contentWindow)
        return true;
    } catch (_) {}
    if (event.source && event.data && typeof event.data.type === "string") {
      const t = event.data.type;
      return t === "battle_action" || t === "battle_turn";
    }
    return false;
  }

  if (cfg.calloutIframeId) {
    window.addEventListener("message", (event) => {
      const data = event && event.data;
      if (!data) return;
      if (!isBattleFrameMessage(event)) return;

      if (data.type === "battle_action") {
        const side = data.side === "p1" ? "p1" : data.side === "p2" ? "p2" : "";
        if (!side) return;
        const pending = pendingCalloutsBySide[side];
        if (pending && pending.length) {
          unreleasedActionTsBySide[side] = 0;
          clearPendingCalloutTimer(side);
          releasePendingCalloutForSide(side);
        } else {
          unreleasedActionTsBySide[side] = Date.now();
        }
      } else if (data.type === "battle_turn") {
        for (const s of ["p1", "p2"]) {
          clearPendingCalloutTimer(s);
          releasePendingCalloutForSide(s);
        }
      }
    });
  }

  /** Agents append ``/state/thoughts.json`` before POSTing ``/thought``; HTTP sync heals WS failures and restarts. */
  let thoughtsHttpSig = "";

  async function maybeRefreshThoughtsFromHttp() {
    try {
      const resp = await fetch("/thoughts", { cache: "no-store" });
      const data = await resp.json();
      if (!data || typeof data !== "object") return;
      const players = data.players || {};
      let n = 0;
      for (const k of Object.keys(players)) {
        const arr = players[k];
        if (Array.isArray(arr)) n += arr.length;
      }
      const sig = `${Number(data.updated_at) || 0}:${n}`;
      if (sig === thoughtsHttpSig) return;
      thoughtsHttpSig = sig;
      handleThoughtsMessage({ type: "history", players });
    } catch (_) {}
  }

  function connectThoughtsWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/thoughts/ws`;
    const ws = new WebSocket(url);
    ws.onmessage = (event) => {
      try {
        handleThoughtsMessage(JSON.parse(event.data));
      } catch (_) {}
    };
    ws.onclose = () => setTimeout(connectThoughtsWS, 2000);
    ws.onerror = () => ws.close();
  }

  /*
   * Hub page (`/broadcast`): scoreboard names/portraits come only from the parent's
   * `broadcast_scoreboard` CustomEvent (fired from the same SSE path as postMessage
   * to iframes). No `/scoreboard` polling here — avoids duplicate HTTP while the
   * stream is healthy.
   */
  if (window.__BROADCAST_SCOREBOARD_HUB__) {
    window.addEventListener("broadcast_scoreboard", (ev) => {
      const d = ev && ev.detail;
      if (d) applyScoreboardToThoughts(d);
    });
  } else {
    refreshThoughtNamesFromScoreboard();
    setInterval(refreshThoughtNamesFromScoreboard, 5000);
  }
  connectThoughtsWS();
  maybeRefreshThoughtsFromHttp();
  setInterval(maybeRefreshThoughtsFromHttp, 2500);
})();
