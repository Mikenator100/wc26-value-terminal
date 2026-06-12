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


def run_cycle(squad_limit: int | None = None, auto_lineups: bool | None = None) -> int:
    key = os.environ["API_FOOTBALL_KEY"]
    odds_key = os.environ.get("ODDS_API_KEY") or os.environ.get("THE_ODDS_API_KEY")
    feed_path = os.environ.get("FEED_PATH", "feed.json")
    feed_dir = os.path.dirname(feed_path)
    if feed_dir:
        os.makedirs(feed_dir, exist_ok=True)
    cache = FileCache(os.environ.get("CACHE_DIR", ".cache"))

    # parse a structured slip CSV up front: its two teams get full squad
    # depth, every other team is capped (players are the API-budget hog)
    book = None
    deep_teams = None
    props_csv = os.environ.get("PROPS_CSV")
    if props_csv and os.path.exists(props_csv):
        try:
            from .csvbook import is_structured, load_structured, _team_norm
            if is_structured(props_csv):
                book = load_structured(props_csv)
                deep_teams = {_team_norm(book["home"]), _team_norm(book["away"])}
        except Exception as e:
            print(f"props csv pre-parse skipped: {e}")

    provider = ApiFootballProvider(key, cache=cache)

    # in-tournament Elo: re-derive ratings from the baseline + every finished
    # WC fixture so far (idempotent — recomputed from scratch each cycle) and
    # write them where elo.load_table() picks them up for this build
    try:
        from .elo import BASELINE, apply_results
        fixtures_all = provider.fixtures(_env_int("LEAGUE", 1), _env_int("SEASON", 2026))
        updated = apply_results(BASELINE, fixtures_all)
        changed = {k: v for k, v in updated.items() if v != BASELINE.get(k)}
        state_path = os.environ.setdefault(
            "ELO_STATE", os.path.join(_data_dir(), "elo_state.json"))
        with open(state_path, "w") as f:
            json.dump(updated, f)
        if changed:
            print(f"elo updated from results: {len(changed)} teams moved")
    except Exception as e:
        print(f"elo update skipped: {e}")

    feed = build_feed(
        provider,
        league=_env_int("LEAGUE", 1),
        season=_env_int("SEASON", 2026),
        max_matches=_env_int("MAX_MATCHES", 8),
        team_form=_env_int("TEAM_FORM", 3),
        squad_limit=squad_limit if squad_limit is not None else _env_int("SQUAD_LIMIT", 8),
        auto_lineups=bool(_env_int("AUTO_LINEUPS", 1)) if auto_lineups is None else auto_lineups,
        deep_teams=deep_teams,
    )

    if odds_key:
        try:
            from .odds import TheOddsApiProvider, merge_odds_into_feed
            odds = TheOddsApiProvider(odds_key, cache=cache)
            merge_odds_into_feed(feed, odds.events_odds())
            from .snapshots import apply_market_xg
            n = apply_market_xg(feed)
            if n:
                print(f"xg fitted to sharp no-vig prices for {n} matches")
        except Exception as e:
            print(f"odds merge skipped: {e}")

    if props_csv and os.path.exists(props_csv):
        try:
            from .csvbook import merge_structured_into_feed
            if book is not None:
                st = merge_structured_into_feed(feed, book)
                if not st["match_found"]:
                    # the slip's match has left the feed (kicked off/finished):
                    # nothing is attached, no placeholders are injected — the
                    # file is inert until the next one replaces it
                    print(f"book csv stale (event not in feed) — ignored: {props_csv}")
                else:
                    print(f"book csv: {st}")
            else:
                from .csvprops import load_props_csv, merge_props_into_feed
                n = merge_props_into_feed(feed, load_props_csv(props_csv))
                print(f"props csv: prices attached to {n} players")
        except Exception as e:
            print(f"props csv skipped: {e}")

    tmp = feed_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(feed, f)
    os.replace(tmp, feed_path)  # atomic: the service never sees a partial feed
    print(f"wrote {len(feed)} matches -> {feed_path}")

    # record an odds/model snapshot per cycle — the backtest's data source
    # (the last snapshot before kickoff doubles as the closing line)
    rows: list[dict] = []
    try:
        from .snapshots import snapshot_feed
        rows = snapshot_feed(feed)
        if rows:
            with open(_history_path(), "a") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            print(f"snapshot: {len(rows)} outcomes -> {_history_path()}")
    except Exception as e:
        print(f"snapshot skipped: {e}")

    # paper-trade the strategy: log every qualifying pick into a separate
    # ledger (1u flat, no money) so calibration and segment trust accumulate
    # settled volume fast — the betlog needs ~50 settled bets to switch on
    if rows and _env_int("PAPER_BETS", 1):
        try:
            from .betlog import Ledger
            from .backtest import pick_paper_bets
            paper = Ledger(_paper_db_path())
            existing = {(b["match_id"], b["market"], b["selection"]) for b in paper.all()}
            logged = 0
            picks = pick_paper_bets(
                rows,
                weight=float(os.environ.get("PAPER_WEIGHT", "") or 0.3),
                threshold=float(os.environ.get("PAPER_EDGE", "") or 0.03),
            )
            for p in picks:
                key = (p["match_id"], p["market"], p["selection"])
                if key in existing:
                    continue
                paper.record(match_id=p["match_id"], market=p["market"],
                             selection=p["selection"], model_prob=p["model_prob"],
                             price=p["price"], stake=1.0, bankroll=100.0)
                existing.add(key)
                logged += 1
            if logged:
                print(f"paper: logged {logged} picks -> {_paper_db_path()}")
        except Exception as e:
            print(f"paper logging skipped: {e}")

    _settle()
    return len(feed)


