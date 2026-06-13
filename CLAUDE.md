# CLAUDE.md

Context for Claude Code. Read this first. It captures what this project is, how
it's built, the decisions behind it, what works, and what's left.

## What this is

A **value-betting analytics tool for the FIFA World Cup 2026**. It pulls odds and
stats, prices every market with its own models, finds where the bookmaker's price
beats fair value, and learns from a logged bet history over time. Personal/research
use. Built across a chat; this repo is the handoff.

It is **not** a tipping service and **not** an attempt to scrape Bet365 directly
(that's against their terms and technically infeasible). It compares Bet365's
retail price against a sharp benchmark (Pinnacle) and the project's own models.

## Repo layout

```
frontend/ValueTerminal.jsx   Single-file React app (the "terminal" UI). All models
                             run client-side on a feed; renders Markets + Track record.
backend/                     Python package `datalayer` — the ETL + models + API.
  datalayer/
    build.py                 CLI: builds feed.json (stats + odds + lineups). Entry point.
    providers.py             API-Football client (cache, throttle, paging, results).
    normalize.py             per-90, role inference, club/country split, team xG (λ).
    odds.py                  The Odds API -> markets shape; merge into feed.
    lineups.py               Predicted XIs (formation->roles; manual JSON + HTML scraper seam).
    countmarkets.py          Negative-binomial count engine (corners/cards/shots/player props)
                             + player_expectations (opponent strength + set-piece flags).
    enrich_soccerdata.py     FBref/SofaScore/FotMob enrichment seam (optional).
    settler.py               Auto-settlement from results + closing-price (CLV) capture.
    service.py               Flask API: persist ledger, serve feed, settle, performance.
    betlog/                  The learning loop:
      ledger.py              SQLite bet ledger (record/settle).
      metrics.py             ROI/CLV/Brier + isotonic calibration (PAV) + segment stats.
      recommender.py         Calibration + segment-trust ranking; expected value per pick.
  tests/                     Pure-function + API tests (no network/key needed).
  Dockerfile, docker-compose.yml, requirements.txt, README.md
docs/ARCHITECTURE.md         Module map, data flow, the feed JSON contract, the math.
docs/ROADMAP.md              Done / in-progress / next, with priorities.
```

## Commands (run from `backend/`)

```bash
pip install -r requirements.txt

# build the feed (stats only)
python -m datalayer.build --key "$API_FOOTBALL_KEY" --league 1 --season 2026 --max-matches 8 --out feed.json
# + live Bet365/Pinnacle odds, predicted lineups (inferred from recent XIs;
#   --lineups-json xis.json supplies manual XIs that win), and hand-collected
#   Bet365 prices from a CSV (backend/props/). Two CSV formats, auto-sniffed:
#   ladder-style player props (csvprops.py) or the full structured one-match
#   export (csvbook.py: every player + match markets like alternative totals,
#   3-way corners, ranges, combos -> player bookOdds + match bookPrices;
#   slip players missing from the API roster get placeholder profiles under
#   the terminal's "From slip" tab). Per-match workflow: overwrite
#   backend/props/bet365_structured_markets.csv with the new match's export
#   and push — once its match leaves the feed the file goes stale and is
#   ignored automatically (no cleanup needed beyond replacing it).
python -m datalayer.build --key "$API_FOOTBALL_KEY" --odds-key "$THE_ODDS_API_KEY" --auto-lineups \
  --squad-limit 26 --props-csv props/bet365_structured_markets.csv --out feed.json

# run the API (persistent ledger + feed + auto-settle); set STATIC_DIR to the
# built frontend (frontend/dist) to serve the terminal from the same origin
gunicorn 'datalayer.service:app'            # :8000
# or: python -m datalayer.service

# the scheduled loop: rebuild feed + auto-settle every FEED_INTERVAL seconds
# (env-configured, budget-safe defaults — see datalayer/jobs.py; RUN_ONCE=1 for cron)
python -m datalayer.jobs

# everything together: terminal + API + scheduled feed job + persistent volume
# (keys read from ../.env)
docker compose up --build

# backtest recorded snapshots (jobs writes history.jsonl every cycle)
python -m datalayer.backtest history.jsonl --key "$API_FOOTBALL_KEY"
```

Cloud: one Render web service via the root `render.yaml` blueprint — see
`DEPLOY.md` (free tier: sleeps when idle, SQLite resets on redeploys; optional
`ACCESS_CODE` env turns on a basic-auth gate).

```bash

# tests (all pass, no network needed)
for t in tests/*.py; do python "$t"; done
```

Frontend: `ValueTerminal.jsx` (default export `App`) plus a minimal Vite harness
(`frontend/package.json`, `index.html`, `main.jsx`):

```bash
cd frontend && npm install && npm run dev    # http://localhost:5173
```

On mount it fetches the feed, ledger, and performance from `API_BASE`
(module-level const, default `http://localhost:8000` = the local
datalayer.service) and falls back to baked-in `SAMPLE_MATCHES` when nothing
answers, so the preview always renders. When live: "Log bet" POSTs to the
persistent ledger, settles POST back, and Track record reads
`/api/performance` instead of the in-memory sample history.

## Architecture in one paragraph

The **backend** builds a `feed.json` (one object per match: teams, expected goals,
players with club/country splits and roles, and live odds) and exposes a Flask API
that persists a bet ledger and auto-settles finished fixtures. The **frontend**
reads the feed and runs all the pricing models client-side, rendering value bars,
an SGM builder, the full market catalog, player props, and a Track-record
dashboard. The **learning loop** logs each recommended bet, settles it with the
closing price, recalibrates the model's probabilities, and learns which market
segments have real edge. See `docs/ARCHITECTURE.md` for the data flow and the
exact feed JSON contract (the integration boundary between the two halves).

## Modeling core (the math)

- **Goals markets** (1X2, totals, BTTS, correct score, combos, margins, HT/FT,
  etc.) are exact sums over a **Poisson score matrix** built from each team's
  expected goals, with the **Dixon-Coles low-score correction** (rho=-0.13;
  independent Poisson misprices draws). Every such market is derivable with no
  extra data.
- **xG hierarchy** (strongest signal wins): (1) **market-implied** — when
  Pinnacle has priced the match, `snapshots.implied_lambdas` inverts the
  de-vigged 1X2+totals into the lambdas, so every derived market stays
  coherent with the sharp line; (2) **Elo matchup anchor** — and the Elo is
  **live**: each jobs cycle re-derives ratings from the baseline + every
  finished WC fixture (`elo.apply_results`, K=50, margin multiplier,
  idempotent, written to `ELO_STATE`)
  (`elo.elo_lambdas`: ~220 rating points ≈ 1 goal around a 2.6-goal total)
  tilted ±18% by Elo-weighted recent form — raw form averages are never
  used directly, qualifier blowouts vs minnows read like 4 goals/game even
  after discounting; (3) season stats / form averages only for unrated teams.
  Pre-fit estimates are kept as `xgModelHome/Away` in the feed.
- **Same-game multis**: hit rate is the **joint** probability from the score matrix
  (correlation captured exactly) — never the product of leg odds. Player legs
  (to score / SOT / shots) are repriced in the conditional goal environment the
  match legs imply, then multiplied in (conditional independence, flagged as
  an approximation in the UI).
- **Count markets** (corners, cards, shots, SOT, offsides, tackles, fouls; player
  shots/SOT/tackles/fouls/passes/saves) use a **negative binomial** distribution,
  because these counts are overdispersed and Poisson underprices the tails.
  **Player dispersion is tuned to real Bet365 ladder shapes** (r≈3-4 — their
  prices decay near-geometrically; `PDISP`/JSX `PD`, kept in sync); team-level
  counts keep tighter r. Small samples shrink: team rates toward
  tournament-typical priors (4 pseudo-games, `teamrates.PRIORS`), player
  per-90s toward role baselines (270-minute half-trust, `grounded()` in both
  engines). `python -m datalayer.tuneprops feed.json` reports model-vs-book
  bias per prop family whenever a props CSV is matched — the tuning loop.
- **Player props**: blend club + country per-90 rates (kept separate on purpose)
  with the blend **shrunk toward the better-sampled source** (minutes-weighted;
  the country-weight slider states a preference, the data earns its say),
  rescale to the player's match position (rescale factor bounded [0.6, 1.6]),
  scale by **void-aware exposure measured from recent minutes** (book props
  void on no-show; a 'comes off on 60' starter or a 15-minute super-sub no
  longer prices like a 90-minute anchor). Rates use **predictive grounding**
  (Poisson-Gamma: role baseline is the prior, prior strength = minutes to
  expect ~3 events, so thin zero-count samples stop reading as zero ability)
  and **scoring is shot-based** (SOT-rate × finishing shrunk to role
  conversion, 30% direct-goals mix — goals are the noisiest stat). A
  **team-mass normalisation** pass scales each squad's goals/assists/shots
  to the match's (market-fitted) budget.
- **Prop accuracy**: attacking output scales with the team's match xG vs baseline
  (opponent defence is baked into xG); defensive/discipline output scales with the
  opponent's attack; `pen`/`fk` flags add set-piece contribution to the taker.
- **Learning loop**: isotonic regression (PAV) recalibrates stated probabilities to
  realised outcomes; segment trust is driven by **CLV** (closing line value) and
  shrinks toward neutral on small samples; the recommender ranks by adjusted edge
  and attaches an expected value (profit) to each pick. A **paper trader**
  auto-logs every qualifying pick each feed cycle (1u flat, separate paper.db,
  `PAPER_EDGE` threshold) so calibration accumulates settled volume without
  staking; Track record has a My bets / Paper trader toggle.

## Decisions & conventions (the "why")

- **"Fair odds" = sharp no-vig (Pinnacle) blended with the model.** Value = the
  best available AU sportsbook price above fair (the `bet365` field now carries
  that best price; `bestBook` names the book). Pinnacle/Betfair are the truth
  proxy, not any retail line.
  De-vigging uses the **power method** (margin lands on longshots, where books
  park it) — proportional division overstates longshot probabilities. Both
  engines (JSX `noVigProbs`, `snapshots._no_vig`) stay in sync.
- **Neutral venue**: `team_lambdas` applies no home advantage by default (it's
  a World Cup) — except host-nation matches (USA/Mexico/Canada get ~1.08 on
  goals via `build._venue_advantage`).
- **Profitability and CLV are the objective, never hit rate.** A high hit rate at
  short odds is usually a losing bet. The recommender optimises expected value;
  performance reports P&L/ROI/CLV first.
- **Keep the frontend JS engine and the Python engine in sync.** The same models
  exist in both (`ValueTerminal.jsx` for preview, `datalayer/countmarkets.py` etc.
  for the real feed). If you change pricing logic, change both and re-run tests.
- **Tests are pure-function and run without keys/network.** Keep them that way;
  mock external providers.
- **Club vs country stats stay separate** — never blend them into one total. The
  whole player model depends on this split.

## Data sources & env vars

- `API_FOOTBALL_KEY` — API-Football (league=1, season=2026). Fixtures, lineups
  (confirmed ~40 min pre-KO), results, and the **club/country player split**.
- `THE_ODDS_API_KEY` — The Odds API. Regions `eu,au`: Pinnacle (sharp anchor,
  eu) + the **best price across AU fixed-odds sportsbooks** (SportsBet/TAB/
  Neds/Ladbrokes/PointsBet/bet365 AU/...; exchanges excluded for commission)
  for h2h/totals/btts. Each outcome carries `bet365` (= best retail price you'd
  actually bet) and `bestBook` (which book offers it).
- `soccerdata` (optional, no key) — FBref/SofaScore/FotMob enrichment. FBref is the
  per-90 backbone; SofaScore/FotMob hit internal endpoints (ToS-grey, fragile).

## Current status

Working & tested: all model layers (goals, SGMs, count markets, player props),
opponent-strength + set-piece accuracy, the backend ETL, live odds adapter,
predicted lineups, the betlog (calibration/CLV/segment trust), auto-settlement,
and the Flask service with persistence. The frontend previews everything on sample
data. See `docs/ROADMAP.md`.

## Real rate data (done — June 2026)

Count markets and player props now run on real rates, verified live against
WC2026 data:

- **Team rates** (`datalayer/teamrates.py`): per-game corners/cards/shots/SOT/
  offsides/fouls/redProb averaged from each team's recent finished fixtures via
  `fixtures/statistics` (the `teams/statistics` endpoint has no corners/shots).
  Attached to each match as `teamRates` by `build.py`; `--team-form N` controls
  the window (0 disables — each finished fixture costs 1 API call uncached,
  cached a month after). Team tackles aren't published; estimated from fouls
  (~1.4×, bounded 12–21).
- **Player per-90 counts**: API-Football player blocks already carry tackles/
  fouls/fouled/passes/saves — `normalize.py` aggregates them into the club and
  country blocks. A raw count of 0 over real minutes means "not recorded", so it
  becomes `None` and pricing falls back to role baselines (`measured()` in both
  `countmarkets.player_expectations` and the JSX `priceProps` — the engines stay
  in sync).
- **xG early-tournament fallback**: the WC-season stats average over 0 games at
  kickoff of the tournament, so `build.py` back-fills goals for/against from the
  same recent-form window.
- `--squad-limit` joins `--team-form` as the API-budget levers (each player
  costs ~4 calls uncached).

## Constraints & honest caveats (important)

- **Do not build a Bet365 scraper.** ToS + aggressive anti-bot. Use the aggregator.
- **SofaScore/FotMob have no public API**; `soccerdata` uses internal endpoints —
  cache hard, go slow, treat as fragile. Fine for personal use; for anything public,
  prefer a paid feed.
- **Bet365 restricts accounts that consistently bet value.** The edges are real but
  account longevity is a separate, unsolved problem.
- **Betting returns are extremely noisy.** Hundreds of bets prove little; long
  drawdowns happen with genuine edge; most models don't beat the margin. The betlog
  is built to tell the truth about this — calibration stays off below 50 settled
  bets and segment trust shrinks below ~25 on purpose, to avoid overfitting a tiny
  history. Do **not** "train ML to predict winners" on the bet log; it will overfit.
- **Free-tier limits**: API-Football ~100 calls/day; The Odds API cost = markets ×
  regions per call. Caching is not optional.
- **Frontend artifacts can't persist** (no localStorage) — that's why the backend
  service exists for the real ledger.
- Surface responsible-gambling context (18+, analysis only) in any user-facing build.
