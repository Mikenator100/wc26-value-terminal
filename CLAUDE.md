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
# + live Bet365/Pinnacle odds and predicted lineups
python -m datalayer.build --key "$API_FOOTBALL_KEY" --odds-key "$THE_ODDS_API_KEY" --lineups-json xis.json --out feed.json

# run the API (persistent ledger + feed + auto-settle)
gunicorn 'datalayer.service:app'            # :8000
# or: python -m datalayer.service

# everything together with persistence + scheduled feed + auto-settle
API_FOOTBALL_KEY=... ODDS_API_KEY=... docker compose up --build

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
  expected goals. Every such market is derivable with no extra data.
- **Same-game multis**: hit rate is the **joint** probability from the score matrix
  (correlation captured exactly) — never the product of leg odds.
- **Count markets** (corners, cards, shots, SOT, offsides, tackles, fouls; player
  shots/SOT/tackles/fouls/passes/saves) use a **negative binomial** distribution,
  because these counts are overdispersed and Poisson underprices the tails.
- **Player props**: blend club + country per-90 rates (kept separate on purpose),
  rescale to the player's match position, scale by start probability.
- **Prop accuracy**: attacking output scales with the team's match xG vs baseline
  (opponent defence is baked into xG); defensive/discipline output scales with the
  opponent's attack; `pen`/`fk` flags add set-piece contribution to the taker.
- **Learning loop**: isotonic regression (PAV) recalibrates stated probabilities to
  realised outcomes; segment trust is driven by **CLV** (closing line value) and
  shrinks toward neutral on small samples; the recommender ranks by adjusted edge
  and attaches an expected value (profit) to each pick.

## Decisions & conventions (the "why")

- **"Fair odds" = sharp no-vig (Pinnacle) blended with the model.** Value = Bet365
  price above fair. Pinnacle/Betfair are the truth proxy, not Bet365's own line.
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
- `THE_ODDS_API_KEY` — The Odds API. Bet365 + Pinnacle (+ Betfair fallback) for
  h2h/totals/btts.
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
