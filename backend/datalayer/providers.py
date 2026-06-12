"""Data providers.

Right now there is one concrete provider, API-Football (api-sports.io), which
is the only widely-available source that returns club and national-team player
statistics as *separate* records — the split this whole project depends on.

The Provider protocol is deliberately small so other sources (a scraped
predicted-lineup site, soccerdata/FBref enrichment, a different paid API) can be
dropped in without touching the normalisation or build layers.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional, Protocol

import requests


# --------------------------------------------------------------------------- #
# simple on-disk cache — the free tier is ~100 calls/day, so caching is not
# optional. Keyed by endpoint+params, with a per-endpoint TTL.
# --------------------------------------------------------------------------- #
class FileCache:
    def __init__(self, root: str = ".cache", default_ttl: int = 3600):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl

    def _key(self, endpoint: str, params: dict) -> Path:
        blob = endpoint + "?" + json.dumps(params, sort_keys=True)
        h = hashlib.sha256(blob.encode()).hexdigest()[:24]
        return self.root / f"{h}.json"

    def get(self, endpoint: str, params: dict, ttl: Optional[int] = None) -> Optional[Any]:
        path = self._key(endpoint, params)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > (ttl if ttl is not None else self.default_ttl):
            return None
        return json.loads(path.read_text())

    def put(self, endpoint: str, params: dict, value: Any) -> None:
        self._key(endpoint, params).write_text(json.dumps(value))


class Provider(Protocol):
    def fixtures(self, league: int, season: int) -> list[dict]: ...
    def lineups(self, fixture_id: int) -> list[dict]: ...
    def team_statistics(self, league: int, season: int, team: int) -> dict: ...
    def player_seasons(self, player_id: int, seasons: list[int]) -> list[dict]: ...
    def team_players(self, team: int, season: int) -> list[dict]: ...
    def predictions(self, fixture_id: int) -> Optional[dict]: ...
    def team_recent_fixtures(self, team: int, last: int = 5) -> list[dict]: ...
    def fixture_statistics(self, fixture_id: int) -> list[dict]: ...
    def fixture_players(self, fixture_id: int, ttl: int = 30 * 86400) -> list[dict]: ...


# --------------------------------------------------------------------------- #
class ApiFootballProvider:
    """Concrete provider for api-sports.io v3 (also works via RapidAPI).

    Pass host/header style for whichever subscription you hold:
      direct:   ApiFootballProvider(key, mode="direct")
      rapidapi: ApiFootballProvider(key, mode="rapidapi")
    """

    BASES = {
        "direct": "https://v3.football.api-sports.io",
        "rapidapi": "https://api-football-v1.p.rapidapi.com/v3",
    }

    def __init__(
        self,
        api_key: str,
        mode: str = "direct",
        cache: Optional[FileCache] = None,
        min_interval: float = 0.5,  # be gentle: throttle between live calls
    ):
        self.base = self.BASES[mode]
        if mode == "rapidapi":
            self.headers = {
                "x-rapidapi-key": api_key,
                "x-rapidapi-host": "api-football-v1.p.rapidapi.com",
            }
        else:
            self.headers = {"x-apisports-key": api_key}
        self.cache = cache or FileCache()
        self.min_interval = min_interval
        self._last_call = 0.0

    # -- low-level fetch with cache + throttle + paging ------------------- #
    def _get(self, endpoint: str, params: dict, ttl: int = 3600) -> list[dict]:
        cached = self.cache.get(endpoint, params, ttl)
        if cached is not None:
            return cached

        gap = self.min_interval - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)

        url = f"{self.base}/{endpoint}"
        resp = requests.get(url, headers=self.headers, params=params, timeout=20)
        self._last_call = time.time()
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("errors"):
            raise RuntimeError(f"API-Football error on {endpoint}: {payload['errors']}")

        results = payload.get("response", [])

        # follow pagination when present
        paging = payload.get("paging", {})
        cur, total = paging.get("current", 1), paging.get("total", 1)
        while cur < total:
            cur += 1
            time.sleep(self.min_interval)
            page = requests.get(
                url, headers=self.headers, params={**params, "page": cur}, timeout=20
            ).json()
            results.extend(page.get("response", []))

        self.cache.put(endpoint, params, results)
        return results

    # -- typed endpoints ------------------------------------------------- #
    def fixtures(self, league: int, season: int) -> list[dict]:
        # schedule changes during a tournament; short TTL
        return self._get("fixtures", {"league": league, "season": season}, ttl=1800)

    def lineups(self, fixture_id: int, ttl: int = 300) -> list[dict]:
        # lineups appear ~40 min before KO; cache briefly so we pick up the
        # drop. Pass a long ttl for finished fixtures (they never change).
        return self._get("fixtures/lineups", {"fixture": fixture_id}, ttl=ttl)

    def team_statistics(self, league: int, season: int, team: int) -> dict:
        res = self._get(
            "teams/statistics",
            {"league": league, "season": season, "team": team},
            ttl=21600,
        )
        # this endpoint returns a single object, not a list
        return res if isinstance(res, dict) else (res[0] if res else {})

    def player_seasons(self, player_id: int, seasons: list[int]) -> list[dict]:
        """All statistics blocks for a player across the given seasons.

        Each block is one (team, league) the player featured in, which is how we
        separate club vs country later.
        """
        out: list[dict] = []
        for s in seasons:
            res = self._get("players", {"id": player_id, "season": s}, ttl=43200)
            for entry in res:
                for stat in entry.get("statistics", []):
                    out.append({"player": entry.get("player", {}), "season": s, **stat})
        return out

    def team_players(self, team: int, season: int) -> list[dict]:
        return self._get("players", {"team": team, "season": season}, ttl=43200)

    def predictions(self, fixture_id: int) -> Optional[dict]:
        res = self._get("predictions", {"fixture": fixture_id}, ttl=3600)
        return res[0] if res else None

    def team_recent_fixtures(self, team: int, last: int = 5) -> list[dict]:
        """A team's most recent fixtures across all competitions (form window)."""
        return self._get("fixtures", {"team": team, "last": last}, ttl=21600)

    def fixture_statistics(self, fixture_id: int) -> list[dict]:
        # match-level counts (corners, shots, fouls, cards) per team; finished
        # matches never change, so cache for a month
        return self._get("fixtures/statistics", {"fixture": fixture_id}, ttl=30 * 86400)

    def fixture_players(self, fixture_id: int, ttl: int = 30 * 86400) -> list[dict]:
        # per-player match stats (shots/SOT/tackles/fouls/minutes) for both
        # teams of one fixture — the source of the players' last-5 strips
        return self._get("fixtures/players", {"fixture": fixture_id}, ttl=ttl)

    def fixture(self, fixture_id: int) -> Optional[dict]:
        res = self._get("fixtures", {"id": fixture_id}, ttl=300)
        return res[0] if res else None

    def events(self, fixture_id: int) -> list[dict]:
        return self._get("fixtures/events", {"fixture": fixture_id}, ttl=300)
