"""Scheduled feed builder + auto-settler — the deployable loop.

Runs the same build the CLI does, on an interval, writing the feed atomically
so the service never serves a half-written file, then triggers auto-settlement
(over HTTP when SERVICE_URL is set — keeps the service the single SQLite
writer — or directly against the ledger otherwise).

All configuration is environment variables, with API-budget-safe defaults:

  API_FOOTBALL_KEY  required
  ODDS_API_KEY      optional — odds merge is skipped without it
  FEED_PATH         default feed.json
  CACHE_DIR         default .cache  (mount it: caching is the API budget)
  LEAGUE / SEASON   default 1 / 2026
  MAX_MATCHES       default 8   (a full 71-match build would burn ~the whole
                                 free API-Football day on first run)
  TEAM_FORM         default 3
  SQUAD_LIMIT       default 8
  AUTO_LINEUPS      default 1
  FEED_INTERVAL     default 1800 seconds
  SERVICE_URL       e.g. http://api:8000 -> POST /api/settle/auto each cycle
  LEDGER_DB         default bets.db (only used without SERVICE_URL)
  RUN_ONCE          set to anything truthy for a single cycle (cron / smoke)

Run:  python -m datalayer.jobs
"""

from __future__ import annotations

import json
import os
import time

from .providers import ApiFootballProvider, FileCache
from .build import build_feed


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def run_cycle() -> int:
    key = os.environ["API_FOOTBALL_KEY"]
    odds_key = os.environ.get("ODDS_API_KEY") or os.environ.get("THE_ODDS_API_KEY")
    feed_path = os.environ.get("FEED_PATH", "feed.json")
    cache = FileCache(os.environ.get("CACHE_DIR", ".cache"))

    provider = ApiFootballProvider(key, cache=cache)
    feed = build_feed(
        provider,
        league=_env_int("LEAGUE", 1),
        season=_env_int("SEASON", 2026),
        max_matches=_env_int("MAX_MATCHES", 8),
        team_form=_env_int("TEAM_FORM", 3),
        squad_limit=_env_int("SQUAD_LIMIT", 8),
        auto_lineups=bool(_env_int("AUTO_LINEUPS", 1)),
    )

    if odds_key:
        try:
            from .odds import TheOddsApiProvider, merge_odds_into_feed
            odds = TheOddsApiProvider(odds_key, cache=cache)
            merge_odds_into_feed(feed, odds.events_odds())
        except Exception as e:
            print(f"odds merge skipped: {e}")

    tmp = feed_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(feed, f)
    os.replace(tmp, feed_path)  # atomic: the service never sees a partial feed
    print(f"wrote {len(feed)} matches -> {feed_path}")

    _settle()
    return len(feed)


def _settle() -> None:
    service_url = os.environ.get("SERVICE_URL")
    try:
        if service_url:
            import requests
            r = requests.post(f"{service_url}/api/settle/auto", timeout=120)
            print(f"auto-settle via {service_url}: {r.status_code} {r.text[:120]}")
        else:
            from .betlog import Ledger
            from .settler import Settler, ApiFootballResults
            provider = ApiFootballProvider(
                os.environ["API_FOOTBALL_KEY"],
                cache=FileCache(os.environ.get("CACHE_DIR", ".cache")),
            )
            ledger = Ledger(os.environ.get("LEDGER_DB", "bets.db"))
            out = Settler(ledger, ApiFootballResults(provider)).settle_open()
            print(f"auto-settle direct: {out}")
    except Exception as e:
        print(f"auto-settle skipped: {e}")


def main() -> None:
    interval = _env_int("FEED_INTERVAL", 1800)
    while True:
        try:
            run_cycle()
        except Exception as e:
            print(f"cycle failed: {e}")
        if os.environ.get("RUN_ONCE"):
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
