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

## Next (in priority order)

1. **Wire real rate data** (the critical path). Pull team per-game rates
   (corners/cards/shots/SOT/offsides/tackles/fouls) and player per-90 rates
   (tackles/fouls/passes/saves) from API-Football team stats and FBref (soccerdata),
   and merge into `teamRates` and player profiles in `build.py`. Engines already
   consume these; right now they run on sample/role-baseline values.
2. **Remaining count markets** — throw-ins, free kicks, goal kicks. Same NB engine,
   just need the per-game rates.
3. **Predicted-lineup source** — point `HtmlPredictedLineups` at a real predicted-XI
   site (adapt selectors) or keep the manual `xis.json` path.
4. **Frontend ↔ backend wiring** — externalise `SAMPLE_MATCHES` to a `feed.json`
   fetch; set `API_BASE` so logging persists; show real `/api/performance` in Track
   record instead of the in-memory mock.
5. **Deployment** — host the service + scheduled feed job; mount the ledger volume;
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

- Count-market accuracy is gated on the rate data (item 1 above).
- No referee-specific card adjustment yet (a referee multiplier hook exists in spirit).
- Opponent strength uses team xG as the single signal; no per-zone or matchup detail.
- Bet365 prop prices aren't in the aggregator feed the way main markets are — props
  show model fair odds; paste the slip price (or wire a prop-odds source) for edge.
