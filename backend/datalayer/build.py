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
from .normalize import build_player_profile, team_lambdas, recent_player_counts
from .teamrates import team_rates, form_goal_averages, team_form_stats, FINISHED
from .lineups import predicted_from_recent_xis, name_key

# seasons to aggregate the country split over (international samples are
# small). Each season is one API call per player — the single biggest cost in
# a build — so this stays as short as the data allows.
COUNTRY_SEASONS = [2024, 2025, 2026]

# squad depth for matches WITHOUT a slip CSV: deep squads only pay off when
# there are slip prices to match against. The minutes-ordered slice keeps the
# regulars, and the predicted XI is always added on top, so this is plenty.
SHALLOW_SQUAD = 16
# hard per-team cap on players built (each costs 3 API calls). The predicted
# XI (11) + 3 bench is enough for prop coverage; slip-CSV players are added
# separately and aren't subject to this.
MAX_PLAYERS = 14


def _entry_minutes(entry: dict) -> float:
    """Total minutes across a team_players entry's stat blocks — used to rank
    a roster so the squad cap keeps regulars (stars) over fringe players."""
    return sum(((s.get("games") or {}).get("minutes") or 0)
               for s in (entry.get("statistics") or []))

# WC2026 is at neutral venues — no home advantage — except for the three host
# nations, who really do get the crowd (~ the usual venue edge on goals)
HOSTS = {"usa", "united states", "mexico", "canada"}
HOST_ADV = 1.08


def _venue_advantage(home_name: str, away_name: str) -> float:
    h = (home_name or "").lower() in HOSTS
    a = (away_name or "").lower() in HOSTS
    if h and not a:
        return HOST_ADV
    if a and not h:
        return 1 / HOST_ADV
    return 1.0


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
    """Per-game corner/card/shot rates over a team's recent finished fixtures,
    opponent-normalised: counts racked up against a weak side are discounted so
    a minnow-heavy schedule doesn't inflate a team's corner/shot/card form.

    Costs 1 API call per finished fixture uncached; finished-match statistics
    are cached for a month, so repeat builds are free.
    """
    from .elo import load_table, rating, goal_factor
    table = load_table()
    payloads, opp_factors = [], []
    for fx in recent_fixtures:
        status = ((fx.get("fixture") or {}).get("status") or {}).get("short")
        fid = (fx.get("fixture") or {}).get("id")
        if status not in FINISHED or not fid:
            continue
        try:
            payloads.append(provider.fixture_statistics(int(fid)))
        except Exception:
            continue
        teams = fx.get("teams") or {}
        is_home = (teams.get("home") or {}).get("id") == team_id
        opp = ((teams.get("away") if is_home else teams.get("home")) or {}).get("name", "")
        opp_factors.append(goal_factor(rating(opp, table)))
    return team_rates(team_id, payloads, opp_factors=opp_factors)


def _games_played(team_stats: dict) -> int:
    return (((team_stats or {}).get("fixtures") or {}).get("played") or {}).get("total") or 0


