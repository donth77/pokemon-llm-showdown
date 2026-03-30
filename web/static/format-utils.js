/** @param {number|null|undefined} sec */
function displayDuration(sec) {
  if (sec == null) return "—";
  var total = Number(sec);
  if (Number.isNaN(total)) return "—";
  var mins = Math.floor(total / 60);
  var s = Math.floor(total % 60);
  return mins + " m " + s + " s";
}

function displayBattleFormat(formatId) {
  if (!formatId) return "--";
  var s = String(formatId).trim();
  var m = /^gen(\d+)randombattle$/i.exec(s);
  if (m) return "[Gen " + m[1] + "] Random Battle";
  m = /^gen(\d+)randomdoublesbattle$/i.exec(s);
  if (m) return "[Gen " + m[1] + "] Random Doubles Battle";
  m = /^gen(\d+)/i.exec(s);
  if (m) {
    var tail = s.slice(m[0].length);
    if (!tail) return "[Gen " + m[1] + "]";
    var label = tail
      .replace(/([a-z])([A-Z])/g, "$1 $2")
      .replace(/(\d+)/g, " $1 ")
      .split(/[^a-z0-9]+/i)
      .filter(Boolean)
      .map(function (w) {
        if (/^\d+$/.test(w)) return w;
        return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
      })
      .join(" ");
    return "[Gen " + m[1] + "]" + (label ? " " + label : "");
  }
  return s;
}

/** @param {Record<string, unknown>} m match row from scoreboard / API */
function formatTournamentStageLabel(m) {
  var bracket = m.series_bracket;
  var rn = m.series_round_number;
  var maxWb = m.tournament_max_winners_round;
  var ttype = String(m.tournament_type || "");
  if (bracket === "grand_finals") {
    return "Grand Finals";
  }
  if (
    bracket === "winners" &&
    rn != null &&
    rn !== "" &&
    maxWb != null &&
    maxWb !== ""
  ) {
    var r = Number(rn);
    var mx = Number(maxWb);
    if (Number.isFinite(r) && Number.isFinite(mx) && mx > 0) {
      if (r === mx) {
        if (ttype === "double_elimination") {
          return "WB Finals";
        }
        return "Finals";
      }
      if (r === mx - 1 && mx >= 3) {
        return ttype === "double_elimination" ? "WB Semifinals" : "Semifinals";
      }
      if (r === mx - 2 && mx >= 4) {
        return ttype === "double_elimination" ? "WB Quarterfinals" : "Quarterfinals";
      }
    }
  }
  return "";
}

/**
 * @param {Record<string, unknown>|null|undefined} m
 * @param {boolean} isDraw
 */
function victorySplashTitleLabel(m, isDraw) {
  if (isDraw) return "Match result";
  if (!m) return "Winner";
  if (m.victory_tournament_clinched) return "Tournament Winner";
  if (m.victory_series_clinched) {
    var tname = m.tournament_name;
    if (tname != null && String(tname).trim()) {
      var stage = formatTournamentStageLabel(m);
      if (stage) return stage + " Winner";
      var bracket = m.series_bracket;
      var rn = m.series_round_number;
      if (rn != null && rn !== "") {
        if (bracket === "losers") return "LB R" + String(rn) + " Winner";
        return "R" + String(rn) + " Winner";
      }
    }
    return "Series Winner";
  }
  return "Winner";
}

/**
 * @param {Record<string, unknown>} m
 * @param {(x: string) => string} [escaper]
 * @param {boolean} [includeMatchPosition] default false; M# is a bracket slot index, often omitted in UI
 */
function buildTournamentContextSegments(m, escaper, includeMatchPosition) {
  var escFn =
    escaper ||
    function (x) {
      return String(x);
    };
  var showMp = includeMatchPosition === true;
  var bracket = m.series_bracket;
  var stage = formatTournamentStageLabel(m);
  var rn = m.series_round_number;
  var mp = m.series_match_position;
  var gn = m.game_number;
  var segs = [];
  if (stage) {
    segs.push(escFn(stage));
  } else if (rn != null && rn !== "") {
    var rlab = escFn(String(rn));
    if (bracket === "losers") {
      segs.push("LB R" + rlab);
    } else {
      segs.push("R" + rlab);
    }
  }
  if (showMp && mp != null && mp !== "") {
    segs.push("M" + escFn(String(mp)));
  }
  if (gn != null) {
    segs.push("G" + escFn(String(gn)));
  }
  return segs;
}

/**
 * Broadcast top bar: battle format only (tournament line is a separate pill).
 * @param {Record<string, unknown>} data
 */
function formatBroadcastFormatLabel(data) {
  if (!data) return "Format: --";
  var fmt = displayBattleFormat(data.battle_format);
  var fmtPart = fmt && fmt !== "--" ? fmt : "--";
  return "Format: " + fmtPart;
}

/**
 * Plain-text line for victory modal / ticker (no HTML).
 * @param {boolean} [includeMatchPosition] passed through to buildTournamentContextSegments
 */
function formatTournamentMatchContextLine(m, includeMatchPosition) {
  if (!m) return "";
  var tname = m.tournament_name;
  if (tname == null || !String(tname).trim()) return "";
  var name = String(tname).trim();
  var segs = buildTournamentContextSegments(
    m,
    function (x) {
      return x;
    },
    includeMatchPosition,
  );
  if (!segs.length) return name;
  return name + " · " + segs.join(" · ");
}

/**
 * @param {Record<string, unknown>} m
 * @param {(s: string) => string} escFn
 * @param {boolean} [includeMatchPosition] passed through to buildTournamentContextSegments
 */
function formatHistoryTournamentContext(m, escFn, includeMatchPosition) {
  var esc = escFn || function (x) {
    return String(x);
  };
  var tname = m.tournament_name;
  if (tname == null || !String(tname).trim()) return "";
  var name = esc(String(tname).trim());
  var segs = buildTournamentContextSegments(m, esc, includeMatchPosition);
  if (!segs.length) return name;
  return name + " - " + segs.join(" · ");
}

function displayTournamentBracketType(type) {
  var s = String(type || "").trim();
  if (s === "round_robin") return "Round robin";
  if (s === "single_elimination") return "Single elimination";
  if (s === "double_elimination") return "Double elimination";
  return s || "—";
}
