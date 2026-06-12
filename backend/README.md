# World Cup 2026 — Data Layer

ETL that turns raw football data into the exact JSON the Value Terminal renders.
It produces, per fixture: expected goals for the Poisson model, and every
player's **club vs country** per-90 splits with role and lineup status.

## Why this exists

The terminal's models are only as good as their inputs. The hard requirement is
keeping a player's **club** form and **international-only** form *separate* —
most free stat sources hand you a blended total. API-Football is the practical
source that returns one statistics record per team a player featured for, so the
split is real, not estimated.

## What it outputs

One object per match, drop-in for `ValueTerminal.jsx`:

```jsonc
{
  "id": "999", "home": "Brazil", "away": "Morocco",
  "group": "Group C", "kickoff": "2026-06-20T18:00:00+00:00", "live": false,
  "xgHome": 1.92, "xgAway": 0.62,          // -> Poisson score matrix
  "markets": [],                            // filled by the odds adapter
  "players": [
    {
      "name": "Test Striker", "team": "Brazil",
      "clubRole": "ST", "countryRole": "ST",
      "predictedPos": "ST", "confirmedPos": "ST",
      "startProb": 0.85, "confirmedIn": false,
      "club":    { "shots": 3.32, "sot": 1.39, "goals": 0.76, "assists": 0.24, "cards": 0.13 },
      "country": { "shots": 2.53, "sot": 0.98, "goals": 0.42, "assists": 0.28, "cards": 0.13 }
    }
  ]
}
```

`cards` is a per-match booking probability (Poisson P(≥1 yellow)), matching how
the terminal treats it. All other rates are per 90 minutes.

## Setup

```bash
pip install -r requirements.txt
```

Get a key from dashboard.api-football.com (free tier ≈ 100 calls/day) or via
RapidAPI. World Cup data lives at `league=1, season=2026`.

## Run

```bash
python -m datalayer.build --key YOUR_KEY --season 2026 --max-matches 8 --out feed.json
# RapidAPI subscription instead of direct:
python -m datalayer.build --key YOUR_KEY --mode rapidapi --out feed.json
```

Then have the React app fetch `feed.json` (replace `fetchMatchOdds()` /
`SAMPLE_MATCHES`). Run it on a schedule (cron / serverless) — every few hours
pre-match, and every ~5 min in the hour before kickoff to catch confirmed XIs.

## How the pieces map to the model

| Output            | Source                                   | Used by                         |
|-------------------|------------------------------------------|---------------------------------|
| `xgHome/xgAway`   | team goals-for/against vs comp average, or provider prediction | Poisson score matrix → 1X2, O/U, BTTS, SGMs |
| `club` / `country`| `players` endpoint, split by team, aggregated over seasons | player-prop engine               |
| `clubRole/countryRole` | position + lineup grid + output     | role re-scaling for props        |
| `confirmedIn`, `confirmedPos`, `startProb` | `fixtures/lineups` when released | predicted → confirmed re-pricing |

## The two known seams

1. **Predicted XIs.** API-Football only publishes lineups once official
   (~40 min pre-KO). Until then `startProb`/`predictedPos` fall back to each
   player's modal position. To get true predicted XIs earlier, pass a
   `predicted_lineups` dict into `build_match()` from a scraped source
   (e.g. a predicted-lineup site) — the seam is already wired.

2. **Odds.** `markets` is left empty here; the live odds adapter (Bet365 +
   Pinnacle via an aggregator) fills it. Keeping odds and stats in separate
   feeds means either can refresh on its own cadence.

## Optional: free enrichment

For richer club numbers (shot quality, real xG) without paying, `soccerdata`
scrapes FBref and Understat into pandas. Add an adapter implementing the same
`Provider` methods and merge per-90 fields in `normalize._rates`. Note FBref's
terms restrict heavy scraping — cache aggressively and keep request volume low.

## Tests

```bash
python tests/test_pipeline.py
```

Covers per-90 maths, the club/country split, role inference, expected-goals
derivation, and an end-to-end `build_match` with a fake provider — no key or
network needed.

---

# Learning loop (`datalayer.betlog`)

Records every recommended bet, settles it with the result **and the closing
price**, then improves future recommendations from that history. Tested
end-to-end in `tests/test_betlog.py` (no key/network needed).

## Why not "best hit rate"

Hit rate and profit are different. A 1.20 favourite hits ~83% and is often a
losing bet; a 5.00 longshot hits 20% and can be profitable. Optimising for hit
rate steers you into low-odds negative-value bets. The system instead learns:

- **Calibration** — when the model says 40%, do those bets win 40%? Isotonic
  regression (PAV) on settled results maps stated probabilities to realised
  ones, correcting over/under-confidence.
- **Edge, via CLV** — closing line value (did your price beat the market's final
  price?) is the fastest honest signal of real edge; you get a read after dozens
  of bets instead of the thousands ROI needs to clear the noise.

## Loop

