"""Live odds: fetch Bet365 (retail) + Pinnacle (sharp) and shape them for the
terminal's `markets` field.

Uses The Odds API v4. One fetch returns every bookmaker for every fixture, so we
get the price you'd bet (Bet365) next to the sharp benchmark (Pinnacle, or
Betfair Exchange as a fallback) that the no-vig fair line is built from.

The OddsProvider protocol keeps this swappable for OddsPapi / TheStatsAPI later.
"""

from __future__ import annotations

import unicodedata
from typing import Optional, Protocol

import requests

from .providers import FileCache

SPORT_KEY = "soccer_fifa_world_cup"
# priority order for the "sharp" reference used to build the fair line
SHARP_PRIORITY = ["pinnacle", "betfair_ex_eu", "betfair_ex_uk"]
RETAIL = "bet365"


class OddsProvider(Protocol):
    def events_odds(self, markets: str, bookmakers: str) -> list[dict]: ...


class TheOddsApiProvider:
    BASE = "https://api.the-odds-api.com/v4"

    def __init__(
        self,
        api_key: str,
        regions: str = "eu,uk",
        cache: Optional[FileCache] = None,
    ):
        self.api_key = api_key
        self.regions = regions
        self.cache = cache or FileCache()
        self.requests_remaining: Optional[str] = None

    # NB: the bulk /odds endpoint rejects btts (422 INVALID_MARKET) — btts is
    # only served by the per-event endpoint at 1 call per fixture. The shaping
    # code below still handles btts whenever a payload carries it.
    def events_odds(self, markets: str = "h2h,totals", bookmakers: str = "bet365,pinnacle,betfair_ex_eu") -> list[dict]:
        params = {
            "apiKey": self.api_key,
            "regions": self.regions,
            "markets": markets,
            "bookmakers": bookmakers,
            "oddsFormat": "decimal",
        }
        # odds move; keep the cache short so we pick up line changes
        cached = self.cache.get("odds", {k: v for k, v in params.items() if k != "apiKey"}, ttl=120)
        if cached is not None:
            return cached
        resp = requests.get(f"{self.BASE}/sports/{SPORT_KEY}/odds", params=params, timeout=20)
        resp.raise_for_status()
        self.requests_remaining = resp.headers.get("x-requests-remaining")
        data = resp.json()
        self.cache.put("odds", {k: v for k, v in params.items() if k != "apiKey"}, data)
        return data


# --------------------------------------------------------------------------- #
# normalisation: one event -> terminal markets [{key,name,outcomes:[{label,bet365,pinnacle}]}]
# --------------------------------------------------------------------------- #
def _book_market(event: dict, book_key: str, market_key: str) -> Optional[dict]:
    for b in event.get("bookmakers", []):
        if b.get("key") != book_key:
            continue
        for m in b.get("markets", []):
            if m.get("key") == market_key:
                return m
    return None


def _sharp_market(event: dict, market_key: str) -> Optional[dict]:
    for book in SHARP_PRIORITY:
        m = _book_market(event, book, market_key)
        if m:
            return m
    return None


def _price(market: Optional[dict], pred) -> Optional[float]:
    if not market:
        return None
    for o in market.get("outcomes", []):
        if pred(o):
            return o.get("price")
    return None


def _outcome(label: str, retail: Optional[float], sharp: Optional[float]) -> Optional[dict]:
    # need both a price to bet and a sharp reference; if sharp missing, fall
    # back to retail so the row still renders (fair line degrades to retail no-vig)
    if retail is None and sharp is None:
        return None
    return {"label": label, "bet365": retail or sharp, "pinnacle": sharp or retail}


def normalize_event_markets(event: dict) -> list[dict]:
    home, away = event.get("home_team"), event.get("away_team")
    markets: list[dict] = []

    # --- 1X2 (h2h) -> ordered [home, draw, away] ----------------------- #
    rb, sb = _book_market(event, RETAIL, "h2h"), _sharp_market(event, "h2h")
    h = _outcome(home, _price(rb, lambda o: o["name"] == home), _price(sb, lambda o: o["name"] == home))
    d = _outcome("Draw", _price(rb, lambda o: o["name"] == "Draw"), _price(sb, lambda o: o["name"] == "Draw"))
    a = _outcome(away, _price(rb, lambda o: o["name"] == away), _price(sb, lambda o: o["name"] == away))
    if h and d and a:
        markets.append({"key": "1x2", "name": "Match result", "outcomes": [h, d, a]})

    # --- totals -> pick the line closest to 2.5, ordered [Over, Under] -- #
    rb, sb = _book_market(event, RETAIL, "totals"), _sharp_market(event, "totals")
    pts = sorted({o.get("point") for o in (sb or rb or {}).get("outcomes", []) if o.get("point") is not None},
                 key=lambda p: abs(p - 2.5))
    if pts:
        line = pts[0]
        over = _outcome(
            f"Over {line}",
            _price(rb, lambda o: o["name"] == "Over" and o.get("point") == line),
            _price(sb, lambda o: o["name"] == "Over" and o.get("point") == line),
        )
        under = _outcome(
            f"Under {line}",
            _price(rb, lambda o: o["name"] == "Under" and o.get("point") == line),
            _price(sb, lambda o: o["name"] == "Under" and o.get("point") == line),
        )
        if over and under:
            markets.append({"key": "ou25", "name": f"Total goals — Over/Under {line}", "outcomes": [over, under]})

    # --- BTTS -> [Yes, No] --------------------------------------------- #
    rb, sb = _book_market(event, RETAIL, "btts"), _sharp_market(event, "btts")
    yes = _outcome("Yes", _price(rb, lambda o: o["name"] == "Yes"), _price(sb, lambda o: o["name"] == "Yes"))
    no = _outcome("No", _price(rb, lambda o: o["name"] == "No"), _price(sb, lambda o: o["name"] == "No"))
    if yes and no:
        markets.append({"key": "btts", "name": "Both teams to score", "outcomes": [yes, no]})

    return markets


# --------------------------------------------------------------------------- #
# merge odds events into a feed built by datalayer.build
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c for c in s.lower() if c.isalnum())


def merge_odds_into_feed(feed: list[dict], events: list[dict]) -> list[dict]:
    """Match odds events to feed matches by team names (either orientation)."""
    index = {}
    for ev in events:
        key = frozenset({_norm(ev.get("home_team")), _norm(ev.get("away_team"))})
        index[key] = ev

    matched = 0
    for match in feed:
        key = frozenset({_norm(match.get("home")), _norm(match.get("away"))})
        ev = index.get(key)
        if ev:
            mk = normalize_event_markets(ev)
            if mk:
                match["markets"] = mk
                matched += 1
    print(f"merged odds into {matched}/{len(feed)} matches")
    return feed
