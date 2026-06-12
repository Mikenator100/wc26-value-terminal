"""Odds + model-probability snapshots, appended each feed cycle.

The Odds API's historical endpoints are paid-plan only, so the backtest data
source is the system itself: every jobs cycle appends one JSONL line per priced
outcome (bet365 price, pinnacle price, sharp no-vig prob, model prob). The last
snapshot before kickoff doubles as the closing line. `datalayer.backtest`
replays the file once results are known.

This module also carries the small Python Poisson goals engine the snapshots
need (1X2 / totals / BTTS marginals from the same score matrix the terminal
uses) — kept minimal on purpose; the frontend remains the full market catalog.
"""

from __future__ import annotations

import json
import math
import time
from typing import Optional

MAX_GOALS = 10


def score_matrix(lam_home: float, lam_away: float, max_goals: int = MAX_GOALS) -> list[list[float]]:
    ph = [math.exp(-lam_home) * lam_home ** i / math.factorial(i) for i in range(max_goals + 1)]
    pa = [math.exp(-lam_away) * lam_away ** j / math.factorial(j) for j in range(max_goals + 1)]
    return [[ph[i] * pa[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]


def result_probs(m: list[list[float]]) -> dict[str, float]:
    home = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i > j)
    draw = sum(m[i][i] for i in range(len(m)))
    return {"home": home, "draw": draw, "away": 1 - home - draw}


def total_over_prob(m: list[list[float]], line: float) -> float:
    return sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i + j > line)


def btts_prob(m: list[list[float]]) -> float:
    return sum(m[i][j] for i in range(1, len(m)) for j in range(1, len(m)))


def _no_vig(prices: list[Optional[float]]) -> list[Optional[float]]:
    inv = [(1 / p) if p and p > 1 else None for p in prices]
    s = sum(x for x in inv if x)
    if not s:
        return [None] * len(prices)
    return [(x / s) if x else None for x in inv]


def _parse_line(label: str) -> Optional[float]:
    try:
        return float(label.split()[-1])
    except (ValueError, IndexError):
        return None


def snapshot_feed(feed: list[dict], ts: Optional[float] = None) -> list[dict]:
    """One row per priced outcome across the feed's merged markets."""
    ts = ts or time.time()
    rows: list[dict] = []
    for match in feed:
        markets = match.get("markets") or []
        if not markets:
            continue
        m = score_matrix(match.get("xgHome", 1.3), match.get("xgAway", 1.3))
        rp = result_probs(m)
        for mk in markets:
            outcomes = mk.get("outcomes", [])
            sharp = _no_vig([o.get("pinnacle") for o in outcomes])
            for idx, o in enumerate(outcomes):
                key, label = mk.get("key"), o.get("label", "")
                line = None
                if key == "1x2":
                    outcome = ("home", "draw", "away")[idx] if idx < 3 else None
                    model = rp.get(outcome) if outcome else None
                elif key == "ou25":
                    over = label.lower().startswith("over")
                    outcome = "over" if over else "under"
                    line = _parse_line(label)
                    if line is None:
                        continue
                    p_over = total_over_prob(m, line)
                    model = p_over if over else 1 - p_over
                elif key == "btts":
                    outcome = "yes" if label.lower() == "yes" else "no"
                    p_yes = btts_prob(m)
                    model = p_yes if outcome == "yes" else 1 - p_yes
                else:
                    continue
                rows.append({
                    "ts": round(ts, 1),
                    "match_id": match.get("id"),
                    "kickoff": match.get("kickoff"),
                    "market": key,
                    "outcome": outcome,
                    "label": label,
                    "line": line,
                    "bet365": o.get("bet365"),
                    "pinnacle": o.get("pinnacle"),
                    "sharp_prob": round(sharp[idx], 5) if sharp[idx] else None,
                    "model_prob": round(model, 5) if model is not None else None,
                })
    return rows


def append_snapshots(path: str, feed: list[dict], ts: Optional[float] = None) -> int:
    rows = snapshot_feed(feed, ts)
    if rows:
        with open(path, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return len(rows)
