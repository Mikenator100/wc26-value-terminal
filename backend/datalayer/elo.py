"""Opponent-quality weighting for form windows.

The xG fallback and team rates average over recent finished fixtures, which at
a World Cup are friendlies/qualifiers against wildly uneven opposition —
South Korea's 3-0 over El Salvador says much less than 3-0 over France would.
This module turns an opponent's Elo into a bounded multiplier so goals scored
against weak sides are discounted and goals conceded to them count worse.

The baked table is a *snapshot* (June 2026, eloratings.net-style scale,
hand-rounded). It does not need to be precise — the factor is bounded and only
tilts form averages — but you can override or extend it with a JSON file of
{team name: rating} via the ELO_JSON env var.
"""

from __future__ import annotations

import json
import os
import unicodedata
from typing import Optional

# reference rating: a mid-pack World Cup side. factor=1 against these.
REF = 1800.0
# rating points per doubling-ish of the multiplier (bounded below anyway)
SCALE = 700.0
FACTOR_RANGE = (0.65, 1.45)

# June 2026 snapshot, approximate — override via ELO_JSON
BASELINE: dict[str, float] = {
    "argentina": 2120, "spain": 2110, "france": 2060, "england": 2050,
    "brazil": 2000, "portugal": 2010, "netherlands": 1990, "germany": 1945,
    "colombia": 1935, "uruguay": 1930, "italy": 1915, "belgium": 1900,
    "croatia": 1880, "morocco": 1920, "japan": 1885, "mexico": 1810,
    "usa": 1790, "switzerland": 1840, "denmark": 1850, "ecuador": 1880,
    "senegal": 1820, "iran": 1760, "south korea": 1770, "korea republic": 1770,
    "australia": 1740, "austria": 1830, "ukraine": 1780, "turkey": 1830,
    "turkiye": 1830,  # API-Football's spelling, post accent-strip
    "sweden": 1760, "poland": 1770, "serbia": 1770, "wales": 1720,
    "scotland": 1750, "norway": 1850, "czech republic": 1750, "czechia": 1750,
    "hungary": 1730, "greece": 1790, "russia": 1760, "slovakia": 1720,
    "slovenia": 1720, "romania": 1700, "algeria": 1750, "egypt": 1740,
    "nigeria": 1720, "ivory coast": 1760, "cameroon": 1700, "ghana": 1650,
    "tunisia": 1700, "south africa": 1680, "mali": 1700, "burkina faso": 1640,
    "canada": 1730, "panama": 1680, "costa rica": 1620, "jamaica": 1600,
    "honduras": 1570, "el salvador": 1450, "trinidad and tobago": 1460,
    "guatemala": 1540, "curacao": 1580, "haiti": 1560, "new zealand": 1590,
    "bosnia and herzegovina": 1660, "bosnia & herzegovina": 1660,
    "cote d'ivoire": 1760, "republic of ireland": 1680, "ireland": 1680,
    "north macedonia": 1640, "albania": 1640, "georgia": 1660,
    "korea dpr": 1450, "north korea": 1450,
    "saudi arabia": 1640, "qatar": 1630, "iraq": 1620, "uae": 1610,
    "jordan": 1630, "uzbekistan": 1640, "china": 1540, "india": 1380,
    "paraguay": 1780, "peru": 1740, "chile": 1730, "venezuela": 1720,
    "bolivia": 1640, "cape verde": 1620, "dr congo": 1660, "kenya": 1500,
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def load_table() -> dict[str, float]:
    """Baseline snapshot + manual overrides (ELO_JSON) + in-tournament state
    (ELO_STATE, written by the jobs loop after each cycle's results)."""
    table = dict(BASELINE)
    for env in ("ELO_JSON", "ELO_STATE"):
        path = os.environ.get(env)
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    table.update({_norm(k): float(v) for k, v in json.load(f).items()})
            except Exception as e:
                print(f"{env} ignored: {e}")
    return table


def rating(team_name: str, table: Optional[dict[str, float]] = None) -> Optional[float]:
    return (table if table is not None else load_table()).get(_norm(team_name))


# tournament K-factor (eloratings.net uses 50-60 for World Cup matches) and
# the standard margin multiplier
K_FACTOR = 50.0


def _margin_mult(diff: int) -> float:
    if diff <= 1:
        return 1.0
    if diff == 2:
        return 1.5
    return (11 + diff) / 8


def apply_results(table: dict[str, float], fixtures: list[dict]) -> dict[str, float]:
    """Update a ratings table from finished fixtures (chronological order).

    Standard Elo: expected score from the rating gap, K scaled by the margin
    of victory. Returns a NEW table; unknown teams are skipped (a rating
    invented from one match would be noise)."""
    out = dict(table)
    finished = [f for f in fixtures
                if ((f.get("fixture") or {}).get("status") or {}).get("short") in ("FT", "AET", "PEN")]
    finished.sort(key=lambda f: (f.get("fixture") or {}).get("date") or "")
    for f in finished:
        teams, goals = f.get("teams") or {}, f.get("goals") or {}
        hn = (teams.get("home") or {}).get("name", "")
        an = (teams.get("away") or {}).get("name", "")
        gh, ga = goals.get("home"), goals.get("away")
        if gh is None or ga is None:
            continue
        kh, ka = _norm(hn), _norm(an)
        if kh not in out or ka not in out:
            continue
        rh, ra = out[kh], out[ka]
        exp_home = 1 / (1 + 10 ** ((ra - rh) / 400))
        score = 1.0 if gh > ga else 0.0 if gh < ga else 0.5
        delta = K_FACTOR * _margin_mult(abs(gh - ga)) * (score - exp_home)
        out[kh] = round(rh + delta, 1)
        out[ka] = round(ra - delta, 1)
    return out


def goal_factor(opp_rating: Optional[float]) -> float:
    """Multiplier for goals scored against this opponent (and divisor for
    goals conceded to them). Unknown opponent -> neutral 1.0."""
    if opp_rating is None:
        return 1.0
    f = 10 ** ((opp_rating - REF) / SCALE)
    return max(FACTOR_RANGE[0], min(FACTOR_RANGE[1], f))


# the matchup anchor: WC matches average ~2.6 goals; ~220 Elo points of
# rating gap is worth about one goal of expected superiority
TOTAL_GOALS = 2.6
ELO_PER_GOAL = 220.0


def elo_lambdas(r_home: float, r_away: float, venue_mult: float = 1.0,
                total: float = TOTAL_GOALS) -> tuple[float, float]:
    """Expected goals for each side straight from the Elo matchup.

    Small-sample form averages are unreliable strength estimates across
    uneven schedules (qualifier blowouts vs elite friendlies); the rating
    difference is the steadier anchor — form should only tilt it.
    """
    d = max(-2.2, min(2.2, (r_home - r_away) / ELO_PER_GOAL))
    lam_home = max(0.25, (total + d) / 2) * venue_mult
    lam_away = max(0.25, (total - d) / 2) / venue_mult
    return round(lam_home, 3), round(lam_away, 3)
