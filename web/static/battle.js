/**
 * Battle Control Page — SSE state subscription, move selection, action submission.
 * Reads window.__BATTLE_CONFIG__ for matchId, stateStreamUrl, actionUrl, thoughtsWsUrl.
 */
(function () {
  "use strict";

  var cfg = window.__BATTLE_CONFIG__;
  if (!cfg) return;

  // --- DOM refs ---
  var opponentPortrait = document.getElementById("opponent-portrait");
  var opponentName = document.getElementById("opponent-name");
  var opponentPokemon = document.getElementById("opponent-pokemon");
  var calloutBubble = document.getElementById("callout-bubble");
  var reasoningPanel = document.getElementById("reasoning-panel");
  var fieldConditions = document.getElementById("field-conditions");
  var turnInfo = document.getElementById("turn-info");
  var yourPokemonName = document.getElementById("your-pokemon-name");
  var yourPokemonTypes = document.getElementById("your-pokemon-types");
  var yourPokemonDetail = document.getElementById("your-pokemon-detail");
  var yourHpBar = document.getElementById("your-hp-bar");
  var yourHpText = document.getElementById("your-hp-text");
  var actionsPanel = document.getElementById("actions-panel");
  var actionsGrid = document.getElementById("actions-grid");
  var statusBanner = document.getElementById("status-banner");
  var footerBar = document.getElementById("footer-bar");
  var timerEl = document.getElementById("timer");
  var submitBtn = document.getElementById("submit-btn");
  var resultOverlay = document.getElementById("result-overlay");
  var resultTitle = document.getElementById("result-title");
  var resultDetail = document.getElementById("result-detail");

  var selectedAction = null; // { action_type, index }
  var timerInterval = null;
  var turnDeadline = null;
  var matchFinished = false;
  var latestState = null;
  var reasoningItems = []; // [{turn, reasoning}]

  // --- Type colors ---
  var TYPE_COLORS = {
    normal:"#a8a878", fire:"#f08030", water:"#6890f0", electric:"#f8d030",
    grass:"#78c850", ice:"#98d8d8", fighting:"#c03028", poison:"#a040a0",
    ground:"#e0c068", flying:"#a890f0", psychic:"#f85888", bug:"#a8b820",
    rock:"#b8a038", ghost:"#705898", dragon:"#7038f8", dark:"#705848",
    steel:"#b8b8d0", fairy:"#ee99ac", stellar:"#40b5a0"
  };

  // --- Helpers ---
  function hpColor(pct) {
    if (pct > 50) return "green";
    if (pct > 20) return "yellow";
    return "red";
  }

  function typeBadge(t) {
    var cls = "type-badge type-" + (t || "normal");
    return '<span class="' + cls + '">' + esc(t || "?") + "</span>";
  }

  function effBadge(eff) {
    if (eff == null) return "";
    if (eff === 0) return '<span class="eff-badge eff-immune">0x</span>';
    if (eff < 1) return '<span class="eff-badge eff-resist">' + eff + "x</span>";
    if (eff === 1) return '<span class="eff-badge eff-neutral">1x</span>';
    if (eff <= 2) return '<span class="eff-badge eff-super">' + eff + "x</span>";
    return '<span class="eff-badge eff-ultra">' + eff + "x</span>";
  }

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  function formatTimer(seconds) {
    var m = Math.floor(seconds / 60);
    var s = Math.floor(seconds % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  // --- Render battle state ---
  function renderState(state) {
    latestState = state;
    if (matchFinished) return;

    // Load battle iframe as soon as we know the tag.
    if (state.battle_tag) loadBattleIframe(state.battle_tag);

    // Opponent active pokemon
    var opp = state.opponent_active;
    if (opp) {
      var oppTypes = (opp.types || []).map(typeBadge).join(" ");
      var oppStatus = opp.status ? ' <span style="color:var(--accent-red)">[' + esc(opp.status) + "]</span>" : "";
      var oppBoosts = "";
      if (opp.boosts && Object.keys(opp.boosts).length) {
        oppBoosts = " | " + Object.entries(opp.boosts).map(function(e){ return e[0]+":" +(e[1]>0?"+":"")+e[1]; }).join(" ");
      }
      opponentPokemon.innerHTML =
        "<strong>" + esc(opp.species) + "</strong> " + oppTypes +
        " | HP: " + opp.hp_pct + "%" + oppStatus +
        (opp.ability ? " | " + esc(opp.ability) : "") +
        (opp.item ? " | " + esc(opp.item) : "") +
        oppBoosts;
    } else {
      opponentPokemon.innerHTML = "";
    }

    // Your active pokemon
    var you = state.active_pokemon;
    if (you) {
      yourPokemonName.textContent = you.species || "--";
      yourPokemonTypes.innerHTML = (you.types || []).map(typeBadge).join(" ");
      var details = [];
      if (you.ability) details.push(you.ability);
      if (you.item) details.push(you.item);
      if (you.status) details.push(you.status);
      if (you.boosts && Object.keys(you.boosts).length) {
        details.push(Object.entries(you.boosts).map(function(e){ return e[0]+":"+(e[1]>0?"+":"")+e[1]; }).join(" "));
      }
      yourPokemonDetail.textContent = details.join(" | ");
      yourHpBar.style.width = you.hp_pct + "%";
      yourHpBar.className = "hp-bar " + hpColor(you.hp_pct);
      yourHpText.textContent = you.hp_pct + "%";
    }

    // Field conditions
    var fc = [];
    if (state.field) {
      if (state.field.weather) fc.push("Weather: " + state.field.weather);
      if (state.field.terrain && state.field.terrain.length) fc.push("Terrain: " + state.field.terrain.join(", "));
      if (state.field.your_side && state.field.your_side.length) fc.push("Your side: " + state.field.your_side.join(", "));
      if (state.field.opponent_side && state.field.opponent_side.length) fc.push("Opp side: " + state.field.opponent_side.join(", "));
    }
    fieldConditions.innerHTML = fc.length ? fc.map(function(c){ return "<span>" + esc(c) + "</span>"; }).join("") : "<span>No field effects</span>";
    turnInfo.textContent = "Turn " + (state.turn || "?") + " | You: " + (state.your_remaining || "?") + " | Opp: " + (state.opponent_remaining || "?");

    // Render action buttons
    renderActions(state);

    // Show action panel, hide waiting banner
    statusBanner.style.display = "none";
    actionsPanel.style.display = "";
    footerBar.style.display = "";

    // Start turn timer
    startTimer();

    // Clear selection
    selectedAction = null;
    submitBtn.disabled = true;
  }

  function renderActions(state) {
    var html = "";
    var moves = state.available_moves || [];
    var switches = state.available_switches || [];
    var forceSwitch = state.force_switch;

    if (!forceSwitch && moves.length) {
      html += '<div class="action-section-label">Moves</div>';
      for (var i = 0; i < moves.length; i++) {
        var m = moves[i];
        var acc = m.always_hits ? "---" : (m.accuracy != null ? m.accuracy + "%" : "---");
        var pow = m.power > 0 ? m.power : "---";
        html +=
          '<button class="action-btn" data-type="move" data-index="' + m.index + '">' +
          '<div class="move-name">' + typeBadge(m.type) + " " + esc(m.name) + effBadge(m.effectiveness) + "</div>" +
          '<div class="move-meta">' + esc(m.category) + " | Pow: " + pow + " | Acc: " + acc + " | PP: " + m.pp + "/" + m.max_pp + "</div>" +
          "</button>";
      }
    }

    if (switches.length) {
      html += '<div class="action-section-label">' + (forceSwitch ? "Choose a switch-in" : "Switch") + "</div>";
      for (var j = 0; j < switches.length; j++) {
        var s = switches[j];
        var sTypes = (s.types || []).map(typeBadge).join(" ");
        var hazard = s.hazard_damage_pct > 0 ? " (~" + s.hazard_damage_pct + "% hazard dmg)" : "";
        html +=
          '<button class="action-btn" data-type="switch" data-index="' + s.index + '">' +
          '<div class="move-name">' + esc(s.species) + " " + sTypes + "</div>" +
          '<div class="switch-hp">HP: ' + s.hp_pct + "%" + (s.status ? " [" + esc(s.status) + "]" : "") + hazard + "</div>" +
          "</button>";
      }
    }

    if (!moves.length && !switches.length) {
      html = '<div class="status-banner">No actions available</div>';
    }

    actionsGrid.innerHTML = html;

    // Bind click handlers
    var btns = actionsGrid.querySelectorAll(".action-btn");
    for (var k = 0; k < btns.length; k++) {
      btns[k].addEventListener("click", onActionClick);
    }
  }

  function onActionClick(e) {
    var btn = e.currentTarget;
    // Deselect all
    var all = actionsGrid.querySelectorAll(".action-btn");
    for (var i = 0; i < all.length; i++) all[i].classList.remove("selected");
    // Select this one
    btn.classList.add("selected");
    selectedAction = {
      action_type: btn.getAttribute("data-type"),
      index: parseInt(btn.getAttribute("data-index"), 10)
    };
    submitBtn.disabled = false;
  }

  // --- Submit action ---
  submitBtn.addEventListener("click", function () {
    if (!selectedAction || matchFinished) return;
    submitBtn.disabled = true;
    disableActions();

    fetch(cfg.actionUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(selectedAction)
    }).then(function (res) {
      if (!res.ok) {
        return res.json().then(function (d) { throw new Error(d.detail || "Submit failed"); });
      }
      showWaiting("Waiting for opponent...");
    }).catch(function (err) {
      showBannerError("Error: " + err.message);
      // Re-enable so they can retry
      submitBtn.disabled = false;
      enableActions();
    });
  });

  function disableActions() {
    var btns = actionsGrid.querySelectorAll(".action-btn");
    for (var i = 0; i < btns.length; i++) btns[i].disabled = true;
    stopTimer();
  }

  function enableActions() {
    var btns = actionsGrid.querySelectorAll(".action-btn");
    for (var i = 0; i < btns.length; i++) btns[i].disabled = false;
  }

  function showWaiting(msg) {
    actionsPanel.style.display = "none";
    footerBar.style.display = "none";
    statusBanner.textContent = msg;
    statusBanner.className = "panel status-banner waiting";
    statusBanner.style.display = "";
  }

  function showBannerError(msg) {
    statusBanner.textContent = msg;
    statusBanner.className = "panel status-banner";
    statusBanner.style.display = "";
  }

  // --- Timer ---
  function startTimer() {
    stopTimer();
    turnDeadline = Date.now() + cfg.turnTimeout * 1000;
    timerInterval = setInterval(updateTimer, 250);
    updateTimer();
  }

  function stopTimer() {
    if (timerInterval) clearInterval(timerInterval);
    timerInterval = null;
  }

  function updateTimer() {
    if (!turnDeadline) return;
    var remaining = Math.max(0, (turnDeadline - Date.now()) / 1000);
    timerEl.textContent = formatTimer(remaining);
    timerEl.className = remaining < 30 ? "timer urgent" : "timer";
    if (remaining <= 0) {
      stopTimer();
      showWaiting("Time's up! Random move selected...");
    }
  }

  // --- Match result ---
  function showResult(state) {
    matchFinished = true;
    stopTimer();
    actionsPanel.style.display = "none";
    footerBar.style.display = "none";
    statusBanner.style.display = "none";

    // Determine result from the state — the state includes finished flag
    // but not who won. We'll show a generic message; the manager has the details.
    resultTitle.textContent = "Battle Complete";
    resultTitle.className = "result-title";
    resultDetail.innerHTML = '<a href="/manager" class="result-link">View results in Manager</a>';
    resultOverlay.classList.add("visible");
  }

  // --- SSE: Battle state stream ---
  function connectSSE() {
    var es = new EventSource(cfg.stateStreamUrl);
    es.onmessage = function (e) {
      try {
        var state = JSON.parse(e.data);
        if (state.finished) {
          showResult(state);
          es.close();
          return;
        }
        renderState(state);
      } catch (err) {
        console.error("SSE parse error:", err);
      }
    };
    es.addEventListener("match_end", function () {
      showResult({});
      es.close();
    });
    es.onerror = function () {
      if (matchFinished) { es.close(); return; }
      // EventSource auto-reconnects
    };
  }

  // --- WebSocket: AI thoughts + callouts ---
  function connectThoughtsWS() {
    var ws;
    try { ws = new WebSocket(cfg.thoughtsWsUrl); } catch (_) { return; }
    ws.onmessage = function (e) {
      try {
        var msg = JSON.parse(e.data);
        if (msg.type === "thought") {
          // Only show callouts + reasoning from the AI side (ignore human's).
          var isFromAi = !aiUsername || msg.player === aiUsername;
          if (isFromAi) {
            if (msg.callout) {
              showCallout(msg.callout);
              if (aiSide) postCalloutToFrame(aiSide, msg.callout);
            }
            if (msg.reasoning) addReasoning(msg.turn, msg.reasoning);
          }
        }
        if (msg.type === "history" && msg.players) {
          Object.keys(msg.players).forEach(function (pname) {
            if (aiUsername && pname !== aiUsername) return;
            var items = msg.players[pname] || [];
            items.forEach(function (item) {
              if (item.reasoning) addReasoning(item.turn, item.reasoning);
              if (item.callout) showCallout(item.callout);
            });
          });
        }
      } catch (_) {}
    };
    ws.onclose = function () {
      if (!matchFinished) setTimeout(connectThoughtsWS, 3000);
    };
  }

  function showCallout(text) {
    if (!text || !text.trim()) return;
    calloutBubble.textContent = '"' + text.trim() + '"';
    calloutBubble.classList.add("visible");
  }

  function addReasoning(turn, text) {
    if (!text || !text.trim()) return;
    reasoningItems.push({ turn: turn, text: text.trim() });
    // Keep last 5
    if (reasoningItems.length > 5) reasoningItems = reasoningItems.slice(-5);
    renderReasoning();
  }

  function renderReasoning() {
    var html = "";
    for (var i = 0; i < reasoningItems.length; i++) {
      var r = reasoningItems[i];
      html += '<div class="reasoning-item"><span class="turn-label">T' + (r.turn || "?") + "</span>" + esc(r.text) + "</div>";
    }
    reasoningPanel.innerHTML = html;
    reasoningPanel.scrollTop = reasoningPanel.scrollHeight;
  }

  // --- Battle iframe + trainer sprites + callouts ---
  var battleIframeWrap = document.getElementById("battle-iframe-wrap");
  var battleIframe = document.getElementById("battle-iframe");
  var loadedBattleTag = null;
  var iframeReady = false;
  // Opponent / player info derived from /current_battle and state updates.
  var aiSide = null;          // "p1" or "p2"
  var humanSide = null;       // "p1" or "p2"
  var aiUsername = null;      // Showdown username of AI
  var humanUsername = null;   // Showdown username of human
  var spriteUrls = { p1: "", p2: "" };
  var spritePostSeq = 0;
  var calloutPostSeq = 0;

  function normalizeBattleTag(tag) {
    if (!tag) return null;
    var s = String(tag).trim().replace(/^>+/, "").replace(/^\/+/, "");
    if (!s) return null;
    return s.startsWith("battle-") ? s : "battle-" + s;
  }

  function loadBattleIframe(battleTag) {
    if (!battleTag || !battleIframe || !cfg.showdownUrl) return;
    var norm = normalizeBattleTag(battleTag);
    if (!norm || norm === loadedBattleTag) return;
    loadedBattleTag = norm;
    var base = cfg.showdownUrl.replace(/#.*$/, "");
    battleIframe.src = base + "#" + norm;
    battleIframeWrap.style.display = "";
  }

  // Re-send sprites whenever the iframe reloads.  The Showdown client's
  // message listener may not register immediately at `load`, so we also
  // retry after a short delay and again a couple seconds in.
  if (battleIframe) {
    battleIframe.addEventListener("load", function () {
      iframeReady = true;
      postTrainerSpritesToFrame();
      setTimeout(postTrainerSpritesToFrame, 500);
      setTimeout(postTrainerSpritesToFrame, 2000);
    });
  }

  function postTrainerSpritesToFrame() {
    try {
      if (!battleIframe || !battleIframe.contentWindow) return;
      spritePostSeq += 1;
      battleIframe.contentWindow.postMessage(
        {
          type: "llm_trainer_sprites",
          player1_sprite_url: spriteUrls.p1 || "",
          player2_sprite_url: spriteUrls.p2 || "",
          version: spritePostSeq,
        },
        "*"
      );
    } catch (_) {}
  }

  function postCalloutToFrame(side, text) {
    // Showdown's pill handler (showdown/static/index.html) auto-hides the pill
    // when the trainer anchor isn't visible (e.g., the user is on the chat
    // tab), so it's safe to fire this on every callout.
    if (!text || (side !== "p1" && side !== "p2")) return;
    try {
      if (!battleIframe || !battleIframe.contentWindow) return;
      calloutPostSeq += 1;
      battleIframe.contentWindow.postMessage(
        {
          type: "llm_callouts",
          p1_callout: side === "p1" ? text : "",
          p2_callout: side === "p2" ? text : "",
          version: calloutPostSeq,
        },
        "*"
      );
    } catch (_) {}
  }

  // --- Load opponent persona info + battle tag from /current_battle ---
  function loadOpponentInfo() {
    fetch("/current_battle").then(function (res) {
      if (!res.ok) return;
      return res.json();
    }).then(function (data) {
      if (!data) return;
      // Determine which side is AI vs human
      aiSide = data.player1_type === "human" ? "p2" : "p1";
      humanSide = aiSide === "p1" ? "p2" : "p1";
      var aiIdx = aiSide === "p1" ? "1" : "2";
      var humanIdx = humanSide === "p1" ? "1" : "2";
      aiUsername = data["player" + aiIdx + "_name"];
      humanUsername = data["player" + humanIdx + "_name"];

      var slug = data["player" + aiIdx + "_persona_slug"];
      var name = data["player" + aiIdx + "_name"];
      var aiSprite = data["player" + aiIdx + "_sprite_url"] || "";

      if (name) opponentName.textContent = name;
      // Helper: load an image with a probe, only replace the visible src
      // once it's known good; shimmer stays up until a real image resolves.
      // Falls back to subsequent candidates on load failure.
      function loadFirstSuccessful(urls) {
        var i = 0;
        function tryNext() {
          if (i >= urls.length) return;
          var url = urls[i++];
          if (!url) return tryNext();
          var probe = new Image();
          probe.onload = function () {
            opponentPortrait.src = url;
            opponentPortrait.alt = name || "AI Opponent";
            opponentPortrait.classList.remove("is-loading");
            opponentPortrait.classList.add("is-loaded");
          };
          probe.onerror = tryNext;
          probe.src = url;
        }
        tryNext();
      }
      // Prefer the large square persona portrait; fall back to the tall
      // portrait; last-resort fall back to the small trainer sprite.
      var candidates = [];
      if (slug) {
        candidates.push("/static/portraits/square/" + slug + ".png");
        candidates.push("/static/portraits/" + slug + ".png");
      }
      if (aiSprite) candidates.push(aiSprite);
      loadFirstSuccessful(candidates);

      // Trainer sprites for the battle iframe: AI gets its persona sprite,
      // human side stays blank so the Showdown default renders for them.
      spriteUrls[aiSide] = aiSprite;
      spriteUrls[humanSide] = "";
      // Always post sprites — the iframe will ignore if not ready yet,
      // and the iframe's own `load` handler also re-posts.
      postTrainerSpritesToFrame();

      // Load battle iframe once we have a tag
      if (data.battle_tag) loadBattleIframe(data.battle_tag);
    }).catch(function () {});
  }

  function haveOpponentInfo() {
    // We consider opponent info "ready" once the portrait has successfully
    // loaded — that's the last piece to populate.
    return opponentPortrait.classList.contains("is-loaded");
  }

  // --- Init ---
  loadOpponentInfo();
  connectSSE();
  connectThoughtsWS();

  // Keep polling /current_battle until we have the opponent's portrait +
  // persona info.  Initial load can race against the agent writing
  // current_battle.json — we need to keep asking until it's populated.
  var opponentPoll = setInterval(function () {
    if (matchFinished) { clearInterval(opponentPoll); return; }
    if (haveOpponentInfo() && aiUsername) {
      clearInterval(opponentPoll);
      return;
    }
    loadOpponentInfo();
  }, 1500);
})();