```python
from datalayer.betlog import Ledger, Recommender, Candidate, performance

led = Ledger("bets.db")
bid = led.record(match_id="wc26-bra-mar", market="ou25", selection="Over 2.5",
                 model_prob=0.58, price=2.05, stake=5.0, bankroll=200.0)
# ...after the match:
led.settle(bid, "won", closing_price=1.98)   # took 2.05 vs 1.98 close -> +CLV

print(performance(led.settled()))            # roi, hit_rate, avg_clv, brier, log_loss

rec = Recommender(led.settled(), kelly_fraction=0.25)
picks = rec.recommend([Candidate("wc26-arg-cro","1x2","Argentina",0.62,1.70)], bankroll=200)
```

`Recommender` applies two corrections on top of the raw model: it **calibrates**
each probability, and it scales edge by **segment trust** (how reliable the
model has been in that market / odds band, driven by CLV, shrunk toward neutral
when the sample is thin). It ranks by *adjusted edge*, not hit rate, and stakes
via fractional Kelly on the calibrated probability.

## Honest limits (read these)

- **Sample size.** Betting P&L is extremely noisy. Hundreds of bets prove little;
  expect long losing streaks even with genuine edge. Calibration stays off until
  50+ settled bets, and segment trust shrinks toward neutral below ~25 — both on
  purpose, to stop the system overfitting a tiny history.
- **CLV needs honest closing prices.** Log the actual closing line; without it
  you lose the best early signal and fall back to slow, noisy ROI.
- **This is not "train ML to predict winners".** Settled-bet counts are far too
  small for that and it would overfit instantly. The model's edge comes from the
  match/player models; the loop's job is to *correct and gate* them, not replace
  them.

# Data sources

| Source        | Access                    | Best for                                  |
|---------------|---------------------------|-------------------------------------------|
| API-Football  | paid API (key)            | fixtures, lineups, **clean club/country split** |
| FBref         | `soccerdata` (scraper)    | high-quality per-90 standard/shooting stats, intl coverage |
| SofaScore     | `soccerdata` (scraper)    | per-90 + player ratings, corroboration    |
| FotMob        | `soccerdata` (scraper)    | per-90 + ratings, broad coverage          |

`datalayer/enrich_soccerdata.py` is the FBref enrichment seam: pull richer
per-90 numbers and overlay them onto the `club` block while keeping
API-Football's club/country split. SofaScore/FotMob have no public API —
`soccerdata` hits internal endpoints (ToS grey, fragile); cache hard, go slow,
and prefer a paid feed for anything beyond personal use.

# Live odds (`datalayer.odds`)

Fills each match's `markets` with **Bet365** (the price you'd bet) and
**Pinnacle** (the sharp line the no-vig fair price is built from), via The Odds
API v4. One fetch returns every bookmaker for every fixture.

```bash
# build stats feed AND merge live odds in one go:
python -m datalayer.build --key API_FOOTBALL_KEY --odds-key THE_ODDS_API_KEY --out feed.json
```

What it does:
- pulls `h2h` (1X2), `totals` (over/under), `btts` from `soccer_fifa_world_cup`
  for `bet365` + `pinnacle` (+ `betfair_ex_eu` as a sharp fallback);
- normalises to the terminal's `markets` shape, ordered exactly as the engine
  expects (1X2 = home/draw/away, totals locked to the 2.5 line, BTTS = yes/no);
- merges into the feed by team name (orientation-independent);
- caches for 120s and surfaces `x-requests-remaining` so you stay in quota.

Once `markets` is populated, the terminal's value/edge columns and the betlog
recommender run on **real prices** instead of samples — point the React app's
`fetchMatchOdds()` at `feed.json`. Tested in `tests/test_odds.py` (no key needed).

Notes: free tier is quota-limited (cost = markets × regions per call; empty
responses are free), so cache and only refresh near kickoff. Bet365 prices via
aggregators carry a short delay, and Bet365 restricts accounts that consistently
bet value — the edges are real but account longevity is a separate problem.

# Predicted lineups (`datalayer.lineups`)

The pre-kickoff counterpart to API-Football's confirmed XIs (which only publish
~40 min out). Produces the `predicted_lineups` dict `build_match` already accepts.

```bash
python -m datalayer.build --key KEY --odds-key ODDS --lineups-json xis.json --out feed.json
```

`xis.json` (the reliable manual path — fill from any predicted-XI source):

```json
{
  "Brazil":  {"formation": "4-2-3-1",
              "starters": ["Alisson","Danilo","Marquinhos","Gabriel","Wendell",
                           "Casemiro","Bruno","Raphinha","Paqueta","Vinicius","Richarlison"],
              "doubtful": ["Paqueta"]},
  "Morocco": {"formation": "4-3-3", "starters": ["..."], "doubtful": []}
}
```

The formation string maps each ordered slot to a role (`formation_to_roles`),
which flows straight into the prop engine's position re-scaling — so a player
predicted out of position is priced in that position before the XI is confirmed.
Doubtful players are discounted to a 0.55 start probability. `HtmlPredictedLineups`
is a scraper seam (configurable CSS selectors, needs `beautifulsoup4`) for a
predicted-XI site — adapt the selectors to your source; the tested core is the
formation mapping. Tested in `tests/test_lineups.py`.

