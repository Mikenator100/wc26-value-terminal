"""Assemble normalised pieces into the match objects the terminal renders.

Output shape (one per fixture) matches ValueTerminal.jsx exactly:

    {
      id, home, away, group, kickoff, live, xgHome, xgAway,
      markets: [...],          # filled by the odds adapter (separate module)
      players: [ {name, clubRole, countryRole, predictedPos, confirmedPos,
                  startProb, confirmedIn, club:{...}, country:{...}} ]
    }

Run as a CLI to dump a feed the React app can fetch:
    python -m datalayer.build --league 1 --season 2026 --out feed.json
"""

from __future__ import annotations

import argparse
import json
from typing import Optional

from .providers import ApiFootballProvider, FileCache, Provider
from .normalize import build_player_profile, team_lambdas

# seasons to aggregate the country split over (international samples are small)
COUNTRY_SEASONS = [2023, 2024, 2025, 2026]


def _fixture_meta(fx: dict) -> dict:
    status = (fx.get("fixture", {}).get("status", {}) or {}).get("short", "NS")
    live = status in {"1H", "HT", "2H", "ET", "BT", "P", "LIVE"}
    return {
        "id": str(fx.get("fixture", {}).get("id")),
        "home": fx.get("teams", {}).get("home", {}).get("name"),
        "away": fx.get("teams", {}).get("away", {}).get("name"),
        "homeId": fx.get("teams", {}).get("home", {}).get("id"),
        "awayId": fx.get("teams", {}).get("away", {}).get("id"),
        "group": (fx.get("league", {}) or {}).get("round", ""),
        "kickoff": (fx.get("fixture", {}) or {}).get("date", ""),
        "live": live,
    }


def _confirmed_lineup_index(lineups: list[dict]) -> dict[str, str]:
    """name -> confirmed position, for players in a released starting XI."""
    idx: dict[str, str] = {}
    for side in lineups:
        for slot in side.get("startXI", []):
            p = slot.get("player", {})
            name = p.get("name")
            if name:
                # map the formation position letter through to a role later;
                # here we keep the raw pos and let normalize.infer_role refine
                idx[name] = p.get("pos") or ""
    return idx


def build_match(
    provider: Provider,
    fixture: dict,
    league: int,
    season: int,
    squad_limit: int = 8,
    predicted_lineups: Optional[dict] = None,
) -> dict:
    """Build one match object. `predicted_lineups` is an optional external feed:
    {team_name: [{name, pos, startProb}]} from a predicted-XI source."""
    meta = _fixture_meta(fixture)

    # --- expected goals -------------------------------------------------- #
    try:
        hs = provider.team_statistics(league, season, meta["homeId"])
        as_ = provider.team_statistics(league, season, meta["awayId"])
    except Exception:
        hs = as_ = {}
    preds = None
    try:
        preds = provider.predictions(int(meta["id"]))
    except Exception:
        pass
    xg_home, xg_away = team_lambdas(hs, as_, predictions=preds)

    # --- confirmed lineup (if released) --------------------------------- #
    confirmed = {}
    try:
        confirmed = _confirmed_lineup_index(provider.lineups(int(meta["id"])))
    except Exception:
        pass

    # --- players --------------------------------------------------------- #
    players: list[dict] = []
    for team_name, team_id in ((meta["home"], meta["homeId"]), (meta["away"], meta["awayId"])):
        try:
            roster = provider.team_players(team_id, season)[:squad_limit]
        except Exception:
            roster = []
        pred = (predicted_lineups or {}).get(team_name, [])
        pred_by_name = {p["name"]: p for p in pred}

        for entry in roster:
            pid = entry.get("player", {}).get("id")
            pname = entry.get("player", {}).get("name")
            if not pid or not pname:
                continue
            try:
                blocks = provider.player_seasons(pid, COUNTRY_SEASONS)
            except Exception:
                continue

            pinfo = pred_by_name.get(pname, {})
            profile = build_player_profile(
                player_name=pname,
                national_team_name=team_name,
                stat_blocks=blocks,
                predicted_pos=pinfo.get("pos"),
                predicted_start_prob=pinfo.get("startProb", 0.7),
            )
            if not profile:
                continue

            # overlay confirmed XI when present
            if pname in confirmed:
                profile["confirmedIn"] = True
                profile["startProb"] = 1.0
                # keep a role inferred from confirmed slot if it differs
                profile["confirmedPos"] = profile["predictedPos"]
            players.append(profile)

    return {
        **{k: meta[k] for k in ("id", "home", "away", "group", "kickoff", "live")},
        "xgHome": xg_home,
        "xgAway": xg_away,
        "markets": [],  # merged in by the odds adapter
        "players": players,
    }


def build_feed(
    provider: Provider,
    league: int = 1,
    season: int = 2026,
    only_upcoming: bool = True,
    max_matches: Optional[int] = None,
    predicted_lineups: Optional[dict] = None,
) -> list[dict]:
    fixtures = provider.fixtures(league, season)
    if only_upcoming:
        keep = {"NS", "1H", "HT", "2H", "ET", "P", "LIVE", "TBD"}
        fixtures = [f for f in fixtures if (f.get("fixture", {}).get("status", {}) or {}).get("short") in keep]
    if max_matches:
        fixtures = fixtures[:max_matches]
    return [build_match(provider, f, league, season, predicted_lineups=predicted_lineups) for f in fixtures]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the World Cup data feed.")
    ap.add_argument("--key", required=True, help="API-Football key")
    ap.add_argument("--mode", default="direct", choices=["direct", "rapidapi"])
    ap.add_argument("--odds-key", default=None, help="The Odds API key (fills live Bet365 + Pinnacle prices)")
    ap.add_argument("--lineups-json", default=None, help="JSON file of predicted XIs (see ManualLineups)")
    ap.add_argument("--league", type=int, default=1)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--max-matches", type=int, default=None)
    ap.add_argument("--out", default="feed.json")
    args = ap.parse_args()

    predicted = None
    if args.lineups_json:
        from .lineups import ManualLineups
        with open(args.lineups_json) as f:
            predicted = ManualLineups(json.load(f)).to_seed_dict()

    provider = ApiFootballProvider(args.key, mode=args.mode, cache=FileCache())
    feed = build_feed(provider, args.league, args.season, max_matches=args.max_matches, predicted_lineups=predicted)

    if args.odds_key:
        from .odds import TheOddsApiProvider, merge_odds_into_feed
        odds = TheOddsApiProvider(args.odds_key, cache=FileCache())
        merge_odds_into_feed(feed, odds.events_odds())
        if odds.requests_remaining:
            print(f"odds requests remaining: {odds.requests_remaining}")

    with open(args.out, "w") as f:
        json.dump(feed, f, indent=2)
    print(f"wrote {len(feed)} matches -> {args.out}")


if __name__ == "__main__":
    main()
