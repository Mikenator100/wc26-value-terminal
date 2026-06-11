"""Optional enrichment from FBref / SofaScore / FotMob via the `soccerdata` lib.

This is a SEAM, not a tested path: it needs `pip install soccerdata` and live
network access to those sites, and the exact DataFrame columns differ slightly
between soccerdata versions — so the column lookups below are defensive and may
need a one-line tweak against your installed version. FBref is the most reliable
per-90 source; SofaScore/FotMob are scrapers of internal endpoints (ToS grey,
rate-limited) — cache hard and go slow.

Usage idea: merge these per-90 rates into the `club` block of a player profile
built from API-Football, keeping API-Football's clean club/country *split* and
using FBref for richer, higher-quality per-90 shot numbers.
"""

from __future__ import annotations

from typing import Optional


def _find_col(df, *candidates):
    """Return the first matching column (handles FBref's MultiIndex headers)."""
    cols = list(df.columns)
    for cand in candidates:
        for c in cols:
            label = " ".join(str(x) for x in c) if isinstance(c, tuple) else str(c)
            if cand.lower() in label.lower():
                return c
    return None


def fbref_per90(league: str, season, cache_dir: Optional[str] = None) -> dict:
    """Per-90 rates keyed by player name, from FBref standard + shooting stats.

    `league` uses soccerdata's codes, e.g. 'ENG-Premier League', and for
    internationals the relevant competition code (look it up via
    soccerdata.FBref.available_leagues()).
    """
    import soccerdata as sd  # imported lazily so the core has no hard dep

    fb = sd.FBref(leagues=league, seasons=season, data_dir=cache_dir)
    std = fb.read_player_season_stats(stat_type="standard").reset_index()
    sho = fb.read_player_season_stats(stat_type="shooting").reset_index()

    name_c = _find_col(std, "player")
    min_c = _find_col(std, "Playing Time Min", "Min")
    gls_c = _find_col(std, "Performance Gls", "Gls")
    ast_c = _find_col(std, "Performance Ast", "Ast")
    crd_c = _find_col(std, "Performance CrdY", "CrdY")
    sh_name_c = _find_col(sho, "player")
    sh_c = _find_col(sho, "Standard Sh", "Sh")
    sot_c = _find_col(sho, "Standard SoT", "SoT")

    import math

    shots_by_name = {}
    for _, r in sho.iterrows():
        shots_by_name[r[sh_name_c]] = (r.get(sh_c, 0) or 0, r.get(sot_c, 0) or 0)

    out: dict[str, dict] = {}
    for _, r in std.iterrows():
        name = r[name_c]
        minutes = float(r.get(min_c, 0) or 0)
        if minutes < 180:  # ignore tiny samples
            continue
        sh, sot = shots_by_name.get(name, (0, 0))
        p90 = lambda x: round((float(x) / minutes) * 90.0, 3)
        yellows_p90 = (float(r.get(crd_c, 0) or 0) / minutes) * 90.0
        out[name] = {
            "shots": p90(sh),
            "sot": p90(sot),
            "goals": p90(r.get(gls_c, 0) or 0),
            "assists": p90(r.get(ast_c, 0) or 0),
            "cards": round(min(0.9, 1 - math.exp(-yellows_p90)), 3),
        }
    return out


def merge_into_profiles(profiles: list[dict], fbref_rates: dict, prefer: str = "fbref") -> list[dict]:
    """Overlay FBref per-90 onto each profile's `club` block where names match."""
    for p in profiles:
        rates = fbref_rates.get(p["name"])
        if not rates:
            continue
        if prefer == "fbref":
            p["club"] = {**p["club"], **rates}
        else:  # average the two sources
            p["club"] = {k: round((p["club"].get(k, 0) + rates[k]) / 2, 3) for k in rates}
    return profiles
