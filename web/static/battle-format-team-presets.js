/**
 * Team preset controls: enabled only for formats where players bring teams.
 * Suffix must match web/manager/battle_format_rules.py (injected on match page).
 */
(function () {
  var suf = window.MANAGER_RANDOM_TEAM_BATTLE_FORMAT_SUFFIX || 'randombattle';

  function normalized(fmt) {
    return String(fmt || '').trim().toLowerCase();
  }

  window.managerUsesServerAssignedTeams = function (battleFormat) {
    var f = normalized(battleFormat);
    return f.length > 0 && f.slice(-suf.length) === suf;
  };

  window.managerTeamPresetPickersEnabled = function (battleFormat) {
    var f = normalized(battleFormat);
    return f.length > 0 && !window.managerUsesServerAssignedTeams(battleFormat);
  };

  /** Normalized format id for comparisons (same rules as server). */
  window.managerNormalizeBattleFormat = normalized;

  /**
   * Whether a team row from /api/manager/teams may be chosen for this battle format.
   * For random formats (no BYO) returns true so hidden selects are unconstrained.
   */
  window.managerTeamRowMatchesBattleFormat = function (row, battleFormat) {
    if (!window.managerTeamPresetPickersEnabled(battleFormat)) return true;
    var want = normalized(battleFormat);
    var got = normalized(row && row.battle_format != null ? row.battle_format : '');
    return got.length > 0 && got === want;
  };
})();
