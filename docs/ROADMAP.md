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

## Next (in priority order)

1. **Remaining count markets** — throw-ins, free kicks, goal kicks. Same NB engine,
   just need the per-game rates.
2. **Predicted-lineup source** — point `HtmlPredictedLineups` at a real predicted-XI
   site (adapt selectors) or keep the manual `xis.json` path.
3. **Deployment** — host the service + scheduled feed job; mount the ledger volume;
   move to Postgres if running multi-instance.

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
  opposition — rates and the xG fallback inherit that schedule bias.
- No referee-specific card adjustment yet (a referee multiplier hook exists in spirit).
- Opponent strength uses team xG as the single signal; no per-zone or matchup detail.
- Bet365 prop prices aren't in the aggregator feed the way main markets are — props
  show model fair odds; paste the slip price (or wire a prop-odds source) for edge.
