"""One-off live smoke test of the real-rate wiring. Budget-conscious:
~6 API-Football calls + 1 Odds API call, all cached on disk afterwards.
Not part of the test suite (tests stay offline)."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from datalayer.providers import ApiFootballProvider, FileCache
from datalayer.teamrates import team_rates
from datalayer.normalize import build_player_profile

KEY = os.environ["API_FOOTBALL_KEY"]
provider = ApiFootballProvider(KEY, cache=FileCache())

print("--- 1. WC2026 fixtures (league=1 season=2026) ---")
fixtures = provider.fixtures(1, 2026)
print(f"fixtures returned: {len(fixtures)}")
upcoming = [f for f in fixtures
            if (f.get("fixture", {}).get("status", {}) or {}).get("short") in ("NS", "TBD")]
print(f"upcoming: {len(upcoming)}")
if upcoming:
    fx = upcoming[0]
    print("first upcoming:", fx["teams"]["home"]["name"], "vs", fx["teams"]["away"]["name"],
          fx["fixture"]["date"])
else:
    fx = fixtures[0] if fixtures else None

if not fx:
    sys.exit("no fixtures — stopping")

home_id = fx["teams"]["home"]["id"]
home_name = fx["teams"]["home"]["name"]

print(f"\n--- 2. recent finished fixtures for {home_name} ({home_id}) ---")
recent = provider.team_recent_fixtures(home_id, last=3)
for r in recent:
    print(" ", r["fixture"]["id"], r["fixture"]["status"]["short"],
          r["teams"]["home"]["name"], "-", r["teams"]["away"]["name"])

print("\n--- 3. fixture statistics -> team rates ---")
finished = [r for r in recent
            if (r["fixture"]["status"] or {}).get("short") in ("FT", "AET", "PEN")][:2]
payloads = [provider.fixture_statistics(r["fixture"]["id"]) for r in finished]
rates = team_rates(home_id, payloads)
print(json.dumps(rates, indent=2))

print(f"\n--- 4. player count stats for one {home_name} player ---")
roster = provider.team_players(home_id, 2026)
print(f"roster entries: {len(roster)}")
if roster:
    entry = roster[0]
    pid, pname = entry["player"]["id"], entry["player"]["name"]
    blocks = provider.player_seasons(pid, [2025])  # one season only: 1 call
    print(f"{pname}: {len(blocks)} stat blocks")
    prof = build_player_profile(pname, home_name, blocks)
    if prof:
        print("club block:", json.dumps(prof["club"]))
        print("country block:", json.dumps(prof["country"]))

print("\n--- 5. The Odds API: WC odds (1 call) ---")
ok = os.environ.get("THE_ODDS_API_KEY")
if ok:
    from datalayer.odds import TheOddsApiProvider
    odds = TheOddsApiProvider(ok, cache=FileCache())
    events = odds.events_odds()
    print(f"events with odds: {len(events)}; requests remaining: {odds.requests_remaining}")
    if events:
        e = events[0]
        print("first event:", e.get("home_team"), "vs", e.get("away_team"),
              "| bookmakers:", [b["key"] for b in e.get("bookmakers", [])])
