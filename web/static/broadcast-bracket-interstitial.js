/**
 * Bracket interstitial: round-robin standings, elimination mini-bracket, next-match line.
 * Load after format-utils.js (uses formatTournamentMatchContextLine).
 */
(function () {
  "use strict";

  function entryDisplayName(e) {
    if (!e) return "—";
    var dn = String(e.display_name || "").trim();
    if (dn) return dn;
    var ds = String(e.persona_display_slug || e.persona_slug || "").trim();
    return ds || "—";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatQueueRowLocal(r, omitIfNecessary) {
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
    if (r.queue_game_if_necessary && !omitIfNecessary) {
      line += " (if necessary)";
    }
    return line;
  }

  function roundRobinStandings(entries, series) {
    var st = {};
    (entries || []).forEach(function (e) {
      st[String(e.id)] = {
        id: e.id,
        name: entryDisplayName(e),
        wins: 0,
        losses: 0,
        played: 0,
      };
    });
    (series || []).forEach(function (s) {
      if (s.status !== "completed" || !s.winner_side) return;
      var p1 = s.player1_entry_id != null ? String(s.player1_entry_id) : "";
      var p2 = s.player2_entry_id != null ? String(s.player2_entry_id) : "";
      if (s.winner_side === "p1" && p1 && st[p1]) {
        st[p1].wins += 1;
        st[p1].played += 1;
        if (p2 && st[p2]) {
          st[p2].losses += 1;
          st[p2].played += 1;
        }
      } else if (s.winner_side === "p2" && p2 && st[p2]) {
        st[p2].wins += 1;
        st[p2].played += 1;
        if (p1 && st[p1]) {
          st[p1].losses += 1;
          st[p1].played += 1;
        }
      }
    });
    var rows = Object.keys(st).map(function (k) {
      return st[k];
    });
    rows.sort(function (a, b) {
      if (b.wins !== a.wins) return b.wins - a.wins;
      if (a.losses !== b.losses) return a.losses - b.losses;
      return String(a.name).localeCompare(String(b.name));
    });
    return rows;
  }

  function renderStandingsTable(container, rows) {
    var wrap = document.createElement("div");
    wrap.className = "bi-standings-wrap";
    var tbl = document.createElement("table");
    tbl.className = "bi-standings";
    var thead = document.createElement("thead");
    thead.innerHTML =
      "<tr><th>Player</th><th>W</th><th>L</th><th>Series</th></tr>";
    tbl.appendChild(thead);
    var tbody = document.createElement("tbody");
    rows.forEach(function (r) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td class='bi-std-name'>" +
        escapeHtml(r.name) +
        "</td><td class='bi-std-w'>" +
        r.wins +
        "</td><td>" +
        r.losses +
        "</td><td class='bi-std-muted'>" +
        r.played +
        "</td>";
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    wrap.appendChild(tbl);
    container.appendChild(wrap);
  }

  function wbFromEndLabel(wbFromEnd, ttype) {
    if (ttype === "double_elimination") {
      if (wbFromEnd === 1) return "WB Finals";
      if (wbFromEnd === 2) return "WB Semifinals";
      if (wbFromEnd === 3) return "WB Quarterfinals";
      if (wbFromEnd === 4) return "R16";
      if (wbFromEnd === 5) return "R32";
      if (wbFromEnd === 6) return "R64";
      if (wbFromEnd >= 7) return "R128";
      return "WB";
    }
    if (wbFromEnd === 1) return "Final";
    if (wbFromEnd === 2) return "Semis";
    if (wbFromEnd === 3) return "Quarters";
    if (wbFromEnd === 4) return "R16";
    if (wbFromEnd === 5) return "R32";
    if (wbFromEnd === 6) return "R64";
    if (wbFromEnd >= 7) return "R128";
    return "R" + wbFromEnd;
  }

  function filterSeriesForSection(series, leg) {
    if (leg === "winners") {
      return (series || []).filter(function (s) {
        return s.bracket === "winners" || s.bracket == null;
      });
    }
    if (leg === "losers") {
      return (series || []).filter(function (s) {
        return s.bracket === "losers";
      });
    }
    if (leg === "grand") {
      return (series || []).filter(function (s) {
        return (
          s.bracket === "grand_finals" || s.bracket === "grand_finals_reset"
        );
      });
    }
    return [];
  }

  function renderMatchCard(s, highlightSeriesId) {
    var sid = s.id != null ? Number(s.id) : NaN;
    var hiId = highlightSeriesId != null ? Number(highlightSeriesId) : NaN;
    var hi = Number.isFinite(sid) && Number.isFinite(hiId) && sid === hiId;
    var wrap = document.createElement("div");
    wrap.className = "bi-match" + (hi ? " bi-match--next" : "");
    if (s.status === "completed") wrap.classList.add("bi-match--done");
    else if (s.status === "in_progress") wrap.classList.add("bi-match--live");

    // Showdown-style names only (server sets player*_battle_display); avoid persona_display slugs.
    var p1n = String(s.player1_battle_display || "").trim() || "TBD";
    var p2n = String(s.player2_battle_display || "").trim() || "TBD";

    function row(name, wins, isWinner) {
      var d = document.createElement("div");
      d.className = "bi-player";
      if (isWinner) d.classList.add("bi-player--winner");
      var span = document.createElement("span");
      span.className = "bi-player-name";
      span.textContent = name;
      var sc = document.createElement("span");
      sc.className = "bi-score";
      sc.textContent = String(wins != null ? wins : 0);
      d.appendChild(span);
      d.appendChild(sc);
      return d;
    }

    var w1 = s.winner_side === "p1" && s.status === "completed";
    var w2 = s.winner_side === "p2" && s.status === "completed";
    wrap.appendChild(row(p1n, s.player1_wins, w1));
    wrap.appendChild(row(p2n, s.player2_wins, w2));
    return wrap;
  }

  function renderElimBracket(container, series, leg, highlightSeriesId, ttype) {
    var filtered = filterSeriesForSection(series, leg);
    if (!filtered.length) {
      container.textContent = "";
      return;
    }
    var maxR = 0;
    filtered.forEach(function (s) {
      var r = Number(s.round_number);
      if (Number.isFinite(r) && r > maxR) maxR = r;
    });

    var root = document.createElement("div");
    root.className = "bi-bracket";

    if (leg === "grand") {
      var sortedGf = filtered.slice().sort(function (a, b) {
        var oa = a.bracket === "grand_finals" ? 0 : 1;
        var ob = b.bracket === "grand_finals" ? 0 : 1;
        if (oa !== ob) return oa - ob;
        return (a.id || 0) - (b.id || 0);
      });
      var col = document.createElement("div");
      col.className = "bi-bracket-col";
      var matchesWrap = document.createElement("div");
      matchesWrap.className = "bi-bracket-matches";
      sortedGf.forEach(function (s) {
        matchesWrap.appendChild(renderMatchCard(s, highlightSeriesId));
      });
      col.appendChild(matchesWrap);
      root.appendChild(col);
    } else {
      for (var rnd = 1; rnd <= maxR; rnd++) {
        var inRound = filtered.filter(function (s) {
          return Number(s.round_number) === rnd;
        });
        if (!inRound.length) continue;
        var col2 = document.createElement("div");
        col2.className = "bi-bracket-col";
        var lbl = document.createElement("div");
        lbl.className = "bi-bracket-col-label";
        if (leg === "winners") {
          var wbfe = maxR - rnd + 1;
          lbl.textContent = "R" + rnd + " · " + wbFromEndLabel(wbfe, ttype);
        } else {
          lbl.textContent = "LB R" + rnd;
        }
        col2.appendChild(lbl);
        var mw = document.createElement("div");
        mw.className = "bi-bracket-matches";
        inRound
          .slice()
          .sort(function (a, b) {
            return (a.match_position || 0) - (b.match_position || 0);
          })
          .forEach(function (s) {
            mw.appendChild(renderMatchCard(s, highlightSeriesId));
          });
        col2.appendChild(mw);
        root.appendChild(col2);
      }
    }

    container.innerHTML = "";
    container.appendChild(root);
  }

  function resolveDoubleElimLeg(nextSeries) {
    if (!nextSeries) return "winners";
    var b = nextSeries.bracket;
    if (b === "losers") return "losers";
    if (b === "grand_finals" || b === "grand_finals_reset") return "grand";
    return "winners";
  }

  function renderBracketInterstitialVisual(container, tournament, options) {
    options = options || {};
    var highlightSeriesId = options.highlightSeriesId;
    var nextSeries = options.nextSeries;
    var series = tournament.series || [];
    var entries = tournament.entries || [];
    var ttype = String(tournament.type || "");

    container.innerHTML = "";

    if (ttype === "round_robin") {
      var srows = roundRobinStandings(entries, series);
      if (!srows.length) return;
      renderStandingsTable(container, srows);
      return;
    }
    if (ttype === "single_elimination") {
      renderElimBracket(
        container,
        series,
        "winners",
        highlightSeriesId,
        "single_elimination",
      );
      return;
    }
    if (ttype === "double_elimination") {
      var leg = resolveDoubleElimLeg(nextSeries);
      var title = document.createElement("div");
      title.className = "bi-bracket-section-title";
      title.textContent =
        leg === "winners"
          ? "Winners bracket"
          : leg === "losers"
            ? "Losers bracket"
            : "Grand finals";
      container.appendChild(title);
      var inner = document.createElement("div");
      inner.className = "bi-bracket-inner";
      renderElimBracket(
        inner,
        series,
        leg,
        highlightSeriesId,
        "double_elimination",
      );
      container.appendChild(inner);
    }
  }

  function queueRowToContextShape(row) {
    if (!row || typeof row !== "object") return {};
    return {
      tournament_name: row.tournament_name,
      series_bracket: row.series_bracket,
      series_round_number: row.series_round_number,
      series_match_position: row.series_match_position,
      tournament_max_winners_round: row.tournament_max_winners_round,
      tournament_type: row.tournament_type,
      game_number: row.game_number,
    };
  }

  function formatBracketInterstitialNextLine(row) {
    var matchup = formatQueueRowLocal(row, true);
    var ctx =
      typeof formatTournamentMatchContextLine === "function"
        ? formatTournamentMatchContextLine(queueRowToContextShape(row), false)
        : "";
    if (!matchup && !ctx) return "";
    if (!matchup) return ctx;
    if (!ctx) return matchup;
    return matchup + " · " + ctx;
  }

  window.renderBracketInterstitialVisual = renderBracketInterstitialVisual;
  window.formatBracketInterstitialNextLine = formatBracketInterstitialNextLine;
})();
