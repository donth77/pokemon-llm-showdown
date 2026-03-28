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
  if (formatId === "gen8randombattle") return "Gen 8 - Random Battle";
  if (formatId === "gen9randombattle") return "Gen 9 - Random Battle";
  return String(formatId);
}
