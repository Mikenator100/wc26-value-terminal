# Architecture

## Data flow

```
                    API-Football        The Odds API        FBref/SofaScore/FotMob
                    (stats, lineups,     (Bet365 +           (per-90 enrichment,
                     results)            Pinnacle odds)       optional)
                         |                    |                     |
                         v                    v                     v
   backend/datalayer/build.py  --------  odds.py  --------  enrich_soccerdata.py
        |  (normalize.py: per-90, roles, club/country split, team xG λ)
        |  (lineups.py: predicted XIs -> roles)
        v
     feed.json   ──────────────────────────────────────────────►  frontend/ValueTerminal.jsx
        ▲                                                          (prices every market client-side:
        │  served by                                                 score matrix + NB count engine
   backend/datalayer/service.py (Flask)                              + player props; renders UI)
        │  - GET /api/feed                                                 │
        │  - POST /api/bets   ◄───────── "Log bet" (set API_BASE) ─────────┘
        │  - POST /api/bets/<id>/settle
        │  - POST /api/settle/auto  ── settler.py ── results + closing price (CLV)
        │  - GET /api/performance   ── betlog/metrics.py + recommender.py
        v
     bets.db (SQLite ledger, on a mounted volume)
```

## The feed JSON contract (integration boundary)

`build.py` emits a list of match objects. This is the exact shape the frontend
(`ValueTerminal.jsx`) consumes; keep them in sync.

```jsonc
{
  "id": "1234567", "home": "Brazil", "away": "Morocco",
  "group": "Group C", "kickoff": "2026-06-20T18:00:00+00:00", "live": false,
  "xgHome": 1.70, "xgAway": 1.00,           // -> Poisson score matrix (all goals markets)
  "teamRates": {                            // -> negative-binomial team count markets
    "home": { "corners": 6.0, "cards": 1.6, "shots": 14, "sot": 5.2,
              "offsides": 1.8, "tackles": 16, "fouls": 11, "redProb": 0.05 },
    "away": { "...": "..." }
  },
  "markets": [                              // from odds.py; empty until odds merged
    { "key": "1x2",  "name": "Match result",
      "outcomes": [ {"label":"Brazil","bet365":1.95,"pinnacle":1.88},
                    {"label":"Draw","bet365":3.6,"pinnacle":3.7},
                    {"label":"Morocco","bet365":4.5,"pinnacle":4.3} ] },
    { "key": "ou25", "name": "Total goals — Over/Under 2.5", "outcomes": [ "..." ] },
    { "key": "btts", "name": "Both teams to score",         "outcomes": [ "..." ] }
  ],
  "bookPrices": { "Corners 3-way 9|Over 9": 2.10 },  // structured slip CSV ->
                                                     // catalog input prefill
  "players": [
    { "name": "Raphinha", "team": "Brazil",
      "clubRole": "W", "countryRole": "W",
      "predictedPos": "W", "confirmedPos": "W",
      "startProb": 0.85, "confirmedIn": false,
      "pen": true, "fk": true,              // set-piece duty flags
      "club":    { "shots": 2.6, "sot": 1.0, "goals": 0.45, "assists": 0.40, "cards": 0.15,
                   "tackles": 0.8, "fouls": 0.9, "fouled": 1.4, "passes": 28.5, "saves": null },
      "country": { "shots": 2.3, "sot": 0.9, "goals": 0.38, "assists": 0.33, "cards": 0.14,
                   "tackles": null, "fouls": null, "fouled": null, "passes": null, "saves": null },
      // count metrics are None when the source didn't record them -> the
      // pricing engines fall back to role baselines
      "_minutes": { "club": 2600, "country": 640 },   // sample-size weighting
      "bookOdds": { "Shots on target 1+": 1.40 },     // props CSV, when matched
      "last5": [ { "shots": 2, "sot": 1, "tackles": 0, "fouls": 1, "minutes": 90 } ]
      // per-match counts, newest first, played matches only (L5 strips)
    }
  ]
}
```

Notes:
- `1x2` outcomes MUST be ordered home, draw, away; `ou25` Over then Under; `btts`
  Yes then No — the frontend engine zips model probabilities to these by index.
