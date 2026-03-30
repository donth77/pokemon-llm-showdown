/**
 * Battle iframe: Showdown URL, /current_battle polling, trainer sprites, memory flush.
 * On /broadcast, the scoreboard hub (250ms) also drives updates via
 * window.__refreshBattleFromHubScoreboard__ so we are not stuck on a dead #battle-
 * room for up to 3s after agents disconnect (common after a game ends, including 2-0
 * series clinches).
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
  /** Last ``status`` applied from scoreboard or /current_battle (for live → idle transitions). */
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

  function scoreboardPayloadToBattleShape(sb) {
    if (!sb || typeof sb !== "object") return null;
    const tc = sb.tournament_context;
    const base = {
      status: sb.battle_status || "idle",
      battle_tag: sb.battle_tag,
      battle_format: sb.battle_format,
      player1_sprite_url: sb.player1_sprite_url,
      player2_sprite_url: sb.player2_sprite_url,
    };
    if (tc && typeof tc === "object") {
      for (const k of Object.keys(tc)) {
        base[k] = tc[k];
      }
    }
    return base;
  }

  const PAGE_LOAD_TIME = Date.now();
  const RELOAD_AFTER_MS = 30 * 60 * 1000;
  let lastSeenStatus = "";

  function maybeReloadBetweenMatches(status) {
    lastSeenStatus = status || "";
    if (
      lastSeenStatus === "idle" &&
      Date.now() - PAGE_LOAD_TIME > RELOAD_AFTER_MS
    ) {
      location.reload();
    }
  }

  function applyBattleUiFromPayload(data) {
    if (!data || typeof data !== "object") return;
    const status = data.status || "idle";
    const wasLive = lastPayloadStatus === "live";

    try {
      lastTrainerSpriteUrls.p1 = String(
        data.player1_sprite_url || "",
      ).trim();
      lastTrainerSpriteUrls.p2 = String(
        data.player2_sprite_url || "",
      ).trim();
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
      currentBattleTag = null;
      const u = new URL(stableBase);
      u.searchParams.set("_", String(Date.now()));
      battleFrame.src = u.toString();
      lastPayloadStatus = status;
      return;
    }

    if (status === "live" && data.battle_tag) {
      const tag = normalizeBattleTag(data.battle_tag);
      if (tag && tag !== currentBattleTag) {
        currentBattleTag = tag;
        const u = new URL(stableBase);
        u.searchParams.set("_", String(Date.now()));
        battleFrame.src = `${u.toString()}#${tag}`;
      }
    }
    lastPayloadStatus = status;
  }

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

  window.__refreshBattleFromHubScoreboard__ = function (sb) {
    applyBattleUiFromPayload(scoreboardPayloadToBattleShape(sb));
  };

  refreshBattleTarget();
  setInterval(refreshBattleTarget, 2000);
})();
