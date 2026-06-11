"""Predicted starting XIs — the pre-kickoff counterpart to API-Football's
confirmed lineups.

`build_match` already accepts a `predicted_lineups` dict of the form
    {team_name: [{"name", "pos", "startProb"}]}
so this module's only job is to produce that dict. Two sources:

  - ManualLineups: you (or any feed) supply formation + starters as JSON. Fully
    reliable — the recommended path.
  - HtmlPredictedLineups: a best-effort scraper seam for a predicted-XI site.
    Predicted-XI pages vary wildly and are ToS-grey, so the selectors are
    configurable and must be adapted to your chosen source; the reusable,
    *tested* core is the formation->roles mapping below.

The formation mapping reuses the same role buckets as the player-prop engine, so
a predicted position flows straight into the role re-scaling.
"""

from __future__ import annotations

import unicodedata
from typing import Optional, Protocol

# common formations -> role per slot (GK first, then defence L->R, mid, attack)
FORMATION_ROLES = {
    "4-3-3": ["GK", "FB", "CB", "CB", "FB", "DM", "CM", "CM", "W", "ST", "W"],
    "4-2-3-1": ["GK", "FB", "CB", "CB", "FB", "DM", "DM", "W", "CAM", "W", "ST"],
    "4-4-2": ["GK", "FB", "CB", "CB", "FB", "W", "CM", "CM", "W", "ST", "ST"],
    "3-5-2": ["GK", "CB", "CB", "CB", "FB", "CM", "CM", "CM", "FB", "ST", "ST"],
    "3-4-3": ["GK", "CB", "CB", "CB", "W", "CM", "CM", "W", "W", "ST", "W"],
    "4-1-4-1": ["GK", "FB", "CB", "CB", "FB", "DM", "W", "CM", "CM", "W", "ST"],
    "5-3-2": ["GK", "FB", "CB", "CB", "CB", "FB", "CM", "CM", "CM", "ST", "ST"],
    "4-5-1": ["GK", "FB", "CB", "CB", "FB", "W", "CM", "CM", "CM", "W", "ST"],
}


def formation_to_roles(formation: str) -> list[str]:
    """Roles for the 11 slots of a formation, falling back to a generic shape."""
    f = formation.strip().replace(" ", "")
    if f in FORMATION_ROLES:
        return FORMATION_ROLES[f]

    # generic fallback: parse the digits and assign by line
    try:
        lines = [int(x) for x in f.split("-")]
    except ValueError:
        return FORMATION_ROLES["4-3-3"]
    roles = ["GK"]
    for li, n in enumerate(lines):
        is_def = li == 0
        is_att = li == len(lines) - 1
        for k in range(n):
            wide = n >= 4 and (k == 0 or k == n - 1)
            if is_def:
                roles.append("FB" if wide and n >= 4 else "CB")
            elif is_att:
                roles.append("W" if wide else "ST")
            else:
                roles.append("W" if wide else "CM")
    # pad/truncate to 11
    return (roles + ["CM"] * 11)[:11]


def build_predicted_xi(
    formation: str,
    starters: list[str],
    start_prob: float = 0.85,
    doubtful: Optional[list[str]] = None,
) -> list[dict]:
    """Pair an ordered starter list with formation roles -> predicted XI rows."""
    roles = formation_to_roles(formation)
    doubtful = set(_norm(d) for d in (doubtful or []))
    xi = []
    for idx, name in enumerate(starters[:11]):
        sp = 0.55 if _norm(name) in doubtful else start_prob
        xi.append({"name": name, "pos": roles[idx] if idx < len(roles) else "CM", "startProb": round(sp, 2)})
    return xi


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c for c in s.lower() if c.isalnum())


class PredictedLineupProvider(Protocol):
    def to_seed_dict(self) -> dict[str, list[dict]]: ...


class ManualLineups:
    """Reliable path: supply predicted XIs as JSON.

    Input shape:
      { "Brazil": {"formation": "4-2-3-1",
                   "starters": ["Alisson","Danilo", ...],   # 11, in formation order
                   "doubtful": ["Neymar"]},
        "Morocco": {...} }
    """

    def __init__(self, data: dict):
        self.data = data

    def to_seed_dict(self) -> dict[str, list[dict]]:
        out = {}
        for team, info in self.data.items():
            out[team] = build_predicted_xi(
                info.get("formation", "4-3-3"),
                info.get("starters", []),
                doubtful=info.get("doubtful", []),
            )
        return out


class HtmlPredictedLineups:
    """Scraper seam. Configure `selectors` for your chosen predicted-XI source.

    Needs `pip install requests beautifulsoup4`. Returns the same seed dict.
    Default selectors are illustrative — inspect your source and adjust. The
    formation->roles core (above) is what's actually tested.
    """

    DEFAULT_SELECTORS = {
        "fixture": ".predicted-lineup",      # one block per team
        "team": ".team-name",
        "formation": ".formation",
        "player": ".player .name",
        "doubtful_flag": "doubtful",          # class signalling a doubtful starter
    }

    def __init__(self, url: str, selectors: Optional[dict] = None, cache=None):
        self.url = url
        self.selectors = {**self.DEFAULT_SELECTORS, **(selectors or {})}
        self.cache = cache

    def to_seed_dict(self) -> dict[str, list[dict]]:
        import requests
        from bs4 import BeautifulSoup

        html = requests.get(self.url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).text
        return self.parse(html)

    def parse(self, html: str) -> dict[str, list[dict]]:
        from bs4 import BeautifulSoup

        sel = self.selectors
        soup = BeautifulSoup(html, "html.parser")
        out: dict[str, list[dict]] = {}
        for block in soup.select(sel["fixture"]):
            team_el = block.select_one(sel["team"])
            form_el = block.select_one(sel["formation"])
            if not team_el or not form_el:
                continue
            team = team_el.get_text(strip=True)
            formation = form_el.get_text(strip=True)
            starters, doubtful = [], []
            for pl in block.select(sel["player"]):
                name = pl.get_text(strip=True)
                starters.append(name)
                cls = " ".join(pl.parent.get("class", []) + pl.get("class", []))
                if sel["doubtful_flag"] in cls:
                    doubtful.append(name)
            if starters:
                out[team] = build_predicted_xi(formation, starters, doubtful=doubtful)
        return out