# Deployment & persistence (`datalayer.service`)

A Flask API turns the tool into a persistent, deployable app. The SQLite ledger
lives on a mounted volume, so logged bets survive restarts and deploys.

Endpoints:
- `GET  /api/feed` — latest feed.json (stats + odds + lineups)
- `POST /api/bets` — log a bet (the React "Log bet" button calls this)
- `GET  /api/bets` — full ledger
- `POST /api/bets/<id>/settle` — manual settle `{result, closing_price?}`
- `POST /api/settle/auto` — settle finished fixtures from results + capture CLV
- `GET  /api/performance` — **profitability and hit rate together**, plus
  calibration table and segment trust

## Run locally
```bash
pip install -r requirements.txt
python -m datalayer.build --key $API_FOOTBALL_KEY --odds-key $ODDS_API_KEY --auto-lineups --out feed.json
gunicorn 'datalayer.service:app'        # serves on :8000
# set STATIC_DIR=../frontend/dist (after `npm run build` there) to serve the
# terminal from the same origin as the API
# scheduled loop (feed rebuild + auto-settle; env-configured, RUN_ONCE=1 for cron):
python -m datalayer.jobs
```

## Docker (terminal + API + scheduled feed + persistent volume)
```bash
docker compose up --build               # keys read from ../.env
```
The image is multi-stage: node builds `frontend/dist`, the python image serves
it at `/` next to the API. The `feedjob` container rebuilds the feed every
`FEED_INTERVAL` (default 30 min, atomic write, API cache on the volume) and
POSTs to `/api/settle/auto`, which reads finished-fixture results, resolves
each open bet, settles it, and records the closing price so CLV is captured
automatically. Budget knobs: `MAX_MATCHES`, `TEAM_FORM`, `SQUAD_LIMIT`,
`AUTO_LINEUPS` (see `datalayer/jobs.py`).

Hosting: any container host works (Fly.io, Render, Railway, a small VPS). Keep
the volume attached for the ledger. For serverless/multi-instance, swap SQLite
for Postgres (the `Ledger` class is the only thing that needs changing).

## Auto-settlement & CLV (`datalayer.settler`)
`resolve_outcome` settles score-based markets (1X2, totals, BTTS, double chance)
and anytime-goalscorer (from goal events) automatically; markets it doesn't
understand (corners, cards, shots) stay open for manual settling until those
models exist. Closing price is captured at settlement so `avg_clv` and
`pct_positive_clv` in `/api/performance` are real, not estimated.

## Profitability vs hit rate
`/api/performance` reports profitability (P&L, ROI, CLV) first and on equal
footing with hit rate, and the recommender now attaches an `expected_value`
(expected profit) to every pick and ranks by adjusted edge — i.e. it optimises
for profit, never for hit rate alone.

# Count markets (`datalayer.countmarkets`)

The Bet365 markets that aren't derivable from goals — corners, cards, shots,
shots on target, offsides, tackles, fouls (team), and shots/SOT/tackles/fouls/
passes/saves (player). Mirrors the frontend engine.

Counts are overdispersed, so the default distribution is **negative binomial**
(variance = μ + μ²/r); Poisson underprices the tails (e.g. Over 13.5 corners:
15.1% Poisson vs 21.6% NB). `team_markets(rates)` and `player_markets(exp)`
return priced markets (over/under lines, team-with-most, both-teams-to-be-carded,
red card, etc.).

What's needed to make these live: the **rate data**. Team per-game rates
(corners/cards/shots/…) and player per-90 rates (tackles/fouls/passes/saves)
come from API-Football team statistics and FBref (via the soccerdata seam).
Wiring those rates into the player/team profiles in `build.py` is the remaining
ETL step — the pricing engine itself is done and tested. Until then the frontend
prices these off role-baseline and sample rates so you can see every market.

Still outstanding from the Bet365 list: throw-ins, free kicks, goal kicks (same
engine, just need their rates), and **prop accuracy** (opponent-strength
adjustment + set-piece/penalty-taker flags) — the next build.

# Prop accuracy (opponent strength + set pieces)

Player and team count markets now adjust for context:

- **Opponent strength** — attacking output (goals, shots, SOT, assists, corners)
  scales with the team's match xG vs the tournament baseline (the opponent's
  defence is already baked into that xG); defensive/discipline output (tackles,
  fouls, cards) scales with the opponent's attack. Same striker prices at fair
  1.85 to score vs a weak defence but 3.53 vs a strong one.
- **Set pieces** — `pen`/`fk` flags add the penalty and direct-free-kick
  contribution to the taker's goal/shot/SOT numbers (penalty duty pulls the
  example striker to fair 1.59). Players carry these flags; the UI shows PEN/FK
  badges.

`countmarkets.player_expectations(profile, pos, start_prob, team_xg, opp_xg,
set_pieces)` applies all of this and feeds `player_markets`. Tested in
`tests/test_countmarkets.py`.
