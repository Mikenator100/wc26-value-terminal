"""Turn candidate bets into ranked recommendations that improve with history.

Two corrections are applied on top of the raw model:
  1. calibration  — the model's probability is replaced by its calibrated value
     learned from settled results (over/under-confidence is corrected).
  2. segment trust — edge is scaled by how reliable the model has been in that
     market / odds band historically, measured primarily by CLV (the early
     signal of real edge), shrunk toward neutral when the sample is small.

Output ranks by *adjusted edge*, not hit rate — see the README on why hit rate
is the wrong objective.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .ledger import odds_band
from .metrics import Calibrator, fit_calibrator, segment_stats


def _kelly(price: float, prob: float, fraction: float) -> float:
    edge = price * prob - 1
    if edge <= 0:
        return 0.0
    return (edge / (price - 1)) * fraction


@dataclass
class Candidate:
    match_id: str
    market: str
    selection: str
    model_prob: float
    price: float


@dataclass
class Recommendation:
    match_id: str
    market: str
    selection: str
    raw_prob: float
    calibrated_prob: float
    price: float
    raw_edge: float
    adjusted_edge: float
    segment_trust: float
    stake: float
    expected_value: float = 0.0  # expected profit on this bet (profitability, not hit rate)


def _segment_trust(seg_perf: dict, min_n: int = 25) -> float:
    """Map a segment's history to a trust multiplier in roughly [0.4, 1.25].

    Driven by CLV first (fast, reliable), ROI second. Shrunk toward 1.0 when the
    sample is thin so a couple of lucky/unlucky results don't swing it.
    """
    n = seg_perf.get("n", 0)
    if not n:
        return 1.0
    clv = seg_perf.get("avg_clv")
    roi = seg_perf.get("roi") or 0.0
    signal = clv if clv is not None else roi
    raw = 1.0 + 3.0 * signal           # +/-3x sensitivity to the edge signal
    raw = max(0.4, min(1.25, raw))
    shrink = min(1.0, n / min_n)        # blend toward neutral until min_n bets
    return 1.0 + (raw - 1.0) * shrink


class Recommender:
    def __init__(self, history: list[dict], kelly_fraction: float = 0.25, min_edge: float = 0.0):
        self.calibrator: Calibrator = fit_calibrator(history)
        self.segments = segment_stats([b for b in history if b["status"] in ("won", "lost")])
        self.kelly_fraction = kelly_fraction
        self.min_edge = min_edge

    def _trust(self, market: str, price: float) -> float:
        m = self.segments.get("by_market", {}).get(market, {})
        o = self.segments.get("by_odds_band", {}).get(odds_band(price), {})
        # combine market and odds-band trust multiplicatively, lightly
        return _segment_trust(m) * (0.5 + 0.5 * _segment_trust(o))

    def evaluate(self, c: Candidate) -> Recommendation:
        cal_p = self.calibrator(c.model_prob)
        raw_edge = c.price * c.model_prob - 1
        cal_edge = c.price * cal_p - 1
        trust = self._trust(c.market, c.price)
        adj_edge = cal_edge * trust
        stake = _kelly(c.price, cal_p, self.kelly_fraction) if adj_edge > 0 else 0.0
        return Recommendation(
            match_id=c.match_id,
            market=c.market,
            selection=c.selection,
            raw_prob=round(c.model_prob, 4),
            calibrated_prob=round(cal_p, 4),
            price=c.price,
            raw_edge=round(raw_edge, 4),
            adjusted_edge=round(adj_edge, 4),
            segment_trust=round(trust, 3),
            stake=round(stake, 4),
        )

    def recommend(self, candidates: list[Candidate], bankroll: float = 100.0, top: Optional[int] = None):
        recs = [self.evaluate(c) for c in candidates]
        picks = [r for r in recs if r.adjusted_edge > self.min_edge]
        picks.sort(key=lambda r: r.adjusted_edge, reverse=True)
        for r in picks:
            r.stake = round(r.stake * bankroll, 2)  # fraction -> currency
            r.expected_value = round(r.adjusted_edge * r.stake, 2)  # expected profit
        return picks[:top] if top else picks

    @property
    def calibration_active(self) -> bool:
        return self.calibrator.active
