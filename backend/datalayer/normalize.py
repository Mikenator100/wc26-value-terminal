"""Turn raw API responses into the normalised shapes the terminal consumes.

The two outputs that matter:
  - build_player_profile(): a player's club + country per-90 splits and roles
  - team_lambdas():        expected goals (xG) for each side of a fixture

All functions here are pure (no network), so they unit-test cleanly against
mock payloads — see tests/.
"""

from __future__ import annotations

from typing import Optional

# role buckets the terminal's player engine understands
ROLES = ("ST", "SS", "W", "CAM", "CM", "DM", "FB", "CB", "GK")


def _safe(d: dict, *path, default=0):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def per90(count: float, minutes: float) -> float:
    if not minutes:
        return 0.0
    return (count / minutes) * 90.0


def card_probability(yellows: float, minutes: float) -> float:
    """Booking propensity as a per-match probability.

    Treat yellows as Poisson over minutes -> P(at least one booking in 90).
    Keeps it in [0,1] and consistent with how the terminal treats `cards`.
    """
    lam = per90(yellows, minutes)
    import math

    return min(0.9, 1 - math.exp(-lam))


def infer_role(position: Optional[str], grid: Optional[str], stats: dict) -> str:
    """Map API-Football position (G/D/M/F) + lineup grid + output into a role.

    `grid` is "row:col" within the formation; higher col = wider on one flank,
    but row is the more reliable signal for depth. We combine the coarse
    position letter with width (assist share, grid) to separate e.g. wingers
    from central forwards.
    """
    # API-Football uses full words (games.position: "Attacker") and single
    # letters (lineup pos: "F") interchangeably; normalise both.
    raw = (position or "").upper()[:1]
    pos = {"A": "F"}.get(raw, raw)  # "Attacker" -> F
    assists = _safe(stats, "goals", "assists")
    goals = _safe(stats, "goals", "total")
    shots = _safe(stats, "shots", "total")
    minutes = _safe(stats, "games", "minutes") or 1
    apps = _safe(stats, "games", "appearences") or 1

    # crude width signal from the grid column (formations are ~5 wide)
    wide = False
    if grid and ":" in grid:
        try:
            col = int(grid.split(":")[1])
            wide = col in (1, 4, 5)  # flanks
        except ValueError:
            pass

    if pos == "G":
        return "GK"
    if pos == "D":
        return "FB" if wide else "CB"
    if pos == "M":
        if per90(assists, minutes) > 0.25 or wide:
            return "CAM" if not wide else "W"
        # distinguish holding vs box-to-box by shot volume
        return "DM" if per90(shots, minutes) < 0.9 else "CM"
    if pos == "F":
        if wide or per90(assists, minutes) > per90(goals, minutes):
            return "W"
        # high shot + goal share central -> striker, else second striker
        return "ST" if per90(shots, minutes) >= 2.3 else "SS"
    return "CM"  # fallback


def _aggregate(stats_blocks: list[dict]) -> dict:
    """Sum raw counts + minutes across several (team,season) blocks."""
    tot = {"minutes": 0, "shots": 0, "sot": 0, "goals": 0, "assists": 0, "yellow": 0, "apps": 0,
           "tackles": 0, "fouls": 0, "fouled": 0, "passes": 0, "saves": 0}
    for s in stats_blocks:
        tot["minutes"] += _safe(s, "games", "minutes")
        tot["apps"] += _safe(s, "games", "appearences")
        tot["shots"] += _safe(s, "shots", "total")
        tot["sot"] += _safe(s, "shots", "on")
        tot["goals"] += _safe(s, "goals", "total")
        tot["assists"] += _safe(s, "goals", "assists")
        tot["yellow"] += _safe(s, "cards", "yellow")
        tot["tackles"] += _safe(s, "tackles", "total")
        tot["fouls"] += _safe(s, "fouls", "committed")
        tot["fouled"] += _safe(s, "fouls", "drawn")
        tot["passes"] += _safe(s, "passes", "total")
        tot["saves"] += _safe(s, "goals", "saves")
    return tot


def _rates(agg: dict) -> dict:
    m = agg["minutes"]

    # count metrics where a raw total of 0 over real minutes means "not
    # recorded by the source", not a true zero — leave them None so the
    # pricing layer falls back to role baselines
    def opt90(count):
        return round(per90(count, m), 3) if count else None

    return {
        "shots": round(per90(agg["shots"], m), 3),
        "sot": round(per90(agg["sot"], m), 3),
        "goals": round(per90(agg["goals"], m), 3),
        "assists": round(per90(agg["assists"], m), 3),
        "cards": round(card_probability(agg["yellow"], m), 3),
        "tackles": opt90(agg["tackles"]),
        "fouls": opt90(agg["fouls"]),
        "fouled": opt90(agg["fouled"]),
        "passes": opt90(agg["passes"]),
        "saves": opt90(agg["saves"]),
        "minutes": m,  # carried for confidence weighting / display
    }


