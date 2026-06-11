"""Performance measurement and probability calibration.

Two jobs:
  - score the ledger honestly (ROI, CLV, Brier, log loss, calibration table)
  - learn a calibration map from settled results via isotonic regression (PAV),
    so the model's stated probabilities get corrected toward reality over time.

Pure Python / stdlib only — no sklearn, so it runs anywhere.
"""

from __future__ import annotations

import math
from typing import Callable

from .ledger import odds_band


def _won(b: dict) -> int:
    return 1 if b["status"] == "won" else 0


# --------------------------------------------------------------------------- #
# headline metrics
# --------------------------------------------------------------------------- #
def performance(bets: list[dict]) -> dict:
    if not bets:
        return {"n": 0}
    staked = sum(b["stake"] for b in bets)
    pnl = sum(b["pnl"] or 0 for b in bets)
    wins = sum(_won(b) for b in bets)

    # CLV: did the price we took beat the closing price?
    clv_vals = [
        b["price"] / b["closing_price"] - 1
        for b in bets
        if b.get("closing_price")
    ]
    avg_clv = sum(clv_vals) / len(clv_vals) if clv_vals else None
    pct_pos_clv = (
        sum(1 for c in clv_vals if c > 0) / len(clv_vals) if clv_vals else None
    )

    # proper scoring of the probabilities
    eps = 1e-9
    brier = sum((b["model_prob"] - _won(b)) ** 2 for b in bets) / len(bets)
    logloss = -sum(
        _won(b) * math.log(min(max(b["model_prob"], eps), 1 - eps))
        + (1 - _won(b)) * math.log(min(max(1 - b["model_prob"], eps), 1 - eps))
        for b in bets
    ) / len(bets)

    return {
        "n": len(bets),
        "hit_rate": wins / len(bets),
        "staked": round(staked, 2),
        "pnl": round(pnl, 2),
        "roi": round(pnl / staked, 4) if staked else None,  # yield per unit staked
        "avg_clv": round(avg_clv, 4) if avg_clv is not None else None,
        "pct_positive_clv": round(pct_pos_clv, 3) if pct_pos_clv is not None else None,
        "brier": round(brier, 4),
        "log_loss": round(logloss, 4),
    }


def calibration_table(bets: list[dict], buckets: int = 10) -> list[dict]:
    """Predicted vs realised win rate, bucketed by model probability."""
    out = []
    for k in range(buckets):
        lo, hi = k / buckets, (k + 1) / buckets
        grp = [b for b in bets if lo <= b["model_prob"] < hi or (hi == 1.0 and b["model_prob"] == 1.0)]
        if not grp:
            continue
        out.append(
            {
                "range": f"{lo:.1f}-{hi:.1f}",
                "n": len(grp),
                "avg_pred": round(sum(b["model_prob"] for b in grp) / len(grp), 3),
                "realised": round(sum(_won(b) for b in grp) / len(grp), 3),
            }
        )
    return out


def segment_stats(bets: list[dict]) -> dict:
    """Reliability by market and by odds band — where is the edge real?"""
    def group(key_fn):
        groups: dict = {}
        for b in bets:
            groups.setdefault(key_fn(b), []).append(b)
        return {k: performance(v) for k, v in groups.items()}

    return {
        "by_market": group(lambda b: b["market"]),
        "by_odds_band": group(lambda b: odds_band(b["price"])),
    }


# --------------------------------------------------------------------------- #
# isotonic calibration via Pool Adjacent Violators
# --------------------------------------------------------------------------- #
def _pav(values: list[float], weights: list[float]) -> list[float]:
    """Non-decreasing isotonic fit. Returns fitted value per input position."""
    blocks = []  # each: [weighted_sum, weight, count]
    for v, w in zip(values, weights):
        blocks.append([v * w, w, 1])
        while len(blocks) >= 2 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            s2, w2, c2 = blocks.pop()
            s1, w1, c1 = blocks.pop()
            blocks.append([s1 + s2, w1 + w2, c1 + c2])
    fitted = []
    for s, w, c in blocks:
        fitted.extend([s / w] * c)
    return fitted


class Calibrator:
    """Maps a raw model probability to a calibrated one.

    Below `min_samples` it is the identity (don't trust a tiny history), and it
    warns so the caller knows calibration isn't active yet.
    """

    def __init__(self, xs: list[float], fitted: list[float], active: bool):
        self.xs = xs
        self.fitted = fitted
        self.active = active

    def __call__(self, p: float) -> float:
        if not self.active or not self.xs:
            return p
        if p <= self.xs[0]:
            return self.fitted[0]
        if p >= self.xs[-1]:
            return self.fitted[-1]
        # piecewise-linear interpolation between fitted step points
        for i in range(1, len(self.xs)):
            if p <= self.xs[i]:
                x0, x1 = self.xs[i - 1], self.xs[i]
                y0, y1 = self.fitted[i - 1], self.fitted[i]
                if x1 == x0:
                    return y1
                return y0 + (y1 - y0) * (p - x0) / (x1 - x0)
        return self.fitted[-1]


def fit_calibrator(bets: list[dict], min_samples: int = 50, nbins: int = 15) -> Calibrator:
    settled = [b for b in bets if b["status"] in ("won", "lost")]
    if len(settled) < min_samples:
        return Calibrator([], [], active=False)

    # bin by model probability, then isotonic-fit the per-bin realised rates
    # (weighted by bin count). Binning first keeps a single lucky tail result
    # from pinning the fitted endpoint to 0 or 1.
    bins: dict[int, list[float]] = {}
    for b in settled:
        k = min(nbins - 1, int(b["model_prob"] * nbins))
        bins.setdefault(k, []).append(_won(b))
    # also track average predicted prob per bin for the x-axis
    xbins: dict[int, list[float]] = {}
    for b in settled:
        k = min(nbins - 1, int(b["model_prob"] * nbins))
        xbins.setdefault(k, []).append(b["model_prob"])

    ks = sorted(bins)
    xs = [sum(xbins[k]) / len(xbins[k]) for k in ks]
    rates = [sum(bins[k]) / len(bins[k]) for k in ks]
    weights = [float(len(bins[k])) for k in ks]
    fitted = _pav(rates, weights)
    return Calibrator(xs, fitted, active=True)
