"""Prove the full loop: log -> settle -> measure -> calibrate -> recommend.

We synthesise a history with a *known* flaw — the model is overconfident (says
0.60 on bets that truly win 0.50) and one segment (high odds) is genuinely bad —
then check that the system detects and corrects both.
"""

import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.betlog import (  # noqa: E402
    Ledger,
    Recommender,
    Candidate,
    performance,
    calibration_table,
    fit_calibrator,
)

random.seed(7)


def build_history(led: Ledger):
    """600 settled bets. Model claims 0.60 but reality is 0.50 (overconfident).
    Odds-band 5.0+ bets are deliberately terrible (negative CLV + low win rate).
    """
    for _ in range(600):
        high = random.random() < 0.3
        price = round(random.uniform(5.0, 8.0), 2) if high else round(random.uniform(1.6, 2.2), 2)
        model_prob = 0.60 if not high else 0.22
        # truth: normal segment slightly overconfident; high-odds segment much worse
        true_p = 0.50 if not high else 0.12
        bid = led.record(
            match_id="m" + str(random.randint(1, 50)),
            market="ou25" if not high else "sgm",
            selection="Over 2.5" if not high else "longshot multi",
            model_prob=model_prob,
            price=price,
            stake=1.0,
            bankroll=100.0,
        )
        won = random.random() < true_p
        # CLV = price_taken/closing - 1. Good segment beats the close (took a
        # bigger price -> closing lower); bad high-odds segment took worse prices.
        closing = round(price * (random.uniform(0.95, 0.99) if not high else random.uniform(1.06, 1.16)), 2)
        led.settle(bid, "won" if won else "lost", closing_price=closing)


def main():
    db = tempfile.mktemp(suffix=".db")
    led = Ledger(db)
    build_history(led)
    hist = led.settled()

    perf = performance(hist)
    print("OVERALL:", perf)
    assert perf["n"] == 600
    assert perf["roi"] < 0  # overconfident model loses overall, as designed

    print("\nCALIBRATION TABLE:")
    for row in calibration_table(hist):
        print(" ", row)

    # calibration should pull 0.60 down toward ~0.50
    cal = fit_calibrator(hist)
    assert cal.active, "should be active with 600 settled bets"
    c060 = cal(0.60)
    print(f"\ncalibrated(0.60) = {c060:.3f}  (raw 0.60, truth 0.50)")
    assert 0.44 < c060 < 0.56, c060

    # recommender should distrust the high-odds 'sgm' segment (negative CLV)
    rec = Recommender(hist, kelly_fraction=0.25)
    print("calibration active:", rec.calibration_active)
    good = rec.evaluate(Candidate("mX", "ou25", "Over 2.5", 0.60, 2.05))
    bad = rec.evaluate(Candidate("mX", "sgm", "longshot multi", 0.22, 6.5))
    print("\nGOOD segment eval:", good)
    print("BAD  segment eval:", bad)
    assert good.segment_trust > bad.segment_trust, "bad segment must be trusted less"
    # raw edge on the longshot may look positive, but calibration+trust kill it
    print(f"\nlongshot raw_edge={bad.raw_edge}  adjusted_edge={bad.adjusted_edge}")
    assert bad.adjusted_edge < bad.raw_edge

    # ranking: feed mixed candidates, confirm good value floats up
    cands = [
        Candidate("m1", "ou25", "Over 2.5", 0.58, 2.10),
        Candidate("m1", "sgm", "longshot multi", 0.22, 7.0),
        Candidate("m2", "ou25", "Under 2.5", 0.55, 1.95),
    ]
    picks = rec.recommend(cands, bankroll=200.0)
    print("\nRANKED RECOMMENDATIONS:")
    for p in picks:
        print(f"  {p.selection:18s} adj_edge={p.adjusted_edge:+.3f} trust={p.segment_trust} stake=${p.stake}")
    assert all(p.market != "sgm" for p in picks), "the bad-segment longshot should be filtered out"

    print("\nALL BETLOG TESTS PASSED")


if __name__ == "__main__":
    main()