def split_club_country(stat_blocks: list[dict], national_team_name: str) -> tuple[list, list]:
    """Partition a player's statistics blocks into club vs country.

    A block belongs to 'country' when its team is the national side. Everything
    else (domestic league, continental club competition) is 'club'.
    """
    country, club = [], []
    for b in stat_blocks:
        team_name = _safe(b, "team", "name", default="")
        is_national = bool(_safe(b, "team", "national", default=False)) or (
            team_name.lower() == national_team_name.lower()
        )
        (country if is_national else club).append(b)
    return club, country


def build_player_profile(
    player_name: str,
    national_team_name: str,
    stat_blocks: list[dict],
    predicted_pos: Optional[str] = None,
    predicted_start_prob: float = 0.7,
) -> Optional[dict]:
    """Produce the terminal's player object from raw statistics blocks."""
    club_blocks, country_blocks = split_club_country(stat_blocks, national_team_name)
    if not club_blocks and not country_blocks:
        return None

    club_agg, country_agg = _aggregate(club_blocks), _aggregate(country_blocks)
    club_rates = _rates(club_agg) if club_agg["minutes"] else _rates(country_agg)
    country_rates = _rates(country_agg) if country_agg["minutes"] else _rates(club_agg)

    # role per context, from each context's most-played position block
    def modal_block(blocks):
        return max(blocks, key=lambda b: _safe(b, "games", "minutes")) if blocks else {}

    club_mb, country_mb = modal_block(club_blocks), modal_block(country_blocks)
    club_role = infer_role(_safe(club_mb, "games", "position"), _safe(club_mb, "games", "grid"), club_mb)
    country_role = infer_role(
        _safe(country_mb, "games", "position"), _safe(country_mb, "games", "grid"), country_mb
    )

    pos = predicted_pos or country_role or club_role
    return {
        "name": player_name,
        "team": national_team_name,
        "clubRole": club_role,
        "countryRole": country_role,
        "predictedPos": pos,
        "confirmedPos": pos,        # overwritten by build when a confirmed XI lands
        "startProb": round(predicted_start_prob, 2),
        "confirmedIn": False,       # flipped true when seen in confirmed lineup
        "club": {k: club_rates[k] for k in ("shots", "sot", "goals", "assists", "cards",
                                            "tackles", "fouls", "fouled", "passes", "saves")},
        "country": {k: country_rates[k] for k in ("shots", "sot", "goals", "assists", "cards",
                                                  "tackles", "fouls", "fouled", "passes", "saves")},
        "_minutes": {"club": club_agg["minutes"], "country": country_agg["minutes"]},
    }


# --------------------------------------------------------------------------- #
# expected goals for a fixture -> feeds the terminal's Poisson score matrix
# --------------------------------------------------------------------------- #
def _avg_goals(team_stats: dict, side: str) -> float:
    # season stats can be all-zero strings ("0.0") before a team has played —
    # treat non-positive as "no data" and fall back to a neutral average
    try:
        v = float(_safe(team_stats, "goals", side, "average", "total", default=0) or 0)
    except (TypeError, ValueError):
        v = 0.0
    return v if v > 0 else 1.3


def _avg_goals_for(team_stats: dict) -> float:
    return _avg_goals(team_stats, "for")


def _avg_goals_against(team_stats: dict) -> float:
    return _avg_goals(team_stats, "against")


def team_lambdas(
    home_stats: dict,
    away_stats: dict,
    comp_avg_goals: float = 1.35,
    home_adv: float = 1.05,
    predictions: Optional[dict] = None,
) -> tuple[float, float]:
    """Dixon-Coles-style expected goals for each side.

    Prefers the provider's own predicted goals when available, otherwise builds
    from attack/defence strength relative to the competition average.
    """
    if predictions:
        pg_home = _safe(predictions, "predictions", "goals", "home")
        pg_away = _safe(predictions, "predictions", "goals", "away")
        try:
            h = abs(float(str(pg_home).replace("+", "")))
            a = abs(float(str(pg_away).replace("+", "")))
            if h > 0 and a > 0:
                return round(h, 3), round(a, 3)
        except (TypeError, ValueError):
            pass

    atk_home = _avg_goals_for(home_stats) / comp_avg_goals
    atk_away = _avg_goals_for(away_stats) / comp_avg_goals
    def_home = _avg_goals_against(home_stats) / comp_avg_goals
    def_away = _avg_goals_against(away_stats) / comp_avg_goals

    lam_home = comp_avg_goals * atk_home * def_away * home_adv
    lam_away = comp_avg_goals * atk_away * def_home / home_adv
    # keep within sane football bounds
    clamp = lambda x: max(0.2, min(4.0, x))
    return round(clamp(lam_home), 3), round(clamp(lam_away), 3)
