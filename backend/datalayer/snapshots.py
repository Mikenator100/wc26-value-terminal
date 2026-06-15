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

# Dixon-Coles low-score correction: independent Poisson misprices draws and
# low-scoring games; tau reweights the 0-0/1-0/0-1/1-1 cells (rho < 0 adds
# mass to 0-0 and 1-1). Mirrors the JSX engine; rho from the literature.
DC_RHO = -0.13


def _dc_tau(i: int, j: int, lh: float, la: float, rho: float = DC_RHO) -> float:
    if i == 0 and j == 0:
        return 1 - lh * la * rho
    if i == 0 and j == 1:
        return 1 + lh * rho
    if i == 1 and j == 0:
        return 1 + la * rho
    if i == 1 and j == 1:
        return 1 - rho
    return 1.0


def score_matrix(lam_home: float, lam_away: float, max_goals: int = MAX_GOALS) -> list[list[float]]:
    ph = [math.exp(-lam_home) * lam_home ** i / math.factorial(i) for i in range(max_goals + 1)]
    pa = [math.exp(-lam_away) * lam_away ** j / math.factorial(j) for j in range(max_goals + 1)]
    m = [[ph[i] * pa[j] * _dc_tau(i, j, lam_home, lam_away) for j in range(max_goals + 1)]
         for i in range(max_goals + 1)]
    total = sum(sum(row) for row in m) or 1.0
    return [[x / total for x in row] for row in m]


def result_probs(m: list[list[float]]) -> dict[str, float]:
    home = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i > j)
    draw = sum(m[i][i] for i in range(len(m)))
    return {"home": home, "draw": draw, "away": 1 - home - draw}


def total_over_prob(m: list[list[float]], line: float) -> float:
    return sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i + j > line)


def btts_prob(m: list[list[float]]) -> float:
    return sum(m[i][j] for i in range(1, len(m)) for j in range(1, len(m)))


def _no_vig(prices: list[Optional[float]]) -> list[Optional[float]]:
    """Power-method de-vig (mirrors the JSX noVigProbs): raise implied probs
    to k >= 1 until they sum to 1, so the margin lands on the longshots where
    books park it — proportional division overstates longshot probabilities."""
    inv = [(1 / p) if p and p > 1 else None for p in prices]
    present = [x for x in inv if x]
    s = sum(present)
    if not s:
        return [None] * len(prices)
    if s <= 1 or len(present) != len(inv):
        # arbitrage-looking input, or a missing outcome: plain rescale
        return [(x / s) if x else None for x in inv]
    lo, hi = 1.0, 5.0
    for _ in range(60):
        k = (lo + hi) / 2
        if sum(x ** k for x in present) > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    adj = [x ** k for x in inv]
    s2 = sum(adj)
    return [x / s2 for x in adj]


def _parse_line(label: str) -> Optional[float]:
    try:
        return float(label.split()[-1])
    except (ValueError, IndexError):
        return None


def implied_lambdas(markets: list[dict]) -> Optional[tuple[float, float]]:
    """Invert the sharp no-vig prices into the (λ_home, λ_away) the DC score
    matrix reproduces best.

    When Pinnacle has priced a match, the de-vigged 1X2 (+ totals when
    present) is a far better strength estimate than any small-sample form
    average — fitting the lambdas to it makes every derived market (correct
    scores, team totals, ranges) coherent with the sharpest available signal
    instead of amplifying form noise into absurd tail prices.
    """
    target_1x2 = None
    target_ou = None
    for mk in markets or []:
        outs = mk.get("outcomes", [])
        sharp = _no_vig([o.get("pinnacle") for o in outs])
        if mk.get("key") == "1x2" and len(outs) == 3 and all(sharp):
            target_1x2 = sharp
        elif mk.get("key") == "ou25" and len(outs) == 2 and all(sharp):
            line = _parse_line(outs[0].get("label", ""))
            # only trust a totals line near the goal range — thin future-fixture
            # markets sometimes post anomalous lines (e.g. 4.25) that, fitted
            # literally, blow the implied total out to 4.6 goals
            if line is not None and 1.5 <= line <= 3.75:
                target_ou = (line, sharp[0])
    if not target_1x2:
        return None

    # 1X2 alone under-constrains the TOTAL (it mostly fixes the supremacy), so
    # without a usable totals line, softly anchor the total to the competition
    # average rather than letting the search drift to a high-scoring solution
    PRIOR_TOTAL = 2.7

    def loss(lh: float, la: float) -> float:
        m = score_matrix(lh, la)
        rp = result_probs(m)
        err = ((rp["home"] - target_1x2[0]) ** 2
               + (rp["draw"] - target_1x2[1]) ** 2
               + (rp["away"] - target_1x2[2]) ** 2)
        if target_ou:
            line, p_over = target_ou
            err += 2.0 * (total_over_prob(m, line) - p_over) ** 2  # trust a sane line
        else:
            err += 0.01 * ((lh + la) - PRIOR_TOTAL) ** 2           # soft total anchor
        return err

    # coarse-to-fine grid search; the matrix is cheap and this needs no deps
    best, best_err = (1.3, 1.3), float("inf")
    step, lo, hi = 0.15, 0.25, 3.4
    grid = [lo + i * step for i in range(int((hi - lo) / step) + 1)]
    for lh in grid:
        for la in grid:
            e = loss(lh, la)
            if e < best_err:
                best, best_err = (lh, la), e
    for step in (0.05, 0.01):
        bh, ba = best
        cand = [(bh + i * step, ba + j * step) for i in range(-3, 4) for j in range(-3, 4)]
        for lh, la in cand:
            if lh <= 0.1 or la <= 0.1:
                continue
            e = loss(lh, la)
            if e < best_err:
                best, best_err = (lh, la), e
    return round(best[0], 3), round(best[1], 3)


def apply_market_xg(feed: list[dict]) -> int:
    """Replace each match's xG with the market-implied fit where sharp prices
    exist (the form/Elo estimate is kept as xgModelHome/Away for reference)."""
    n = 0
    for match in feed:
        fit = implied_lambdas(match.get("markets") or [])
        if not fit:
            continue
        match["xgModelHome"], match["xgModelAway"] = match.get("xgHome"), match.get("xgAway")
        match["xgHome"], match["xgAway"] = fit
        n += 1
    return n


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
