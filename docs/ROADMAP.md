# Roadmap

## Done (built & tested)

- **Goals markets** — Poisson score matrix: 1X2, double chance, draw-no-bet,
  totals (every line), team totals, BTTS, correct score, winning margin, exact
  goals, odd/even, result+BTTS / result+totals combos, clean sheets, win-to-nil,
  goals range, teams-to-score, half-time markets, half-time/full-time, highest
  scoring half.
- **Same-game multis** — correlation-aware joint probability + suggested multis
  near a target price.
- **Player props** — club/country split, role re-scaling, predicted/confirmed XI,
  expanded count markets (shots, SOT, tackles, fouls, passes, to-be-fouled,
  to-score-or-assist, GK saves), plus opponent-strength and set-piece accuracy.
- **Team count markets** — corners, cards, shots, SOT, offsides, tackles, fouls
  (negative binomial), team-with-most, both-teams-carded, red card.
- **Data layer** — API-Football ETL (club/country split, lineups, results), The
  Odds API adapter (Bet365 + Pinnacle), predicted lineups, soccerdata enrichment seam.
- **Learning loop** — SQLite ledger, isotonic calibration (PAV), CLV + segment
  trust, recommender ranking by expected value.
- **Backend service** — Flask API, persistence, auto-settlement + closing-line
  capture, profitability reporting; Dockerfile + compose.
- **Frontend** — two-tab terminal previewing everything on sample data; in-app
  log -> settle -> recalibrate.
- **Frontend ↔ backend wiring** — `App` fetches `/api/feed`, `/api/bets`, and
  `/api/performance` from `API_BASE` (falls back to sample data when nothing
  answers); bets and settles persist to the SQLite ledger; Track record shows
  the real performance payload in live mode. Vite dev harness in `frontend/`;
  CORS on the service; the ledger is thread-safe under Flask workers now.
- **Real rate data** — `teamRates` (corners/cards/shots/SOT/offsides/fouls + a
  bounded tackles estimate) averaged from each team's recent finished fixtures
  via `fixtures/statistics` (`datalayer/teamrates.py`); player per-90
  tackles/fouls/fouled/passes/saves aggregated straight from API-Football player
  blocks into the club/country profiles, with None-means-missing so pricing
  falls back to role baselines. Both engines (Python + JSX) prefer measured
  rates. Recent-form goal averages also back-fill xG while the tournament
  season has nothing to average. Verified live against WC2026 data.

- **Derived count markets** — throw-ins / free kicks / goal kicks O/U in both
  engines. No feed exposes these, so they're modelled from measured rates:
  free kicks = fouls + offsides (+ a small extra, by the laws), goal kicks
  track off-target shot volume, throw-ins are a flat documented prior.
- **Predicted-lineup source** — `predicted_from_recent_xis` infers each XI from
  the team's recent *confirmed* starting XIs (`--auto-lineups`; 1 cached call
  per finished form fixture). startProb = smoothed share of recent starts;
  players outside a predicted XI drop to a 0.25 bench default; manual
  `xis.json` still wins; matching is by player id with order-insensitive name
  fallback. The HTML-scraper seam remains for sites with team news.

- **Deployment** — `datalayer/jobs.py` (env-configured scheduled feed builder +
  auto-settle with atomic feed writes and budget-safe defaults), Flask serves
  the built terminal from `STATIC_DIR` (one origin, one container), multi-stage
  Dockerfile (node build -> python image), compose runs api + feedjob on a
  shared `ledger` volume (bets.db, feed.json, API cache) with keys from
  `../.env`. Every piece verified natively (gunicorn + jobs cycle + static
  serving); the compose file itself hasn't been executed — no Docker on the
  dev machine. Postgres deferred until multi-instance is real.

- **Prop prices from CSV** — hand-collected Bet365 slip CSV
  (`backend/props/`) attaches real prices to feed players (`--props-csv` /
  `PROPS_CSV`); initial-aware name matching; the terminal prefills the prop
  odds inputs so edge shows without pasting. Prop exposure is void-aware
  (book props void on no-show, so fair prices are conditional on appearing).