- `markets` is filled by `odds.py`; `teamRates`/player count rates are the data
  still to be wired from real sources (see ROADMAP).
- `cards` on a player is a per-match booking probability; all other player rates
  are per 90 minutes.

## Modules

### Stats ETL
- `providers.py` — `ApiFootballProvider` (cache + throttle + paging). Endpoints:
  fixtures, lineups, teams/statistics, players (per season -> club/country blocks),
  predictions, fixture, events. `FileCache` (TTL per endpoint) — keep it; free tier
  is ~100 calls/day.
- `normalize.py` — pure transforms: `per90`, `card_probability`, `infer_role`
  (G/D/M/F + lineup grid + output -> ST/SS/W/CAM/CM/DM/FB/CB/GK), `split_club_country`
  (partition stat blocks by national team), `build_player_profile`, `team_lambdas`
  (Dixon-Coles-style xG, prefers provider prediction).
- `build.py` — `build_match` / `build_feed` assemble the feed; CLI flags `--key`,
  `--odds-key`, `--lineups-json`, `--max-matches`, `--season`. Country split is
  aggregated over several seasons (small international samples).

### Odds & lineups
- `odds.py` — `TheOddsApiProvider` + `normalize_event_markets` + `merge_odds_into_feed`
  (matches events to fixtures by team names, orientation-independent). Sharp priority:
  pinnacle -> betfair_ex_eu.
- `lineups.py` — `formation_to_roles` (lookup + generic fallback), `ManualLineups`
  (JSON), `HtmlPredictedLineups` (configurable CSS selectors; adapt per site).

### Pricing
- Score matrix + goals markets live in `ValueTerminal.jsx` (`scoreMatrix`,
  `deriveCatalog`) — frontend-only for now; Poisson grid, exact sums.
- `countmarkets.py` — `count_dist` (NB; r=None => Poisson), `over`/`at_least`/
  `team_most`, `team_markets(rates)`, `player_markets(exp)`, and
  `player_expectations(profile, pos, start_prob, team_xg, opp_xg, set_pieces)` which
  applies opponent strength + set-piece adjustments. Frontend mirror: `priceProps`,
  `deriveCatalog` team block.

### Learning loop (`betlog/`)
- `ledger.py` — `Ledger` (SQLite): `record`, `settle`, `open_bets`, `settled`.
  `odds_band` buckets odds for segmenting.
- `metrics.py` — `performance` (n, hit_rate, roi, pnl, avg_clv, brier, log_loss),
  `calibration_table`, `segment_stats`, `fit_calibrator` (binned isotonic via PAV;
  inactive < 50 settled).
- `recommender.py` — `Recommender(history)`: calibrates each candidate, scales edge
  by segment trust (CLV-driven, shrunk on small n), ranks by adjusted edge, attaches
  `expected_value`. `Candidate`/`Recommendation` dataclasses.

### Settlement & service
- `settler.py` — `resolve_outcome` (1X2, totals, BTTS, double chance, anytime
  goalscorer; returns None for unsupported -> manual), `Settler.settle_open`,
  `ApiFootballResults` (fixture score + goal scorers from events).
- `service.py` — `create_app()` Flask app; `app` is the WSGI entry point for gunicorn.

## Frontend (`ValueTerminal.jsx`)

Single React file, default export `App` with two tabs:
- **Markets** (`MarketsView`): fixture rail; per match — value-bet cards (with
  "Log bet"), priced 1X2/OU/BTTS tables, SGM builder, suggested multis, player
  prop cards (with PEN/FK badges and predicted/confirmed XI toggle), and the full
  "All markets" catalog (Result/Goals/Score/Half-time/Team markets) with price
  inputs for edge.
- **Track record** (`PerformanceView`): logged + sample bet history, calibration
  curve, cumulative P&L, segment trust, ledger table; settle controls feed metrics.

Constraints: Tailwind core utilities only in artifact context; no localStorage
(state only). For production, externalise `SAMPLE_MATCHES` to a `feed.json` fetch
and set `API_BASE`.
