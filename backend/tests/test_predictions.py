"""Prediction journal: pre-match prediction shape, record/lock/grade, summary."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.predictions import match_prediction, PredictionLog  # noqa: E402


def _match():
    return {
        "id": "777", "home": "Spain", "away": "Cape Verde", "kickoff": "2026-06-20T18:00:00Z",
        "live": False, "xgHome": 2.5, "xgAway": 0.4, "xgModelHome": 2.4, "xgModelAway": 0.4,
        "teamRates": {"home": {"corners": 6.5}, "away": {"corners": 3.5}},
    }


def test_prediction_shape():
    p = match_prediction(_match())
    assert p["p_home"] > 0.7 and p["p_away"] < 0.1          # heavy favourite
    assert abs(p["p_home"] + p["p_draw"] + p["p_away"] - 1) < 1e-3   # ~1 (4dp rounding)
    assert p["exp_goals"] == 2.9 and 0 < p["p_over25"] < 1
    assert p["market_fit"] == 1                              # xgModel present
    assert p["exp_corners"] > 0 and p["p_corner_over"] is not None
    print(f"prediction shape ok (home {p['p_home']:.0%}, corners exp {p['exp_corners']})")


def test_record_lock_grade():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    try:
        log = PredictionLog(f.name)
        log.record(_match())
        assert len(log.pending()) == 1
        # re-record while pending updates in place (no duplicate)
        log.record(_match())
        assert len(log.pending()) == 1
        # grade: Spain win 3-0 with 11 corners
        assert log.grade("777", 3, 0, corners=11) is True
        assert len(log.pending()) == 0 and len(log.settled()) == 1
        # a settled row is never overwritten by a later record
        log.record(_match())
        assert len(log.pending()) == 0
        s = log.summary()
        assert s["n"] == 1 and s["result_hit"] == 1.0          # favourite won
        assert s["brier_result"] is not None and s["brier_corner"] is not None
        # grading an unknown / already-settled match returns False
        assert log.grade("777", 1, 1) is False
        print(f"record/lock/grade ok (brier_result {s['brier_result']}, hit {s['result_hit']})")
    finally:
        os.unlink(f.name)


if __name__ == "__main__":
    test_prediction_shape()
    test_record_lock_grade()
    print("\nALL PREDICTION TESTS PASSED\n")
