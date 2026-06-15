"""Prediction journal: log the model's pre-match expectation for every match,
then grade it against the actual result as games finish.

This is the model's own report card — distinct from the bet ledger (which is
about prices/edge). It records, per match, the result probabilities, expected
goals + Over 2.5, and expected corners + a corners line, locked at the last
pre-kickoff feed cycle. Once a match finishes it's graded (Brier, log-loss,
hit) so accuracy accumulates and the model can be tuned against its own
realised calibration.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from typing import Optional

from .snapshots import score_matrix, result_probs, total_over_prob
from .countmarkets import count_dist, over as nb_over, DISP


def _clamp(x, a, b):
    return max(a, min(b, x))


def match_prediction(match: dict) -> dict:
    """The model's pre-match expectation from the feed's xG + team rates."""
    lh = match.get("xgHome", 1.3) or 1.3
    la = match.get("xgAway", 1.3) or 1.3
    m = score_matrix(lh, la)
    rp = result_probs(m)
    out = {
        "p_home": round(rp["home"], 4), "p_draw": round(rp["draw"], 4),
        "p_away": round(rp["away"], 4),
        "exp_goals": round(lh + la, 3), "p_over25": round(total_over_prob(m, 2.5), 4),
        "market_fit": 1 if match.get("xgModelHome") is not None else 0,
    }
    tr = match.get("teamRates")
    if tr and tr.get("home") and tr.get("away"):
        aH, aA = _clamp(lh / 1.35, 0.6, 1.7), _clamp(la / 1.35, 0.6, 1.7)
        lam = tr["home"]["corners"] * aH + tr["away"]["corners"] * aA
        line = math.floor(lam) + 0.5
        out.update({"exp_corners": round(lam, 2), "corner_line": line,
                    "p_corner_over": round(nb_over(count_dist(lam, DISP["corners"]), line), 4)})
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
  match_id TEXT PRIMARY KEY, ts REAL, kickoff TEXT, home TEXT, away TEXT,
  p_home REAL, p_draw REAL, p_away REAL, exp_goals REAL, p_over25 REAL,
  exp_corners REAL, corner_line REAL, p_corner_over REAL, market_fit INTEGER,
  status TEXT DEFAULT 'pending', gh INTEGER, ga INTEGER, corners INTEGER,
  brier_result REAL, ll_result REAL, brier_ou25 REAL, brier_corner REAL,
  result_hit INTEGER, settled_ts REAL
);
"""

_COLS = ("p_home", "p_draw", "p_away", "exp_goals", "p_over25",
         "exp_corners", "corner_line", "p_corner_over", "market_fit")


class PredictionLog:
    def __init__(self, path: str = "predictions.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.execute(SCHEMA)
            self.conn.commit()

    def record(self, match: dict) -> None:
        """Upsert a match's prediction while it's still pending (so it locks to
        the last pre-kickoff cycle); never overwrite a settled row."""
        pred = match_prediction(match)
        row = {"match_id": str(match.get("id")), "ts": time.time(),
               "kickoff": match.get("kickoff", ""), "home": match.get("home"),
               "away": match.get("away"),
               **{c: pred.get(c) for c in _COLS}}
        with self._lock:
            ex = self.conn.execute("SELECT status FROM predictions WHERE match_id=?",
                                   (row["match_id"],)).fetchone()
            if ex and ex["status"] == "settled":
                return
            cols = ",".join(row)
            ph = ",".join(":" + c for c in row)
            upd = ",".join(f"{c}=:{c}" for c in row if c != "match_id")
            self.conn.execute(
                f"INSERT INTO predictions ({cols}) VALUES ({ph}) "
                f"ON CONFLICT(match_id) DO UPDATE SET {upd}", row)
            self.conn.commit()

    def grade(self, match_id: str, gh: int, ga: int, corners: Optional[int] = None) -> bool:
        """Score a finished match against its locked prediction."""
        with self._lock:
            r = self.conn.execute("SELECT * FROM predictions WHERE match_id=? AND status='pending'",
                                  (str(match_id),)).fetchone()
            if r is None:
                return False
            aH, aD, aA = (1, 0, 0) if gh > ga else (0, 1, 0) if gh == ga else (0, 0, 1)
            brier = (r["p_home"] - aH) ** 2 + (r["p_draw"] - aD) ** 2 + (r["p_away"] - aA) ** 2
            p_act = r["p_home"] if aH else r["p_draw"] if aD else r["p_away"]
            ll = -math.log(min(max(p_act, 1e-9), 1))
            pick = max((r["p_home"], "h"), (r["p_draw"], "d"), (r["p_away"], "a"))[1]
            actual = "h" if gh > ga else "d" if gh == ga else "a"
            ou_act = 1 if gh + ga > 2.5 else 0
            brier_ou = (r["p_over25"] - ou_act) ** 2
            brier_c = None
            if corners is not None and r["corner_line"] is not None and r["p_corner_over"] is not None:
                c_act = 1 if corners > r["corner_line"] else 0
                brier_c = (r["p_corner_over"] - c_act) ** 2
            self.conn.execute(
                "UPDATE predictions SET status='settled', gh=?, ga=?, corners=?, "
                "brier_result=?, ll_result=?, brier_ou25=?, brier_corner=?, "
                "result_hit=?, settled_ts=? WHERE match_id=?",
                (gh, ga, corners, round(brier, 4), round(ll, 4), round(brier_ou, 4),
                 round(brier_c, 4) if brier_c is not None else None,
                 1 if pick == actual else 0, time.time(), str(match_id)))
            self.conn.commit()
            return True

    def pending(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM predictions WHERE status='pending'").fetchall()]

    def settled(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM predictions WHERE status='settled'").fetchall()]

    def summary(self) -> dict:
        s = self.settled()
        n = len(s)
        if not n:
            return {"n": 0, "pending": len(self.pending())}
        avg = lambda k: round(sum(r[k] for r in s if r[k] is not None)
                              / max(1, sum(1 for r in s if r[k] is not None)), 4)
        cn = sum(1 for r in s if r["brier_corner"] is not None)
        return {
            "n": n, "pending": len(self.pending()),
            "result_hit": round(sum(r["result_hit"] for r in s) / n, 4),
            "brier_result": avg("brier_result"), "log_loss": avg("ll_result"),
            "brier_ou25": avg("brier_ou25"),
            "brier_corner": avg("brier_corner") if cn else None, "corner_n": cn,
            "recent": [
                {"home": r["home"], "away": r["away"],
                 "pred": [round(r["p_home"], 2), round(r["p_draw"], 2), round(r["p_away"], 2)],
                 "exp_goals": r["exp_goals"], "score": f"{r['gh']}-{r['ga']}",
                 "hit": r["result_hit"]}
                for r in sorted(s, key=lambda r: -(r["settled_ts"] or 0))[:20]
            ],
        }
