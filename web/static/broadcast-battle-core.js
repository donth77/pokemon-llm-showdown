/**
 * Battle iframe: Showdown URL, scoreboard SSE (or hub postMessage), trainer sprites.
 * Standalone battle frame uses attachScoreboardStream + GET /scoreboard fallback.
 * On /broadcast, the hub drives updates via __refreshBattleFromHubScoreboard__ only
 * (no second EventSource here).
 *
 * Expects window.__BATTLE_CORE__ set before load (see broadcast templates).
 */
(function () {
  "use strict";
  const cfg = window.__BATTLE_CORE__;
  if (!cfg || !cfg.battleFrameId) return;

  const battleFrame = document.getElementById(cfg.battleFrameId);
  if (!battleFrame) return;

  const battleFormatLabel = cfg.formatLabelId
    ? document.getElementById(cfg.formatLabelId)
    : null;
  const tournamentInfoLabel = cfg.tournamentLabelId
    ? document.getElementById(cfg.tournamentLabelId)
    : null;

  const isLocal =
    window.location.hostname === "localhost" ||
    window.location.hostname === "127.0.0.1";
  const showdownBaseRaw = isLocal ? cfg.localUrl : cfg.internalUrl;

  function showdownBaseUrl() {
    const u = new URL(showdownBaseRaw);
    u.pathname = "/";
    return u.toString();
  }

  const stableBase = showdownBaseUrl();
  let currentBattleTag = null;
  /** Last ``status`` applied from scoreboard-shaped payloads (for live → idle transitions). */
  let lastPayloadStatus = "";
  battleFrame.src = stableBase;

  function syncThoughtPanelHeightsFromOutside() {
    window.dispatchEvent(new CustomEvent("broadcast-battle-iframe-load"));
  }

  const lastTrainerSpriteUrls = { p1: "", p2: "" };
  let trainerSpritePostSeq = 0;

  function postTrainerSpritesToBattleFrame() {
    try {
      if (!battleFrame || !battleFrame.contentWindow) return;
      trainerSpritePostSeq += 1;
      battleFrame.contentWindow.postMessage(
        {
          type: "llm_trainer_sprites",
          player1_sprite_url: lastTrainerSpriteUrls.p1,
          player2_sprite_url: lastTrainerSpriteUrls.p2,
          version: trainerSpritePostSeq,
        },
        "*",
      );
    } catch (_) {}
  }

  battleFrame.addEventListener("load", () => {
    postTrainerSpritesToBattleFrame();
    syncThoughtPanelHeightsFromOutside();
  });

  function normalizeBattleTag(tag) {
    if (!tag) return null;
    let normalized = String(tag).trim();
    normalized = normalized.replace(/^>+/, "").replace(/^\/+/, "");
    if (!normalized) return null;
    if (!normalized.startsWith("battle-")) {
      normalized = `battle-${normalized}`;
    }
    return normalized;
  }

  function shapeFromScoreboard(sb) {
    if (typeof scoreboardPayloadToBattleShape === "function") {
      return scoreboardPayloadToBattleShape(sb);
    }
    return null;
  }

  const PAGE_LOAD_TIME = Date.now();
  const RELOAD_AFTER_MS = 30 * 60 * 1000;
  let lastSeenStatus = "";
  let battleOutroTimer = null;
  let pendingPageReload = false;

  function clearBattleOutroTimer() {
    if (battleOutroTimer) {
      clearTimeout(battleOutroTimer);
      battleOutroTimer = null;
    }
  }

  function resetBattleFrameToLobby() {
    currentBattleTag = null;
    const u = new URL(stableBase);
    u.searchParams.set("_", String(Date.now()));
    battleFrame.src = u.toString();
    if (pendingPageReload) location.reload();
  }

  function maybeReloadBetweenMatches(status) {
    lastSeenStatus = status || "";
    if (
      lastSeenStatus === "idle" &&
      Date.now() - PAGE_LOAD_TIME > RELOAD_AFTER_MS
    ) {
      if (battleOutroTimer != null) {
        pendingPageReload = true;
        return;
      }
      if (currentBattleTag === null) {
        location.reload();
      } else {
        pendingPageReload = true;
      }
    }
  }

  function applyBattleUiFromPayload(data) {
    if (!data || typeof data !== "object") return;
    const status = data.status || "idle";
    const wasLive = lastPayloadStatus === "live";

    try {
      lastTrainerSpriteUrls.p1 = String(data.player1_sprite_url || "").trim();
      lastTrainerSpriteUrls.p2 = String(data.player2_sprite_url || "").trim();
      postTrainerSpritesToBattleFrame();
    } catch (_) {}

    try {
      if (battleFormatLabel) {
        battleFormatLabel.textContent =
          typeof formatBroadcastFormatLabel === "function"
            ? formatBroadcastFormatLabel(data)
            : `Format: ${displayBattleFormat(data.battle_format)}`;
      }
      if (tournamentInfoLabel) {
        if (typeof formatTournamentMatchContextLine === "function") {
          const tourneyLine = formatTournamentMatchContextLine(data);
          if (tourneyLine) {
            tournamentInfoLabel.textContent = tourneyLine;
            tournamentInfoLabel.hidden = false;
          } else {
            tournamentInfoLabel.textContent = "";
            tournamentInfoLabel.hidden = true;
          }
        }
      }
    } catch (_) {}

    maybeReloadBetweenMatches(status);

    const betweenBattles =
      status === "idle" ||
      status === "starting" ||
      status === "tournament_intro" ||
      status === "intro_gap" ||
      status === "error";

    if (betweenBattles && (currentBattleTag !== null || wasLive)) {
      const duplicateIdleWhileOutro =
        battleOutroTimer != null && status === "idle" && !wasLive;
      if (duplicateIdleWhileOutro) {
        lastPayloadStatus = status;
        return;
      }
      /* Next match phases often arrive before BATTLE_IFRAME_OUTRO_SECONDS elapses
       * (DELAY_BETWEEN_MATCHES, tournament intro). lastPayloadStatus is then "idle",
       * so wasLive is false — without this guard we clear the outro timer and reload
       * the iframe immediately, skipping Showdown's win animation and cluttering the
       * victory splash timing. */
      if (
        battleOutroTimer != null &&
        (status === "starting" ||
          status === "tournament_intro" ||
          status === "intro_gap")
      ) {
        lastPayloadStatus = status;
        return;
      }
      clearBattleOutroTimer();
      const dwellRaw = Number(data.battle_iframe_outro_ms);
      const dwellMs =
        Number.isFinite(dwellRaw) && dwellRaw > 0 ? Math.floor(dwellRaw) : 0;
      /* Rely on the iframe battle tag, not only wasLive — missed "live" frames
       * (SSE debounce/reconnect) would otherwise reset to lobby with no outro. */
      const leavingLiveBattle = currentBattleTag !== null;
      if (leavingLiveBattle && dwellMs > 0) {
        battleOutroTimer = setTimeout(function () {
          battleOutroTimer = null;
          resetBattleFrameToLobby();
        }, dwellMs);
      } else {
        resetBattleFrameToLobby();
      }
      lastPayloadStatus = status;
      return;
    }

    /* Load the spectator room as soon as we have a tag (``starting`` / ``intro_gap``),
     * not only on ``live`` — otherwise the iframe stays on the lobby through intros
     * and Showdown connect latency reads as a long blank battle. */
    const canShowBattleRoom =
      (status === "live" || status === "starting" || status === "intro_gap") &&
      data.battle_tag;
    if (canShowBattleRoom) {
      clearBattleOutroTimer();
      const tag = normalizeBattleTag(data.battle_tag);
      if (tag && tag !== currentBattleTag) {
        currentBattleTag = tag;
        const u = new URL(stableBase);
        u.searchParams.set("_", String(Date.now()));
        battleFrame.src = `${u.toString()}#${tag}`;
        // #region agent log
        if (window._dbg)
          window._dbg("battle:src", "battle", { tag: tag }, "H4");
        // #endregion
      }
    }
    lastPayloadStatus = status;
  }

  function applyFromScoreboardPayload(sb) {
    const shaped = shapeFromScoreboard(sb);
    if (shaped) applyBattleUiFromPayload(shaped);
  }

  if (window.__BROADCAST_SCOREBOARD_HUB__) {
    window.__refreshBattleFromHubScoreboard__ = function (sb) {
      applyFromScoreboardPayload(sb);
    };
  } else if (typeof window.attachScoreboardStream === "function") {
    let lastSeq = 0;
    window.attachScoreboardStream({
      getSeq: function () {
        return lastSeq;
      },
      setSeq: function (n) {
        lastSeq = n;
      },
      applyOrdered: applyFromScoreboardPayload,
      applyFallback: applyFromScoreboardPayload,
      fallbackIntervalMs: 2000,
    });
  } else {
    async function refreshBattleTarget() {
      let data;
      try {
        const resp = await fetch("/current_battle", { cache: "no-store" });
        data = await resp.json();
      } catch (_) {
        return;
      }
      applyBattleUiFromPayload(data);
    }
    refreshBattleTarget();
    setInterval(refreshBattleTarget, 2000);
  }

  /**
   * Hard-reload the Showdown iframe only (parent /broadcast stays loaded).
   * Keeps the current battle room hash when live; otherwise loads lobby base.
   * Callable from DevTools or automation: __reloadShowdownBattleFrame__()
   */
  window.__reloadShowdownBattleFrame__ = function () {
    const u = new URL(stableBase);
    u.searchParams.set("_", String(Date.now()));
    if (currentBattleTag) {
      battleFrame.src = `${u.toString()}#${currentBattleTag}`;
    } else {
      battleFrame.src = u.toString();
    }
  };
})();