def _form_stats(team_id: int, recent_fixtures: list[dict]) -> Optional[dict]:
    """Synthesize the goals-average shape team_lambdas reads, from recent form
    weighted by opponent Elo (friendlies against minnows stop inflating xG)."""
    from .elo import load_table, rating, goal_factor
    table = load_table()
    fg = form_goal_averages(team_id, recent_fixtures,
                            elo_factor=lambda opp: goal_factor(rating(opp, table)))
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
    auto_lineups: bool = False,
    deep_teams: Optional[set] = None,
) -> dict:
    """Build one match object. `predicted_lineups` is an optional external feed:
    {team_name: [{name, pos, startProb}]} from a predicted-XI source.
    `team_form` = recent finished fixtures per team used for count rates
    (corners/cards/shots); 0 skips those API calls. `auto_lineups` infers a
    predicted XI from each team's recent confirmed XIs (1 extra call per
    finished form fixture, cached long); manual `predicted_lineups` win.
    `deep_teams` (normalised names): only these get the full `squad_limit` —
    everyone else is capped at SHALLOW_SQUAD, because each player costs
    len(COUNTRY_SEASONS) API calls and deep squads only pay off where a slip
    CSV has prices to match."""
    meta = _fixture_meta(fixture)

    # --- recent form window (shared by count rates + the xG fallback) ---- #
    # fetch a 10-match window in one cheap call per team: the descriptive Team
    # Stats panel reads all of it, while the (expensive, per-fixture-stats)
    # count rates are capped to the first `team_form` finished games below.
    STATS_WINDOW = 10
    recent: dict[str, list[dict]] = {"home": [], "away": []}
    if team_form:
        for side, tid in (("home", meta["homeId"]), ("away", meta["awayId"])):
            try:
                recent[side] = provider.team_recent_fixtures(tid, last=max(STATS_WINDOW, team_form))
            except Exception:
                pass

    def _rate_window(side: str) -> list[dict]:
        # only the most-recent `team_form` FINISHED fixtures hit fixture_statistics
        fin = [f for f in recent[side]
               if ((f.get("fixture") or {}).get("status") or {}).get("short") in FINISHED]
        return fin[:team_form]

    # --- expected goals -------------------------------------------------- #
    try:
        hs = provider.team_statistics(league, season, meta["homeId"])
        as_ = provider.team_statistics(league, season, meta["awayId"])
    except Exception:
        hs = as_ = {}
    adv = _venue_advantage(meta["home"], meta["away"])
    degenerate = _games_played(hs) < 3 or _games_played(as_) < 3

    if degenerate:
        # early in the tournament the league-season stats have nothing to
        # average. Elo sets the SUPREMACY (who wins — validated as the model's
        # stronger signal against results), while the match TOTAL comes from
        # the two teams' Elo-weighted form goals (a flat 2.6 total made every
        # totals/BTTS prediction no-skill; team-specific totals beat it on the
        # finished-match backtest). The total is shrunk toward the average.
        from .elo import load_table, rating, goal_factor, elo_lambdas, TOTAL_GOALS
        table = load_table()
        rh, ra = rating(meta["home"], table), rating(meta["away"], table)
        if rh is not None and ra is not None:
            ef = lambda opp: goal_factor(rating(opp, table))
            fh = form_goal_averages(meta["homeId"], recent["home"], elo_factor=ef)
            fa = form_goal_averages(meta["awayId"], recent["away"], elo_factor=ef)
            if fh and fa:
                form_total = 0.5 * (fh[0] + fa[1]) + 0.5 * (fa[0] + fh[1])
                total = max(1.9, min(3.8, 0.45 * TOTAL_GOALS + 0.55 * form_total))
            else:
                total = TOTAL_GOALS
            xg_home, xg_away = elo_lambdas(rh, ra, venue_mult=adv, total=total)
        else:
            # unrated team: the old form-average fallback
            hs2 = _form_stats(meta["homeId"], recent["home"]) or hs
            as2 = _form_stats(meta["awayId"], recent["away"]) or as_
            xg_home, xg_away = team_lambdas(hs2, as2, home_adv=adv)
    else:
        preds = None
        try:
            preds = provider.predictions(int(meta["id"]))
        except Exception:
            pass
        xg_home, xg_away = team_lambdas(hs, as_, predictions=preds, home_adv=adv)

    # --- confirmed lineup (if released) --------------------------------- #
    confirmed = {}
    try:
        confirmed = _confirmed_lineup_index(provider.lineups(int(meta["id"])))
    except Exception:
        pass

    # --- predicted XIs from recent confirmed lineups ---------------------- #
    auto_pred: dict[str, list[dict]] = {}
    if auto_lineups and team_form:
        for side, tid, tname in (("home", meta["homeId"], meta["home"]),
                                 ("away", meta["awayId"], meta["away"])):
            if predicted_lineups and tname in predicted_lineups:
                continue  # a manual XI always wins
            payloads = []
            for fx in recent[side]:
                status = ((fx.get("fixture") or {}).get("status") or {}).get("short")
                fid = (fx.get("fixture") or {}).get("id")
                if status not in FINISHED or not fid:
                    continue
                try:
                    payloads.append(provider.lineups(int(fid), ttl=30 * 86400))
                except Exception:
                    continue
            xi = predicted_from_recent_xis(tid, payloads)
            if xi:
                auto_pred[tname] = xi

    # --- per-player recent match counts (the last-5 strips) --------------- #
    recent_counts: dict[int, list[dict]] = {}
    if team_form:
        payloads = []
        for side in ("home", "away"):
            for fx in recent[side]:
                status = ((fx.get("fixture") or {}).get("status") or {}).get("short")
                fid = (fx.get("fixture") or {}).get("id")
                if status not in FINISHED or not fid:
                    continue
                try:
                    payloads.append(provider.fixture_players(int(fid)))
                except Exception:
                    continue
        recent_counts = recent_player_counts(payloads)

    # --- players --------------------------------------------------------- #
    players: list[dict] = []
    for team_name, team_id in ((meta["home"], meta["homeId"]), (meta["away"], meta["awayId"])):
        limit = squad_limit
        if deep_teams is not None:
            from .csvbook import _team_norm
            if _team_norm(team_name) not in deep_teams:
                limit = min(squad_limit, SHALLOW_SQUAD)
        try:
            roster_all = provider.team_players(team_id, season)
        except Exception:
            roster_all = []
        # order by minutes so the cap keeps the regulars (the stars), not an
        # arbitrary API slice that surfaced fringe players and three keepers
        roster = sorted(roster_all, key=_entry_minutes, reverse=True)[:limit]

        pred = (predicted_lineups or {}).get(team_name) or auto_pred.get(team_name) or []
        pred_by_id = {p["id"]: p for p in pred if p.get("id")}
        pred_by_name = {name_key(p["name"]): p for p in pred}
        # when an XI is predicted, players outside it are bench material —
        # don't hand them the generic 0.7
        default_sp = 0.25 if pred else 0.7

        # roster lookups so a predicted-XI player who's also on the roster
        # keeps the roster's canonical name (lineups carry name variants)
        roster_name_by_id, roster_id_by_namekey = {}, {}
        for entry in roster:
            p = entry.get("player", {})
            if p.get("id"):
                roster_name_by_id[p["id"]] = p.get("name")
                roster_id_by_namekey[name_key(p.get("name") or "")] = p["id"]

        # pick the ≤MAX_PLAYERS to build BEFORE fetching (each is 3 API calls):
        # the predicted XI first (who actually matters), then fill from the
        # minutes-ordered roster. Keeps the squad small but always star-led.
        cap = min(limit, MAX_PLAYERS)
        candidates: list[tuple] = []  # (pid, pname, pinfo)
        seen: set = set()
        for pinfo in pred:
            pid = pinfo.get("id")
            if not pid:  # name-only predicted entry: resolve to a roster id
                pid = roster_id_by_namekey.get(name_key(pinfo.get("name") or ""))
            if not pid or pid in seen:
                continue
            pname = roster_name_by_id.get(pid) or pinfo.get("name")
            candidates.append((pid, pname, pinfo)); seen.add(pid)
        for entry in roster:
            if len(candidates) >= cap:
                break
            p = entry.get("player", {})
            pid, pname = p.get("id"), p.get("name")
            if pid and pid not in seen:
                candidates.append((pid, pname,
                                   pred_by_name.get(name_key(pname or ""), {}))); seen.add(pid)
        candidates = candidates[:cap]

        for pid, pname, pinfo in candidates:
            if not pid or not pname:
                continue
            try:
                blocks = provider.player_seasons(pid, COUNTRY_SEASONS)
            except Exception:
                continue
            profile = build_player_profile(
                player_name=pname,
                national_team_name=team_name,
                stat_blocks=blocks,
                predicted_pos=pinfo.get("pos"),
                predicted_start_prob=pinfo.get("startProb", default_sp),
            )
            if not profile:
                continue
            profile["last5"] = recent_counts.get(pid, [])[:5]  # newest first
            if pname in confirmed:
                profile["confirmedIn"] = True
                profile["startProb"] = 1.0
                profile["confirmedPos"] = profile["predictedPos"]
            players.append(profile)

    # --- team count rates (corners / cards / shots ...) ------------------- #
    rates = None
    if team_form:
        try:
            rh = team_count_rates(provider, meta["homeId"], _rate_window("home"))
            ra = team_count_rates(provider, meta["awayId"], _rate_window("away"))
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
    # descriptive head-to-head stats panel (form, CS%, FTS%, goals, corners)
    if team_form:
        out["teamStats"] = {
            "home": team_form_stats(meta["homeId"], recent["home"], (rates or {}).get("home")),
            "away": team_form_stats(meta["awayId"], recent["away"], (rates or {}).get("away")),
        }
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
    auto_lineups: bool = False,
    deep_teams: Optional[set] = None,
) -> list[dict]:
    fixtures = provider.fixtures(league, season)
    if only_upcoming:
        keep = {"NS", "1H", "HT", "2H", "ET", "P", "LIVE", "TBD"}
        fixtures = [f for f in fixtures if (f.get("fixture", {}).get("status", {}) or {}).get("short") in keep]
    if max_matches:
        fixtures = fixtures[:max_matches]
    return [build_match(provider, f, league, season, squad_limit=squad_limit,
                        predicted_lineups=predicted_lineups, team_form=team_form,
                        auto_lineups=auto_lineups, deep_teams=deep_teams)
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
    ap.add_argument("--auto-lineups", action="store_true",
                    help="infer predicted XIs from recent confirmed lineups (1 call per finished form fixture, cached a month)")
    ap.add_argument("--props-csv", default=None,
                    help="hand-collected Bet365 player-prop CSV; prices attach to players as bookOdds")
    ap.add_argument("--out", default="feed.json")
    args = ap.parse_args()

    predicted = None
    if args.lineups_json:
        from .lineups import ManualLineups
        with open(args.lineups_json) as f:
            predicted = ManualLineups(json.load(f)).to_seed_dict()

    # a structured slip CSV is parsed up front so its two teams (and only
    # those) get the full squad depth
    book = None
    deep_teams = None
    if args.props_csv:
        from .csvbook import is_structured, load_structured, _team_norm
        if is_structured(args.props_csv):
            book = load_structured(args.props_csv)
            deep_teams = {_team_norm(book["home"]), _team_norm(book["away"])}

    provider = ApiFootballProvider(args.key, mode=args.mode, cache=FileCache())
    feed = build_feed(provider, args.league, args.season, max_matches=args.max_matches,
                      predicted_lineups=predicted, team_form=args.team_form,
                      squad_limit=args.squad_limit, auto_lineups=args.auto_lineups,
                      deep_teams=deep_teams)

    if args.odds_key:
        from .odds import TheOddsApiProvider, merge_odds_into_feed
        odds = TheOddsApiProvider(args.odds_key, cache=FileCache())
        merge_odds_into_feed(feed, odds.events_odds())
        if odds.requests_remaining:
            print(f"odds requests remaining: {odds.requests_remaining}")
        from .snapshots import apply_market_xg
        n = apply_market_xg(feed)
        if n:
            print(f"xg fitted to sharp no-vig prices for {n} matches")

    if args.props_csv:
        if book is not None:
            from .csvbook import merge_structured_into_feed
            st = merge_structured_into_feed(feed, book)
            print(f"book csv: {st}")
        else:
            from .csvprops import load_props_csv, merge_props_into_feed
            n = merge_props_into_feed(feed, load_props_csv(args.props_csv))
            print(f"props csv: prices attached to {n} players")

    with open(args.out, "w") as f:
        json.dump(feed, f, indent=2)
    print(f"wrote {len(feed)} matches -> {args.out}")


if __name__ == "__main__":
    main()
