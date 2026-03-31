/**
 * Unified broadcast splash coordinator: tournament intro, victory, bracket/upcoming,
 * match intro — only one layer visible at a time.
 */
(function () {
  "use strict";

  var ACTIVE_MODAL = null;

  var $tintro = document.getElementById("tintro-splash");
  var $intro = document.getElementById("intro-splash");
  var $bracket = document.getElementById("bracket-splash");
  var $splash = document.getElementById("splash");

  var bracketMs = parseInt(
    document.body.getAttribute("data-bracket-ms") || "0",
    10,
  );
  if (!Number.isFinite(bracketMs)) bracketMs = 0;

  var introMs = parseInt(
    document.body.getAttribute("data-match-intro-ms") || "0",
    10,
  );
  if (!Number.isFinite(introMs)) introMs = 0;

  var LIVE_INTRO_MAX_AGE_SEC = 180;
  var RECENT_STARTING_INTRO_MS = 20000;
  var INTRO_SHOWN_SS_KEY = "pokemon_llm_match_intro_shown_v1";
  var CELEBRATED_TOTAL_KEY = "pokemon_showdown_victory_celebrated_total";

  var introShownMem = {};
  var recentStartingIntroByMatch = {};
  var lastSeenManagerMatchIdForIntro = null;

  var pendingIntroData = null;
  var deferredVictory = null;

  var lastTournamentIntroKey = null;
  var tintroHideTimer = null;

  var introHideTimer = null;
  var introBurstTimer = null;

  var bracketHideTimer = null;
  var bracketEndTimer = null;

  var lastMatchFingerprint = null;
  var celebratedTotalMemory = null;
  var victoryHideTimer = null;
  var victoryShowDelayTimer = null;

  var lastBattleUpdatedAt = -1;
  var lastScoreboardSeq = 0;
  var latestScoreboardPayload = null;

  var victoryVisibleMs = parseInt(
    document.body.getAttribute("data-victory-visible-ms") || "30000",
    10,
  );
  if (!Number.isFinite(victoryVisibleMs) || victoryVisibleMs < 1) {
    victoryVisibleMs = 30000;
  }
  var tournamentVictoryVisibleMs = parseInt(
    document.body.getAttribute("data-tournament-victory-visible-ms") || "60000",
    10,
  );
  if (
    !Number.isFinite(tournamentVictoryVisibleMs) ||
    tournamentVictoryVisibleMs < 1
  ) {
    tournamentVictoryVisibleMs = 60000;
  }
  var victoryShowDelayMs = parseInt(
    document.body.getAttribute("data-victory-show-delay-ms") || "1000",
    10,
  );
  if (!Number.isFinite(victoryShowDelayMs) || victoryShowDelayMs < 0) {
    victoryShowDelayMs = 1000;
  }

  var $label = document.getElementById("splash-label");
  var $portrait = document.getElementById("splash-portrait");
  var $name = document.getElementById("splash-name");
  var $sub = document.getElementById("splash-sub");
  var $splashFmt = document.getElementById("splash-format");
  var $splashTourney = document.getElementById("splash-tourney");
  var $splashVs = document.getElementById("splash-vs");
  var $victorySfx = document.getElementById("victory-sfx");
  var $tournamentVictorySfx = document.getElementById("tournament-victory-sfx");

  var $tintroTitle = document.getElementById("tintro-title");
  var $tintroMeta = document.getElementById("tintro-meta");
  var $tintroFmt = document.getElementById("tintro-format");
  var $tintroRoster = document.getElementById("tintro-roster");

  var $p1img = document.getElementById("intro-portrait-p1");
  var $p2img = document.getElementById("intro-portrait-p2");
  var $p1name = document.getElementById("intro-name-p1");
  var $p2name = document.getElementById("intro-name-p2");
  var $fmt = document.getElementById("intro-format");
  var $tourney = document.getElementById("intro-tourney");

  var $bracketNextLine = document.getElementById("bracket-next-line");
  var $bracketVisualWrap = document.getElementById("bracket-visual-wrap");
  var $bracketVisual = document.getElementById("bracket-visual");
  var $bracketFollowingLabel = document.getElementById(
    "bracket-following-label",
  );
  var $bracketList = document.getElementById("bracket-list");

  function primeAudioForAutoplay(el) {
    if (!el) return;
    el.muted = true;
    var p = el.play();
    if (p && typeof p.then === "function") {
      p.then(function () {
        el.pause();
        el.currentTime = 0;
        el.muted = false;
      }).catch(function () {
        el.muted = false;
      });
    } else {
      el.pause();
      el.currentTime = 0;
      el.muted = false;
    }
  }

  window.addEventListener("message", function (evt) {
    var d = evt && evt.data;
    if (!d || d.type !== "user_gesture_unlock") return;
    primeAudioForAutoplay($victorySfx);
    primeAudioForAutoplay($tournamentVictorySfx);
  });

  function stopVictorySfx() {
    if ($victorySfx) {
      $victorySfx.pause();
      $victorySfx.currentTime = 0;
    }
    if ($tournamentVictorySfx) {
      $tournamentVictorySfx.pause();
      $tournamentVictorySfx.currentTime = 0;
      $tournamentVictorySfx.loop = false;
    }
  }

  function playVictorySfx(isTournamentChamp) {
    if (isTournamentChamp && $tournamentVictorySfx) {
      if ($victorySfx) {
        $victorySfx.pause();
        $victorySfx.currentTime = 0;
      }
      $tournamentVictorySfx.loop = true;
      $tournamentVictorySfx.currentTime = 0;
      var pt = $tournamentVictorySfx.play();
      if (pt && typeof pt.catch === "function") pt.catch(function () {});
      return;
    }
    if ($tournamentVictorySfx) {
      $tournamentVictorySfx.pause();
      $tournamentVictorySfx.currentTime = 0;
      $tournamentVictorySfx.loop = false;
    }
    if (!$victorySfx) return;
    $victorySfx.currentTime = 0;
    var p = $victorySfx.play();
    if (p && typeof p.catch === "function") p.catch(function () {});
  }

  /* --- Hide helpers: only one modal visible --- */

  function clearBracketTimers() {
    if (bracketHideTimer) {
      clearTimeout(bracketHideTimer);
      bracketHideTimer = null;
    }
    if (bracketEndTimer) {
      clearTimeout(bracketEndTimer);
      bracketEndTimer = null;
    }
  }

  function hideBracketImmediate() {
    clearBracketTimers();
    if ($bracket) {
      $bracket.classList.remove("visible", "fading");
    }
    if (ACTIVE_MODAL === "bracket") ACTIVE_MODAL = null;
  }

  function hideTournamentImmediate() {
    if (tintroHideTimer) {
      clearTimeout(tintroHideTimer);
      tintroHideTimer = null;
    }
    if ($tintro) $tintro.classList.remove("visible", "fading");
    if (ACTIVE_MODAL === "tournament") ACTIVE_MODAL = null;
  }

  /** Fade out like bracket-splash; optional ``done`` runs after overlay cleanup. */
  function hideTournamentSplashVisual(done) {
    if (!$tintro) {
      if (typeof done === "function") done();
      return;
    }
    if (tintroHideTimer) {
      clearTimeout(tintroHideTimer);
      tintroHideTimer = null;
    }
    $tintro.classList.remove("visible");
    $tintro.classList.add("fading");
    tintroHideTimer = setTimeout(function () {
      $tintro.classList.remove("fading");
      tintroHideTimer = null;
      if (ACTIVE_MODAL === "tournament") ACTIVE_MODAL = null;
      if (typeof done === "function") done();
      else tryPendingIntro();
    }, 520);
  }

  function hideIntroHard() {
    if (introHideTimer) {
      clearTimeout(introHideTimer);
      introHideTimer = null;
    }
    if (introBurstTimer) {
      clearTimeout(introBurstTimer);
      introBurstTimer = null;
    }
    if ($intro) $intro.classList.remove("visible", "fading");
    if (ACTIVE_MODAL === "intro") ACTIVE_MODAL = null;
  }

  function hideVictoryImmediate() {
    if (victoryShowDelayTimer) {
      clearTimeout(victoryShowDelayTimer);
      victoryShowDelayTimer = null;
    }
    if (victoryHideTimer) {
      clearTimeout(victoryHideTimer);
      victoryHideTimer = null;
    }
    stopVictorySfx();
    if ($splash) $splash.classList.remove("visible", "fading");
    if (ACTIVE_MODAL === "victory") ACTIVE_MODAL = null;
  }

  function hideAllExcept(except) {
    if (except !== "tournament") hideTournamentImmediate();
    if (except !== "intro") hideIntroHard();
    if (except !== "bracket") hideBracketImmediate();
    if (except !== "victory") hideVictoryImmediate();
  }

  function victoryInFlight() {
    if (victoryShowDelayTimer != null || victoryHideTimer != null) return true;
    return $splash && $splash.classList.contains("visible");
  }

  function canShowMatchIntroNow() {
    if (ACTIVE_MODAL === "tournament") return false;
    if (activeTournamentVisible()) return false;
    if (victoryInFlight()) return false;
    if (ACTIVE_MODAL === "bracket") return false;
    if (ACTIVE_MODAL === "victory") return false;
    return true;
  }

  function activeTournamentVisible() {
    return $tintro && $tintro.classList.contains("visible");
  }

  function shouldDeferVictory() {
    return activeTournamentVisible() || ACTIVE_MODAL === "tournament";
  }

  /* --- Tournament intro --- */

  function resetTintroBursts() {
    if (!$tintro) return;
    var bursts = $tintro.querySelectorAll(".tintro-burst");
    bursts.forEach(function (el) {
      var clone = el.cloneNode(true);
      el.parentNode.replaceChild(clone, el);
    });
  }

  function tournamentIntroKey(data) {
    var tc = data && data.tournament_context;
    var tid = tc && tc.tournament_id != null ? String(tc.tournament_id) : "";
    if (tid) return "tintro:t" + tid;
    if (data && data.match_id != null)
      return "tintro:m" + String(data.match_id);
    return "";
  }

  function applyTournamentIntro(data) {
    var tc = data.tournament_context || {};
    var tname =
      tc.tournament_name != null ? String(tc.tournament_name).trim() : "";
    $tintroTitle.textContent = tname || "Tournament";

    var bracket =
      typeof displayTournamentBracketType === "function"
        ? displayTournamentBracketType(tc.tournament_type)
        : String(tc.tournament_type || "");
    var bo = tc.tournament_best_of;
    var boN = bo != null && bo !== "" ? parseInt(String(bo), 10) : NaN;
    $tintroMeta.innerHTML = "";
    if (bracket) {
      var s1 = document.createElement("span");
      s1.textContent = bracket;
      $tintroMeta.appendChild(s1);
    }
    if (Number.isFinite(boN) && boN >= 1) {
      var s2 = document.createElement("span");
      s2.textContent = "Best of " + boN;
      $tintroMeta.appendChild(s2);
    }

    var fmtLine = "";
    if (data.battle_format) {
      var df = displayBattleFormat(data.battle_format);
      if (df && df !== "--") fmtLine = df;
    }
    if (fmtLine) {
      $tintroFmt.textContent = fmtLine;
      $tintroFmt.hidden = false;
    } else {
      $tintroFmt.textContent = "";
      $tintroFmt.hidden = true;
    }

    $tintroRoster.innerHTML = "";
    var roster = data.tournament_intro_roster;
    if (Array.isArray(roster) && roster.length) {
      roster.forEach(function (row) {
        var url = (row.portrait_square_url || "").trim();
        var slug = (row.persona_slug || "").trim();
        if (!url && !slug) return;
        var img = document.createElement("img");
        img.className = "tintro-avatar";
        img.alt = slug || "Trainer";
        if (url) {
          img.src = url;
          img.onerror = function () {
            img.removeAttribute("src");
            img.style.opacity = "0.35";
          };
        } else {
          img.style.opacity = "0.35";
        }
        $tintroRoster.appendChild(img);
      });
    }
  }

  function showTournamentIntroLayer(data) {
    hideAllExcept("tournament");
    var k = tournamentIntroKey(data);
    if (
      k &&
      lastTournamentIntroKey === k &&
      $tintro.classList.contains("visible")
    ) {
      ACTIVE_MODAL = "tournament";
      return;
    }
    lastTournamentIntroKey = k || null;
    applyTournamentIntro(data);
    resetTintroBursts();
    if (tintroHideTimer) {
      clearTimeout(tintroHideTimer);
      tintroHideTimer = null;
    }
    $tintro.classList.remove("fading");
    void $tintro.offsetWidth;
    $tintro.classList.add("visible");
    ACTIVE_MODAL = "tournament";
  }

  function hideTournamentIntroOnGap(done) {
    lastTournamentIntroKey = null;
    hideTournamentSplashVisual(done);
  }

  /* --- Bracket / upcoming --- */

  function formatQueueRow(r) {
    if (!r || typeof r !== "object") return "";
    var a = String(
      r.player1_stream_label || r.player1_persona_label || "",
    ).trim();
    var b = String(
      r.player2_stream_label || r.player2_persona_label || "",
    ).trim();
    var line;
    if (r.pending_slot) {
      line = (a || "TBD") + " vs " + (b || "TBD") + " (pending)";
    } else {
      line = (a || "Player 1") + " vs " + (b || "Player 2");
    }
    if (r.queue_game_if_necessary) {
      line += " (if necessary)";
    }
    return line;
  }

  function hideBracketSplashVisual() {
    if (!$bracket) return;
    if (bracketHideTimer) {
      clearTimeout(bracketHideTimer);
      bracketHideTimer = null;
    }
    $bracket.classList.remove("visible");
    $bracket.classList.add("fading");
    bracketHideTimer = setTimeout(function () {
      $bracket.classList.remove("fading");
      bracketHideTimer = null;
      if (ACTIVE_MODAL === "bracket") ACTIVE_MODAL = null;
      tryPendingIntro();
    }, 520);
  }

  function tournamentContextHasId(scoreboardData) {
    var tc = (scoreboardData && scoreboardData.tournament_context) || {};
    return tc.tournament_id != null && String(tc.tournament_id).trim() !== "";
  }

  function runBracketPhase(scoreboardData) {
    if (bracketMs < 1) {
      tryPendingIntro();
      return;
    }
    if (!tournamentContextHasId(scoreboardData)) {
      tryPendingIntro();
      return;
    }
    var tc = (scoreboardData && scoreboardData.tournament_context) || {};
    var tidRaw = tc.tournament_id;
    var tidNum = parseInt(String(tidRaw), 10);
    if (!Number.isFinite(tidNum)) {
      tryPendingIntro();
      return;
    }
    Promise.all([
      fetch("/api/manager/queue/upcoming?limit=12", {
        credentials: "same-origin",
      }).then(function (r) {
        return r.ok ? r.json() : [];
      }),
      fetch("/api/manager/tournaments/" + encodeURIComponent(String(tidNum)), {
        credentials: "same-origin",
      }).then(function (r) {
        return r.ok ? r.json() : null;
      }),
    ])
      .then(function (pair) {
        var rows = pair[0];
        var tournament = pair[1];
        var rawList = Array.isArray(rows) ? rows : [];
        var list = rawList.filter(function (r) {
          var rt = r && r.tournament_id;
          if (rt == null || rt === "") return false;
          return Number(rt) === tidNum;
        });
        if (list.length < 1) {
          tryPendingIntro();
          return;
        }
        hideAllExcept("bracket");
        ACTIVE_MODAL = "bracket";
        var first = list[0];
        var rest = list.slice(1, 5);
        if ($bracketNextLine) {
          var nline =
            typeof formatBracketInterstitialNextLine === "function"
              ? formatBracketInterstitialNextLine(first)
              : "";
          if (nline) {
            $bracketNextLine.textContent = nline;
            $bracketNextLine.hidden = false;
          } else {
            $bracketNextLine.textContent = "";
            $bracketNextLine.hidden = true;
          }
        }
        if (
          $bracketVisual &&
          tournament &&
          typeof renderBracketInterstitialVisual === "function"
        ) {
          $bracketVisual.innerHTML = "";
          var sid = first.series_id;
          var nextSeries = null;
          if (tournament.series && sid != null) {
            var sidN = Number(sid);
            nextSeries =
              tournament.series.find(function (s) {
                return Number(s.id) === sidN;
              }) || null;
          }
          renderBracketInterstitialVisual($bracketVisual, tournament, {
            highlightSeriesId: sid != null ? sid : null,
            nextSeries: nextSeries,
          });
          var hasVis = $bracketVisual.childNodes.length > 0;
          if ($bracketVisualWrap) $bracketVisualWrap.hidden = !hasVis;
        } else {
          if ($bracketVisualWrap) $bracketVisualWrap.hidden = true;
          if ($bracketVisual) $bracketVisual.innerHTML = "";
        }
        if ($bracketFollowingLabel) {
          $bracketFollowingLabel.hidden = rest.length < 1;
        }
        if ($bracketList) {
          $bracketList.innerHTML = "";
          rest.forEach(function (row) {
            var li = document.createElement("li");
            li.textContent = formatQueueRow(row);
            $bracketList.appendChild(li);
          });
        }
        var bursts = $bracket.querySelectorAll(".bracket-burst");
        bursts.forEach(function (el) {
          var clone = el.cloneNode(true);
          el.parentNode.replaceChild(clone, el);
        });
        $bracket.classList.remove("fading");
        void $bracket.offsetWidth;
        $bracket.classList.add("visible");
        bracketEndTimer = setTimeout(function () {
          bracketEndTimer = null;
          hideBracketSplashVisual();
        }, bracketMs);
      })
      .catch(function () {
        tryPendingIntro();
      });
  }

  function afterVictoryPipeline() {
    var d = latestScoreboardPayload;
    if (bracketMs >= 1) {
      runBracketPhase(d);
    } else {
      tryPendingIntro();
    }
  }

  /* --- Victory --- */

  function resetVictoryBursts() {
    if (!$splash) return;
    var bursts = $splash.querySelectorAll(".burst");
    bursts.forEach(function (el) {
      var clone = el.cloneNode(true);
      el.parentNode.replaceChild(clone, el);
    });
  }

  function runTournamentConfetti() {
    if (typeof confetti !== "function") return;
    var w = window.innerWidth || 1280;
    var h = window.innerHeight || 720;
    var positions = [
      { x: w * 0.5, y: h * 0.36 },
      { x: w * 0.2, y: h * 0.26 },
      { x: w * 0.8, y: h * 0.26 },
      { x: w * 0.34, y: h * 0.5 },
      { x: w * 0.66, y: h * 0.5 },
    ];
    positions.forEach(function (pos, i) {
      setTimeout(function () {
        confetti({
          position: pos,
          count: 100,
          size: 1.15,
          velocity: 240,
          fade: true,
        });
      }, i * 180);
    });
  }

  function showVictory(
    match,
    p1,
    p2,
    p1Portrait,
    p2Portrait,
    p1Sprite,
    p2Sprite,
  ) {
    hideAllExcept("victory");

    if (victoryHideTimer) {
      clearTimeout(victoryHideTimer);
      victoryHideTimer = null;
    }
    if (victoryShowDelayTimer) {
      clearTimeout(victoryShowDelayTimer);
      victoryShowDelayTimer = null;
    }
    stopVictorySfx();

    var winner = (match && match.winner) || "Unknown";
    var isDraw = winner === "Draw";

    function pickArt(portrait, sprite) {
      var a = (portrait || "").trim();
      if (a) return a;
      return (sprite || "").trim();
    }

    var artUrl = "";
    if (!isDraw) {
      if (winner === p1) artUrl = pickArt(p1Portrait, p1Sprite);
      else if (winner === p2) artUrl = pickArt(p2Portrait, p2Sprite);
    }
    $portrait.classList.remove("is-visible");
    $portrait.onload = null;
    $portrait.onerror = null;
    if (artUrl) {
      $portrait.alt = winner + " portrait";
      $portrait.onload = function () {
        $portrait.classList.add("is-visible");
      };
      $portrait.onerror = function () {
        $portrait.classList.remove("is-visible");
        $portrait.removeAttribute("src");
      };
      if ($portrait.getAttribute("src") === artUrl) {
        $portrait.removeAttribute("src");
        void $portrait.offsetWidth;
      }
      $portrait.src = artUrl;
    } else {
      $portrait.alt = "";
      $portrait.removeAttribute("src");
    }

    var isTournamentChamp =
      !isDraw && match && match.victory_tournament_clinched;
    var victoryDisplayMs = isTournamentChamp
      ? tournamentVictoryVisibleMs
      : victoryVisibleMs;
    $label.textContent = victorySplashTitleLabel(match, isDraw);
    $label.className = "label" + (isTournamentChamp ? " tournament-champ" : "");
    $name.className = "winner-name";
    if (isDraw) {
      $name.classList.add("draw");
    } else if (isTournamentChamp) {
      $name.classList.add("tournament-champ");
    } else if (winner === p1) {
      $name.classList.add("p1");
    } else if (winner === p2) {
      $name.classList.add("p2");
    }
    $name.textContent = winner;

    var fmtLine = "";
    if (match && match.battle_format) {
      var fmt = displayBattleFormat(match.battle_format);
      if (fmt && fmt !== "--") fmtLine = fmt;
    }
    if (fmtLine && $splashFmt) {
      $splashFmt.textContent = fmtLine;
      $splashFmt.hidden = false;
    } else if ($splashFmt) {
      $splashFmt.textContent = "";
      $splashFmt.hidden = true;
    }

    var tourneyLine = match ? formatTournamentMatchContextLine(match) : "";
    if (tourneyLine && $splashTourney) {
      $splashTourney.textContent = tourneyLine;
      $splashTourney.hidden = false;
    } else if ($splashTourney) {
      $splashTourney.textContent = "";
      $splashTourney.hidden = true;
    }

    if (p1 && p2 && $splashVs) {
      $splashVs.textContent = p1 + " vs " + p2;
      $splashVs.hidden = false;
    } else if ($splashVs) {
      $splashVs.textContent = "";
      $splashVs.hidden = true;
    }

    if ($sub) {
      var anyMeta =
        ($splashFmt && !$splashFmt.hidden) ||
        ($splashTourney && !$splashTourney.hidden) ||
        ($splashVs && !$splashVs.hidden);
      $sub.style.display = anyMeta ? "" : "none";
    }

    victoryShowDelayTimer = setTimeout(function () {
      victoryShowDelayTimer = null;
      $splash.classList.remove("fading");
      resetVictoryBursts();
      void $splash.offsetWidth;
      $splash.classList.add("visible");
      ACTIVE_MODAL = "victory";
      playVictorySfx(isTournamentChamp);
      if (
        match &&
        match.victory_tournament_clinched &&
        (match.winner || "") !== "Draw"
      ) {
        runTournamentConfetti();
      }

      victoryHideTimer = setTimeout(function () {
        $splash.classList.add("fading");
        victoryHideTimer = setTimeout(function () {
          stopVictorySfx();
          $splash.classList.remove("visible", "fading");
          victoryHideTimer = null;
          if (ACTIVE_MODAL === "victory") ACTIVE_MODAL = null;
          afterVictoryPipeline();
        }, 500);
      }, victoryDisplayMs);
    }, victoryShowDelayMs);
  }

  function readCelebratedTotalStored() {
    try {
      var s = sessionStorage.getItem(CELEBRATED_TOTAL_KEY);
      if (s === null || s === "") return null;
      var n = parseInt(s, 10);
      return Number.isNaN(n) ? null : n;
    } catch (_e) {
      return null;
    }
  }

  function writeCelebratedTotalStored(n) {
    try {
      sessionStorage.setItem(CELEBRATED_TOTAL_KEY, String(n));
    } catch (_e) {}
  }

  function syncCelebratedCursor(tm) {
    var stored = readCelebratedTotalStored();
    if (stored !== null) {
      celebratedTotalMemory = stored;
    } else if (celebratedTotalMemory === null) {
      celebratedTotalMemory = tm;
      writeCelebratedTotalStored(tm);
    }
    if (tm < celebratedTotalMemory) {
      celebratedTotalMemory = tm;
      writeCelebratedTotalStored(tm);
    }
    return celebratedTotalMemory;
  }

  function advanceCelebratedCursor(tm) {
    celebratedTotalMemory = tm;
    writeCelebratedTotalStored(tm);
  }

  function fingerprintLastMatch(m) {
    if (!m || m.id == null) return "";
    var ts = m.timestamp != null ? m.timestamp : "";
    return String(m.id) + ":" + String(ts);
  }

  function buildVictorySplashArgs(data, lm, p1, p2) {
    var vP1 = (lm && lm.player1_name) || p1;
    var vP2 = (lm && lm.player2_name) || p2;
    var vP1portrait =
      (lm && lm.player1_portrait_square_url) ||
      data.player1_portrait_square_url ||
      "";
    var vP2portrait =
      (lm && lm.player2_portrait_square_url) ||
      data.player2_portrait_square_url ||
      "";
    var vP1sprite = (lm && lm.player1_sprite_url) || "";
    var vP2sprite = (lm && lm.player2_sprite_url) || "";
    return {
      vP1: vP1,
      vP2: vP2,
      vP1portrait: vP1portrait,
      vP2portrait: vP2portrait,
      vP1sprite: vP1sprite,
      vP2sprite: vP2sprite,
    };
  }

  function flushDeferredVictory() {
    if (!deferredVictory) return;
    var dv = deferredVictory;
    deferredVictory = null;
    showVictory(
      dv.lm,
      dv.vx.vP1,
      dv.vx.vP2,
      dv.vx.vP1portrait,
      dv.vx.vP2portrait,
      dv.vx.vP1sprite,
      dv.vx.vP2sprite,
    );
  }

  function processVictoryDetection(data) {
    if (!data) return;
    var st = data.battle_status || "idle";
    var p1 = data.player1_name || "Player 1";
    var p2 = data.player2_name || "Player 2";
    var lm = data.last_match;
    var fp = fingerprintLastMatch(lm);
    var tm = Number(data.total_matches) || 0;
    var celebrated = syncCelebratedCursor(tm);
    var newByTotal = tm > celebrated;
    var newByFingerprint =
      Boolean(fp) &&
      lastMatchFingerprint !== null &&
      fp !== lastMatchFingerprint;

    if ((newByTotal || newByFingerprint) && lm) {
      advanceCelebratedCursor(tm);
      if (fp) lastMatchFingerprint = fp;
      var vx = buildVictorySplashArgs(data, lm, p1, p2);
      if (shouldDeferVictory()) {
        deferredVictory = { lm: lm, vx: vx };
      } else {
        showVictory(
          lm,
          vx.vP1,
          vx.vP2,
          vx.vP1portrait,
          vx.vP2portrait,
          vx.vP1sprite,
          vx.vP2sprite,
        );
      }
    } else if (lastMatchFingerprint === null && fp) {
      lastMatchFingerprint = fp;
    }

    if (
      st === "tournament_intro" ||
      st === "intro_gap" ||
      st === "starting" ||
      st === "live"
    ) {
      if (!victoryInFlight()) {
        hideVictoryImmediate();
      }
    }
  }

  /* --- Match intro (dedupe + sequencing) --- */

  function introShownMap() {
    try {
      var raw = sessionStorage.getItem(INTRO_SHOWN_SS_KEY);
      var o = raw ? JSON.parse(raw) : {};
      return o && typeof o === "object" ? o : {};
    } catch (e) {
      return {};
    }
  }

  function introDedupeScopePrefix(d) {
    if (!d) return "";
    var tc = d.tournament_context || {};
    var tid = tc.tournament_id;
    if (tid != null && String(tid).trim() !== "") {
      return "t" + String(tid) + ":";
    }
    return "";
  }

  function recentStartingIntroKey(d) {
    if (!d || d.match_id == null || String(d.match_id) === "") return "";
    return introDedupeScopePrefix(d) + "m:" + String(d.match_id);
  }

  function matchIntroDedupeKey(d) {
    if (!d) return "";
    var scope = introDedupeScopePrefix(d);
    if (d.match_id != null && String(d.match_id) !== "")
      return scope + "m:" + String(d.match_id);
    var p1 = String(d.player1_name || "");
    var p2 = String(d.player2_name || "");
    var fmt = String(d.battle_format || "");
    if (!p1 && !p2) return "";
    var tc = d.tournament_context || {};
    var gn = tc.game_number != null ? String(tc.game_number) : "";
    var gPart = gn ? "|g" + gn : "";
    return scope + "nomid:" + p1 + "|" + p2 + "|" + fmt + gPart;
  }

  function introDedupeKeyFromBattleTag(d) {
    if (!d || d.battle_tag == null) return "";
    var t = String(d.battle_tag).trim();
    if (!t) return "";
    var scope = introDedupeScopePrefix(d);
    return scope + "bt:" + t.replace(/^>+/, "").trim();
  }

  /** Keep dedupe storage bounded; drop same keys from introShownMem as sessionStorage (lexicographic trim matches current behavior). */
  function trimIntroShownMapKeys(map) {
    var keys = Object.keys(map);
    if (keys.length <= 100) return;
    keys.sort();
    for (var i = 0; i < keys.length - 60; i++) {
      var oldKey = keys[i];
      delete map[oldKey];
      delete introShownMem[oldKey];
    }
  }

  /** Timestamps past RECENT_STARTING_INTRO_MS are ignored by introWasShownFor; dropping them is behavior-neutral. */
  function pruneRecentStartingIntroByMatch() {
    var now = Date.now();
    var keys = Object.keys(recentStartingIntroByMatch);
    for (var i = 0; i < keys.length; i++) {
      var rk = keys[i];
      var t0 = recentStartingIntroByMatch[rk];
      if (!t0 || now - t0 >= RECENT_STARTING_INTRO_MS) {
        delete recentStartingIntroByMatch[rk];
      }
    }
  }

  function persistBattleTagDedupe(btKey) {
    if (!btKey) return;
    introShownMem[btKey] = 1;
    try {
      var map = introShownMap();
      map[btKey] = 1;
      trimIntroShownMapKeys(map);
      sessionStorage.setItem(INTRO_SHOWN_SS_KEY, JSON.stringify(map));
    } catch (e) {}
  }

  function introWasShownFor(d) {
    if (!d) return false;
    var st = d.battle_status || "idle";
    var btKey = introDedupeKeyFromBattleTag(d);
    if (st === "live" && btKey) {
      if (introShownMem[btKey] || introShownMap()[btKey]) return true;
      var rsKey = recentStartingIntroKey(d);
      var t0 = rsKey ? recentStartingIntroByMatch[rsKey] : 0;
      if (t0 && Date.now() - t0 < RECENT_STARTING_INTRO_MS) {
        persistBattleTagDedupe(btKey);
        return true;
      }
      return false;
    }
    var k = matchIntroDedupeKey(d);
    if (!k) return false;
    if (introShownMem[k]) return true;
    return !!introShownMap()[k];
  }

  function markIntroShownFor(d) {
    if (!d) return;
    if (d.battle_status === "starting" && d.match_id != null) {
      var rsKey = recentStartingIntroKey(d);
      if (rsKey) recentStartingIntroByMatch[rsKey] = Date.now();
    }
    pruneRecentStartingIntroByMatch();
    var btKey = introDedupeKeyFromBattleTag(d);
    var k = matchIntroDedupeKey(d);
    if (!btKey && !k) return;
    if (btKey) introShownMem[btKey] = 1;
    if (k) introShownMem[k] = 1;
    try {
      var map = introShownMap();
      if (btKey) map[btKey] = 1;
      if (k) map[k] = 1;
      trimIntroShownMapKeys(map);
      sessionStorage.setItem(INTRO_SHOWN_SS_KEY, JSON.stringify(map));
    } catch (e) {}
  }

  function clearMatchIntroDedupeForManagerMatch(d) {
    if (!d) return;
    var k = matchIntroDedupeKey(d);
    if (!k) return;
    delete introShownMem[k];
    var rsKey = recentStartingIntroKey(d);
    if (rsKey) delete recentStartingIntroByMatch[rsKey];
    try {
      var map = introShownMap();
      if (map[k]) {
        delete map[k];
        sessionStorage.setItem(INTRO_SHOWN_SS_KEY, JSON.stringify(map));
      }
    } catch (e) {}
  }

  function liveBattleFreshEnoughForIntro(d) {
    var u = d.battle_updated_at;
    if (u == null) return false;
    var t = typeof u === "number" ? u : parseFloat(String(u), 10);
    if (!Number.isFinite(t)) return false;
    return Date.now() / 1000 - t < LIVE_INTRO_MAX_AGE_SEC;
  }

  function resetIntroBursts() {
    if (!$intro) return;
    var bursts = $intro.querySelectorAll(".intro-burst");
    bursts.forEach(function (el) {
      var clone = el.cloneNode(true);
      el.parentNode.replaceChild(clone, el);
    });
  }

  function hideIntroSplashAnimated() {
    if (introHideTimer) {
      clearTimeout(introHideTimer);
      introHideTimer = null;
    }
    if (introBurstTimer) {
      clearTimeout(introBurstTimer);
      introBurstTimer = null;
    }
    $intro.classList.remove("visible");
    $intro.classList.add("fading");
    introHideTimer = setTimeout(function () {
      $intro.classList.remove("fading");
      introHideTimer = null;
      if (ACTIVE_MODAL === "intro") ACTIVE_MODAL = null;
    }, 600);
  }

  function pickArt(portrait, sprite) {
    var a = (portrait || "").trim();
    if (a) return a;
    return (sprite || "").trim();
  }

  function setPortraitImg(el, url, alt) {
    if (!el) return;
    el.onload = null;
    el.onerror = null;
    var u = (url || "").trim();
    if (!u) {
      el.removeAttribute("src");
      el.alt = "";
      el.style.opacity = "0.35";
      return;
    }
    el.alt = alt || "";
    el.style.opacity = "1";
    el.onerror = function () {
      el.removeAttribute("src");
      el.style.opacity = "0.35";
    };
    if (el.getAttribute("src") === u) {
      el.removeAttribute("src");
      void el.offsetWidth;
    }
    el.src = u;
  }

  function applyIntroFromScoreboard(data) {
    var p1 = data.player1_name || "Player 1";
    var p2 = data.player2_name || "Player 2";
    $p1name.textContent = p1;
    $p2name.textContent = p2;
    setPortraitImg(
      $p1img,
      pickArt(data.player1_portrait_square_url, data.player1_sprite_url),
      p1,
    );
    setPortraitImg(
      $p2img,
      pickArt(data.player2_portrait_square_url, data.player2_sprite_url),
      p2,
    );

    if ($fmt) {
      var fmtLine = "";
      if (data.battle_format) {
        var df = displayBattleFormat(data.battle_format);
        if (df && df !== "--") fmtLine = df;
      }
      if (fmtLine) {
        $fmt.textContent = fmtLine;
        $fmt.hidden = false;
      } else {
        $fmt.textContent = "";
        $fmt.hidden = true;
      }
    }

    if ($tourney) {
      var tc = data.tournament_context || {};
      var tline =
        typeof formatTournamentMatchContextLine === "function"
          ? formatTournamentMatchContextLine(tc)
          : "";
      if (tline) {
        $tourney.textContent = tline;
        $tourney.hidden = false;
      } else {
        $tourney.textContent = "";
        $tourney.hidden = true;
      }
    }
  }

  function showIntroSequence(data) {
    if (introMs < 1) return;
    hideAllExcept("intro");
    markIntroShownFor(data);
    applyIntroFromScoreboard(data);
    resetIntroBursts();
    if (introHideTimer) {
      clearTimeout(introHideTimer);
      introHideTimer = null;
    }
    if (introBurstTimer) clearTimeout(introBurstTimer);
    $intro.classList.remove("fading");
    void $intro.offsetWidth;
    $intro.classList.add("visible");
    ACTIVE_MODAL = "intro";
    introBurstTimer = setTimeout(function () {
      introBurstTimer = null;
      introHideTimer = setTimeout(function () {
        hideIntroSplashAnimated();
      }, introMs);
    }, 50);
  }

  function pregameFingerprint(d) {
    if (!d || d.battle_status !== "starting") return "";
    var mid = d.match_id != null ? String(d.match_id) : "";
    var u = d.battle_updated_at != null ? String(d.battle_updated_at) : "";
    var p1 = String(d.player1_name || "");
    var p2 = String(d.player2_name || "");
    var fmt = String(d.battle_format || "");
    return mid + "|" + u + "|" + p1 + "|" + p2 + "|" + fmt;
  }

  function tryPendingIntro() {
    if (!pendingIntroData) return;
    var d = pendingIntroData;
    var st = d.battle_status || "idle";
    if (st !== "starting" && st !== "live") {
      pendingIntroData = null;
      return;
    }
    if (!canShowMatchIntroNow()) return;
    if (introMs < 1) {
      pendingIntroData = null;
      return;
    }
    if (introWasShownFor(d)) {
      pendingIntroData = null;
      return;
    }
    var isStarting = st === "starting";
    var isLive = st === "live";
    if (isStarting) {
      if (!pregameFingerprint(d)) {
        return;
      }
    } else if (isLive) {
      if (!liveBattleFreshEnoughForIntro(d) || !matchIntroDedupeKey(d)) {
        pendingIntroData = null;
        return;
      }
    }
    pendingIntroData = null;
    showIntroSequence(d);
  }

  function processMatchIntroSection(data) {
    if (!data) return;
    var st = data.battle_status || "idle";
    if (st === "tournament_intro" || st === "intro_gap") return;

    if (introMs < 1) return;
    if (introWasShownFor(data)) return;

    var isStarting = st === "starting";
    var isLive = st === "live";

    if (isStarting) {
      var fp = pregameFingerprint(data);
      if (!fp) return;
      if (!canShowMatchIntroNow()) {
        pendingIntroData = data;
        return;
      }
      showIntroSequence(data);
      return;
    }

    if (
      isLive &&
      liveBattleFreshEnoughForIntro(data) &&
      matchIntroDedupeKey(data)
    ) {
      if (!canShowMatchIntroNow()) {
        pendingIntroData = data;
        return;
      }
      showIntroSequence(data);
    }
  }

  function processScoreboardData(data, fromOrderedHub) {
    latestScoreboardPayload = data;
    if (!data) return;

    var u = data.battle_updated_at;
    var tu = NaN;
    if (u != null) {
      tu = typeof u === "number" ? u : parseFloat(String(u), 10);
    }
    var st = data.battle_status || "idle";

    if (
      !fromOrderedHub &&
      (st === "tournament_intro" || st === "intro_gap") &&
      Number.isFinite(tu) &&
      tu < lastBattleUpdatedAt - 1e-6
    ) {
      return;
    }

    if (Number.isFinite(tu)) {
      lastBattleUpdatedAt = Math.max(lastBattleUpdatedAt, tu);
    }

    if (st === "tournament_intro") {
      clearMatchIntroDedupeForManagerMatch(data);
      showTournamentIntroLayer(data);
      processVictoryDetection(data);
      return;
    }

    if (st === "intro_gap") {
      clearMatchIntroDedupeForManagerMatch(data);
      hideTournamentIntroOnGap(function () {
        flushDeferredVictory();
        tryPendingIntro();
      });
    }

    var midRaw = data.match_id;
    if (midRaw != null && String(midRaw) !== "") {
      var midNum = Number(midRaw);
      if (
        Number.isFinite(midNum) &&
        midNum !== lastSeenManagerMatchIdForIntro
      ) {
        lastSeenManagerMatchIdForIntro = midNum;
        clearMatchIntroDedupeForManagerMatch(data);
      }
    }

    if (
      $intro.classList.contains("visible") &&
      (st === "idle" || st === "error")
    ) {
      hideIntroSplashAnimated();
    }

    processVictoryDetection(data);
    processMatchIntroSection(data);
  }

  var useBroadcastHub = /\bembed=broadcast\b/.test(location.search || "");

  function onHubScoreboardMessage(ev) {
    if (ev.origin !== window.location.origin) return;
    var d = ev.data;
    if (!d || d.type !== "scoreboard" || !d.payload) return;
    var orderedHub = false;
    if (typeof d.seq === "number") {
      if (d.seq === lastScoreboardSeq) return;
      lastScoreboardSeq = d.seq;
      orderedHub = true;
    }
    processScoreboardData(d.payload, orderedHub);
  }

  if (useBroadcastHub) {
    window.addEventListener("message", onHubScoreboardMessage);
    fetch("/scoreboard", { cache: "no-store" })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data) processScoreboardData(data);
      })
      .catch(function () {});
  } else {
    window.attachScoreboardStream({
      getSeq: function () {
        return lastScoreboardSeq;
      },
      setSeq: function (n) {
        lastScoreboardSeq = n;
      },
      applyOrdered: function (payload) {
        processScoreboardData(payload, true);
      },
      applyFallback: function (payload) {
        processScoreboardData(payload);
      },
      onFetchError: function (err) {
        console.warn("[broadcast-splashes] scoreboard poll failed:", err);
      },
    });
  }
})();
