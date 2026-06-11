"""Team per-game count rates from finished-fixture statistics.

API-Football's `teams/statistics` endpoint has goals and cards but no corners
or shots, so the real per-game rates come from `fixtures/statistics` on each of
a team's recent finished matches. This module is the pure half: given those
payloads, average them into the `teamRates` block the count-market engine and
the terminal consume:

    {corners, cards, shots, sot, offsides, tackles, fouls, redProb}

All functions are network-free and unit-tested against mock payloads.
"""

from __future__ import annotations

from typing import Optional

# fixtures/statistics `type` label -> our metric key
STAT_KEYS = {
    "Corner Kicks": "corners",
    "Yellow Cards": "cards",
    "Red Cards": "red",
    "Total Shots": "shots",
    "Shots on Goal": "sot",
    "Offsides": "offsides",
    "Fouls": "fouls",
}

# tackles are not in fixtures/statistics; teams tackle roughly in proportion to
# the fouls they commit (~1.4 tackles per foul at team level), bounded to the
# realistic per-game range
TACKLES_PER_FOUL = 1.4
TACKLES_RANGE = (12.0, 21.0)

RED_PROB_RANGE = (0.02, 0.30)

FINISHED = {"FT", "AET", "PEN"}


def form_goal_averages(team_id: int, fixtures: list[dict]) -> Optional[tuple[float, float]]:
    """Per-game (goals for, goals against) over recent finished fixtures.

    Used as the xG fallback early in a tournament, when the league-season
    statistics endpoint has no games to average yet.
    """
    gf = ga = games = 0
    for fx in fixtures or []:
        if ((fx.get("fixture") or {}).get("status") or {}).get("short") not in FINISHED:
            continue
        goals = fx.get("goals") or {}
        gh, gaw = goals.get("home"), goals.get("away")
        if gh is None or gaw is None:
            continue
        if ((fx.get("teams") or {}).get("home") or {}).get("id") == team_id:
            gf, ga = gf + gh, ga + gaw
        else:
            gf, ga = gf + gaw, ga + gh
        games += 1
    if not games:
        return None
    return round(gf / games, 3), round(ga / games, 3)


def _team_stats(payload: list[dict], team_id: int) -> Optional[dict]:
    """Extract one team's {metric: value} from a fixtures/statistics response."""
    for block in payload or []:
        if (block.get("team") or {}).get("id") != team_id:
            continue
        out: dict[str, float] = {}
        for stat in block.get("statistics", []):
            key = STAT_KEYS.get(stat.get("type"))
            if key is not None and stat.get("value") is not None:
                out[key] = float(stat["value"])
        return out or None
    return None


def team_rates(team_id: int, stats_payloads: list[list[dict]]) -> Optional[dict]:
    """Average a team's per-game counts over several fixtures/statistics payloads.

    Returns None when no payload contains the team — callers fall back to
    omitting teamRates (the terminal then skips count markets for the match).
    """
    sums: dict[str, float] = {}
    games = 0
    for payload in stats_payloads:
        stats = _team_stats(payload, team_id)
        if not stats:
            continue
        games += 1
        for k, v in stats.items():
            sums[k] = sums.get(k, 0.0) + v
    if not games:
        return None

    avg = lambda k, default: round(sums.get(k, default * games) / games, 2)
    fouls = avg("fouls", 12.0)
    tackles = round(min(TACKLES_RANGE[1], max(TACKLES_RANGE[0], fouls * TACKLES_PER_FOUL)), 2)
    red_prob = round(min(RED_PROB_RANGE[1], max(RED_PROB_RANGE[0], sums.get("red", 0.0) / games)), 3)
    return {
        "corners": avg("corners", 5.0),
        "cards": avg("cards", 1.8),
        "shots": avg("shots", 12.0),
        "sot": avg("sot", 4.3),
        "offsides": avg("offsides", 1.8),
        "tackles": tackles,
        "fouls": fouls,
        "redProb": red_prob,
        "_games": games,  # sample size, for display/diagnostics
    }