- **Backtesting groundwork** — The Odds API's historical endpoints are paid,
  so the system records its own history: every jobs cycle appends odds+model
  snapshots (`snapshots.py`, with the backend's Poisson goals marginals);
  `python -m datalayer.backtest history.jsonl --key ...` replays them into
  flat-stake P&L/ROI/CLV per edge bucket once results land. Needs accumulated
  cycles before it says anything.
- **Cloud deployment** — single Render web service (`render.yaml` blueprint):
  Docker image serves terminal + API same-origin, feed scheduler runs
  in-process (`ENABLE_FEED_JOB`), optional `ACCESS_CODE` basic-auth gate.
  See `DEPLOY.md` for the push-to-GitHub flow and the free-tier caveats
  (instance sleeps when idle; SQLite resets on redeploys without a paid disk).

- **Model accuracy pass (June 2026)** — Dixon-Coles low-score correction in
  both engines (rho=-0.13); Elo-weighted form goals (`elo.py`, baked snapshot
  + `ELO_JSON` override) so friendly schedules stop inflating xG;
  minutes-weighted club/country blending (sample size earns the say, the
  slider states the preference); void-aware prop exposure.
- **Paper trader** — every feed cycle auto-logs qualifying picks (blend vs
  sharp, `PAPER_EDGE` threshold, 1u flat) into a separate paper ledger,
  settles them with closing prices from the snapshot history, and serves
  `/api/paper/performance`; Track record gets a My bets / Paper trader toggle.
  Exists to feed the calibrator (needs ~50 settled) without staking.
- **Player props in SGMs** — to-score / SOT / shots legs in the builder,
  repriced in the conditional goal environment of the selected match legs
  (positive correlation with Overs captured; conditional independence
  approximation flagged in the UI).
- **Keep-awake** — the service self-pings `RENDER_EXTERNAL_URL` every 10 min
  (`KEEP_AWAKE=0` to disable); 750 free instance-hours cover 24/7, and the
  scheduler keeps recording snapshots instead of sleeping.

## Next (in priority order)

1. **Accumulate snapshot history, then run the backtest** — the evaluator is
   built; it needs days of recorded cycles across many matches to mean
   anything. The paper ledger fills the calibrator on the same timeline.
2. **Persistent ledger in the cloud** — paid Render disk or Postgres, once the
   bet/paper history is worth keeping (it currently resets on redeploys).
3. **Player minutes model** — replace the 0.35 sub-share constant with
   per-player expected minutes from recent appearance patterns.

## Backlog / ideas

- Specials markets from the Bet365 list not yet modelled (team specials, player
  to-be-fouled refinements, headed SOT / SOT-outside-box as distinct player props).
- Half-time models are independent-half approximations; a proper joint half model
  would improve HT/FT and highest-scoring-half.
- Calibration per market family (separate calibrators) once enough settled bets.
- Backtesting harness using historical odds (The Odds API / OddsPapi include history)
  to validate edge before betting live.
- Bankroll management view; staking plan presets.

## Known limitations (carry these forward)

- Team tackles aren't in `fixtures/statistics`; `teamRates.tackles` is estimated
  from fouls (~1.4×, bounded 12–21 per game).
- Form windows early in the tournament are friendlies/qualifiers against mixed
  opposition — count rates inherit that schedule bias (xG no longer does: it
  anchors on market prices or Elo). Prior-shrinkage bounds the damage.
- Player-prop dispersion (PDISP r≈3-4) is tuned by eye to one Bet365 ladder
  CSV; rerun `datalayer.tuneprops` against each new props CSV and adjust.
- No referee-specific card adjustment yet (a referee multiplier hook exists in spirit).
- Opponent strength uses team xG as the single signal; no per-zone or matchup detail.
- Bet365 prop prices aren't in the aggregator feed the way main markets are — props
  show model fair odds; paste the slip price (or wire a prop-odds source) for edge.
