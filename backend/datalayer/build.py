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
from .teamrates import team_rates, form_goal_averages, FINISHED

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


def team_count_rates(provider: Provider, team_id: int, recent_fixtures: list[dict]) -> Optional[dict]:
    """Per-game corner/card/shot rates over a team's recent finished fixtures.

    Costs 1 API call per finished fixture uncached; finished-match statistics
    are cached for a month, so repeat builds are free.
    """
    payloads = []
    for fx in recent_fixtures:
        status = ((fx.get("fixture") or {}).get("status") or {}).get("short")
        fid = (fx.get("fixture") or {}).get("id")
        if status not in FINISHED or not fid:
            continue
        try:
            payloads.append(provider.fixture_statistics(int(fid)))
        except Exception:
            continue
    return team_rates(team_id, payloads)


def _games_played(team_stats: dict) -> int:
    return (((team_stats or {}).get("fixtures") or {}).get("played") or {}).get("total") or 0


def _form_stats(team_id: int, recent_fixtures: list[dict]) -> Optional[dict]:
    """Synthesize the goals-average shape team_lambdas reads, from recent form."""
    fg = form_goal_averages(team_id, recent_fixtures)
    if not fg:
        return None
    return {"goals": {"for": {"average": {"total": fg[0]}},
                      "against": {"average": {"total": fg[1]}}}}


def build_match(
    provider: Provider,
    fixture: dict,
    league: int,
    season: int,
    squad_limit: int = 8,
    predicted_lineups: Optional[dict] = None,
    team_form: int = 5,
) -> dict:
    """Build one match object. `predicted_lineups` is an optional external feed:
    {team_name: [{name, pos, startProb}]} from a predicted-XI source.
    `team_form` = recent finished fixtures per team used for count rates
    (corners/cards/shots); 0 skips those API calls."""
    meta = _fixture_meta(fixture)

    # --- recent form window (shared by count rates + the xG fallback) ---- #
    recent: dict[str, list[dict]] = {"home": [], "away": []}
    if team_form:
        for side, tid in (("home", meta["homeId"]), ("away", meta["awayId"])):
            try:
                recent[side] = provider.team_recent_fixtures(tid, last=team_form)
            except Exception:
                pass

    # --- expected goals -------------------------------------------------- #
    try:
        hs = provider.team_statistics(league, season, meta["homeId"])
        as_ = provider.team_statistics(league, season, meta["awayId"])
    except Exception:
        hs = as_ = {}
    # early in the tournament the league-season stats have nothing to average;
    # fall back to goals for/against over the recent form window
    if _games_played(hs) < 3:
        hs = _form_stats(meta["homeId"], recent["home"]) or hs
    if _games_played(as_) < 3:
        as_ = _form_stats(meta["awayId"], recent["away"]) or as_
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

    # --- team count rates (corners / cards / shots ...) ------------------- #
    rates = None
    if team_form:
        try:
            rh = team_count_rates(provider, meta["homeId"], recent["home"])
            ra = team_count_rates(provider, meta["awayId"], recent["away"])
            if rh and ra:
                rates = {"home": rh, "away": ra}
        except Exception:
            pass

    out = {
        **{k: meta[k] for k in ("id", "home", "away", "group", "kickoff", "live")},
        "xgHome": xg_home,
        "xgAway": xg_away,
        "markets": [],  # merged in by the odds adapter
        "players": players,
    }
    if rates:
        out["teamRates"] = rates
    return out


def build_feed(
    provider: Provider,
    league: int = 1,
    season: int = 2026,
    only_upcoming: bool = True,
    max_matches: Optional[int] = None,
    predicted_lineups: Optional[dict] = None,
    team_form: int = 5,
    squad_limit: int = 8,
) -> list[dict]:
    fixtures = provider.fixtures(league, season)
    if only_upcoming:
        keep = {"NS", "1H", "HT", "2H", "ET", "P", "LIVE", "TBD"}
        fixtures = [f for f in fixtures if (f.get("fixture", {}).get("status", {}) or {}).get("short") in keep]
    if max_matches:
        fixtures = fixtures[:max_matches]
    return [build_match(provider, f, league, season, squad_limit=squad_limit,
                        predicted_lineups=predicted_lineups, team_form=team_form)
            for f in fixtures]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the World Cup data feed.")
    ap.add_argument("--key", required=True, help="API-Football key")
    ap.add_argument("--mode", default="direct", choices=["direct", "rapidapi"])
    ap.add_argument("--odds-key", default=None, help="The Odds API key (fills live Bet365 + Pinnacle prices)")
    ap.add_argument("--lineups-json", default=None, help="JSON file of predicted XIs (see ManualLineups)")
    ap.add_argument("--league", type=int, default=1)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--max-matches", type=int, default=None)
    ap.add_argument("--team-form", type=int, default=5,
                    help="recent finished fixtures per team for count rates; 0 disables (saves ~12 calls/match uncached)")
    ap.add_argument("--squad-limit", type=int, default=8,
                    help="players per team (each costs ~4 API calls uncached)")
    ap.add_argument("--out", default="feed.json")
    args = ap.parse_args()

    predicted = None
    if args.lineups_json:
        from .lineups import ManualLineups
        with open(args.lineups_json) as f:
            predicted = ManualLineups(json.load(f)).to_seed_dict()

    provider = ApiFootballProvider(args.key, mode=args.mode, cache=FileCache())
    feed = build_feed(provider, args.league, args.season, max_matches=args.max_matches,
                      predicted_lineups=predicted, team_form=args.team_form,
                      squad_limit=args.squad_limit)

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
