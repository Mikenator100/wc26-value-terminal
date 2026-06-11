# WC26 Value Terminal

Value-betting analytics for the FIFA World Cup 2026: pull odds + stats, price every
market with purpose-built models, surface where Bet365's price beats fair value, and
learn from a logged bet history over time.

**Start here:** [`CLAUDE.md`](./CLAUDE.md) — full context, commands, decisions, and
current state. Then [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for the module
map and the feed JSON contract, and [`docs/ROADMAP.md`](./docs/ROADMAP.md) for what's
next.

## Quick start

```bash
# backend
cd backend
pip install -r requirements.txt
python -m datalayer.build --key "$API_FOOTBALL_KEY" --odds-key "$THE_ODDS_API_KEY" --out feed.json
gunicorn 'datalayer.service:app'        # http://localhost:8000
for t in tests/*.py; do python "$t"; done   # all pass, no key/network needed

# frontend
# frontend/ValueTerminal.jsx is a single React component (default export App).
# Runs on built-in sample data; point it at feed.json and set API_BASE to go live.
```

## Layout

```
frontend/   single-file React terminal UI
backend/    Python `datalayer` package — ETL, pricing models, learning loop, Flask API
docs/       architecture + roadmap
CLAUDE.md   primary context file (read first)
```

For analysis only. 18+. Gamble responsibly.
