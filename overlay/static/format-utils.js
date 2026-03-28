function displayBattleFormat(formatId) {
  if (!formatId) return "--";
  if (formatId === "gen8randombattle") return "Gen 8 - Random Battle";
  if (formatId === "gen9randombattle") return "Gen 9 - Random Battle";
  return String(formatId);
}
