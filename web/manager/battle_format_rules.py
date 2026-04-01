"""Showdown battle format helpers shared by manager API and UI."""

# Formats whose teams are assigned by the simulator (e.g. gen9randombattle).
# Custom-team / BYO presets apply to all other format ids.
SHOWDOWN_SERVER_ASSIGNED_TEAM_SUFFIX = "randombattle"


def uses_server_assigned_teams(battle_format: str) -> bool:
    """True if Showdown generates both teams (random battles, random doubles, etc.)."""
    f = (battle_format or "").strip().lower()
    return bool(f) and f.endswith(SHOWDOWN_SERVER_ASSIGNED_TEAM_SUFFIX)


def normalize_battle_format_id(battle_format: str) -> str:
    """Case- and whitespace-insensitive Showdown format id for comparisons."""
    return (battle_format or "").strip().lower()


def team_preset_tag_matches_battle_format(
    team_battle_format: str, matchup_battle_format: str
) -> bool:
    """True when a team row's stored format equals the match/tournament format (normalized)."""
    a = normalize_battle_format_id(team_battle_format)
    b = normalize_battle_format_id(matchup_battle_format)
    return bool(a) and a == b