def _data_dir() -> str:
    return os.path.dirname(os.environ.get("FEED_PATH", "feed.json")) or "."


def _history_path() -> str:
    return os.environ.get("HISTORY_PATH", os.path.join(_data_dir(), "history.jsonl"))


def _paper_db_path() -> str:
    return os.environ.get("PAPER_DB", os.path.join(_data_dir(), "paper.db"))


def _closing_lookup_from_history(history_path: str):
    """match (match_id, selection label) -> last recorded bet365 price."""
    index: dict[tuple, float] = {}
    try:
        with open(history_path) as f:
            for line in f:
                r = json.loads(line)
                price = r.get("bet365") or r.get("pinnacle")
                if price:
                    index[(r.get("match_id"), r.get("label"))] = price
    except FileNotFoundError:
        pass
    return lambda match_id, market, selection: index.get((match_id, selection))


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

    # the paper ledger always settles locally (it lives in this container),
    # with the closing price looked up from the snapshot history -> CLV
    try:
        if os.path.exists(_paper_db_path()):
            from .betlog import Ledger
            from .settler import Settler, ApiFootballResults
            provider = ApiFootballProvider(
                os.environ["API_FOOTBALL_KEY"],
                cache=FileCache(os.environ.get("CACHE_DIR", ".cache")),
            )
            out = Settler(Ledger(_paper_db_path()), ApiFootballResults(provider),
                          closing_lookup=_closing_lookup_from_history(_history_path())).settle_open()
            if out.get("settled") or out.get("skipped"):
                print(f"paper settle: {out}")
    except Exception as e:
        print(f"paper settle skipped: {e}")


def run_loop() -> None:
    """The scheduler. Ephemeral hosts (Render free) boot with no feed file and
    a cold API cache, and a full build takes minutes — so the first pass skips
    player squads (the expensive part) to get a usable feed up in ~a minute,
    then the full cycle runs immediately after."""
    interval = _env_int("FEED_INTERVAL", 1800)
    first = True
    while True:
        try:
            if first and _env_int("FAST_FIRST_CYCLE", 1):
                run_cycle(squad_limit=0, auto_lineups=False)
            run_cycle()
        except Exception as e:
            print(f"cycle failed: {e}")
        first = False
        if os.environ.get("RUN_ONCE"):
            break
        time.sleep(interval)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
