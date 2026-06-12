"""Snapshot recording + backtest replay, on synthetic data. No network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.snapshots import (  # noqa: E402
    score_matrix, result_probs, total_over_prob, btts_prob, snapshot_feed,
)
from datalayer.backtest import evaluate, pick_paper_bets  # noqa: E402
from datalayer.settler import resolve_outcome  # noqa: E402


def test_matrix():
    m = score_matrix(2.0, 0.5)
    rp = result_probs(m)
    assert abs(sum(rp.values()) - 1) < 1e-6
    assert rp["home"] > 0.6 > rp["away"]            # strong home side
    assert 0 < total_over_prob(m, 2.5) < 1
    assert btts_prob(m) < 0.5                        # weak away attack
    print(f"matrix ok (home {rp['home']:.3f}, over2.5 {total_over_prob(m, 2.5):.3f})")


def test_dixon_coles():
    import math
    lh, la = 1.3, 1.1
    m = score_matrix(lh, la)
    # independent-Poisson draw probability, computed analytically here
    pois = lambda k, lam: math.exp(-lam) * lam ** k / math.factorial(k)
    indep_draw = sum(pois(k, lh) * pois(k, la) for k in range(11))
    dc_draw = result_probs(m)["draw"]
    assert dc_draw > indep_draw          # rho < 0 adds mass to 0-0 and 1-1
    assert m[0][1] < pois(0, lh) * pois(1, la)  # and removes it from 0-1
    print(f"dixon-coles ok (draw {dc_draw:.4f} > independent {indep_draw:.4f})")


def _feed_match(b365_home=2.6, pin_home=2.5):
    return {
        "id": "m1", "kickoff": "2026-06-20T18:00:00+00:00",
        "xgHome": 1.9, "xgAway": 0.7,
        "markets": [
            {"key": "1x2", "name": "Match result", "outcomes": [
                {"label": "Strongland", "bet365": b365_home, "pinnacle": pin_home},
                {"label": "Draw", "bet365": 3.4, "pinnacle": 3.5},
                {"label": "Weakia", "bet365": 3.1, "pinnacle": 3.0},
            ]},
            {"key": "ou25", "name": "Total goals — Over/Under 2.5", "outcomes": [
                {"label": "Over 2.5", "bet365": 2.0, "pinnacle": 1.95},
                {"label": "Under 2.5", "bet365": 1.9, "pinnacle": 1.95},
            ]},
        ],
    }


def test_snapshot_rows():
    rows = snapshot_feed([_feed_match()], ts=1000.0)
    assert len(rows) == 5
    home = next(r for r in rows if r["outcome"] == "home")
    assert home["bet365"] == 2.6 and 0 < home["sharp_prob"] < 1 and 0 < home["model_prob"] < 1
    over = next(r for r in rows if r["outcome"] == "over")
    assert over["line"] == 2.5
    # a match without merged odds produces no rows
    assert snapshot_feed([{"id": "x", "xgHome": 1, "xgAway": 1, "markets": []}]) == []
    print(f"snapshot ok ({len(rows)} rows; home model {home['model_prob']})")


def test_evaluate():
    # entry snapshot: bet365 generous (2.6) -> edge clears threshold;
    # closing snapshot: sharp price shortens to 2.0 -> positive CLV
    early = snapshot_feed([_feed_match(b365_home=2.6, pin_home=2.5)], ts=1000.0)
    closing = snapshot_feed([_feed_match(b365_home=2.1, pin_home=2.0)], ts=2000.0)
    snaps = early + closing
    results = {"m1": (2, 0)}  # home win, 2 goals total

    rep = evaluate(snaps, results, edge_threshold=0.02, model_weight=0.3)
    assert rep["bets"] >= 1
    home_bet = next(b for b in rep["picks"] if b["market"] == "1x2")
    assert home_bet["bet365"] == 2.6            # bet at the early, generous price
    assert home_bet["pnl"] == 1.6               # won at 2.6 flat 1u
    assert home_bet["clv"] > 0.1                # 2.6 vs ~2.24 closing no-vig fair
    # under 2.5 with total exactly... 2 goals -> under wins; over loses if bet
    assert abs(rep["pnl"] - sum(b["pnl"] for b in rep["picks"])) < 1e-9
    # push handling: total == line returns stake
    push = evaluate(
        snapshot_feed([_feed_match()], ts=1.0) + snapshot_feed([_feed_match()], ts=2.0),
        {"m1": (2, 1)}, edge_threshold=-1)      # threshold -1: bet everything
    over_bet = next(b for b in push["picks"] if "Over" in b["label"])
    assert over_bet["pnl"] in (0.0, 1.0)        # 3 goals: over 2.5 wins... not a push here
    print(f"evaluate ok (bets {rep['bets']}, pnl {rep['pnl']}, clv {home_bet['clv']:.3f})")


def test_implied_lambdas_roundtrip():
    from datalayer.snapshots import implied_lambdas
    # price a known match (1.6 vs 1.0) with ~3% margin, then invert it back
    true_lh, true_la = 1.6, 1.0
    m = score_matrix(true_lh, true_la)
    rp = result_probs(m)
    p_over = total_over_prob(m, 2.5)
    juice = lambda p: round(1 / (p * 1.03), 3)
    markets = [
        {"key": "1x2", "outcomes": [
            {"label": "Home", "pinnacle": juice(rp["home"])},
            {"label": "Draw", "pinnacle": juice(rp["draw"])},
            {"label": "Away", "pinnacle": juice(rp["away"])},
        ]},
        {"key": "ou25", "outcomes": [
            {"label": "Over 2.5", "pinnacle": juice(p_over)},
            {"label": "Under 2.5", "pinnacle": juice(1 - p_over)},
        ]},
    ]
    fit = implied_lambdas(markets)
    assert fit is not None
    assert abs(fit[0] - true_lh) < 0.15 and abs(fit[1] - true_la) < 0.15, fit
    # no sharp prices -> no fit
    assert implied_lambdas([{"key": "1x2", "outcomes": [{"label": "Home"}]}]) is None
    print(f"implied lambdas ok (true {true_lh}/{true_la} -> fitted {fit[0]}/{fit[1]})")


def test_power_devig():
    from datalayer.snapshots import _no_vig
    # 1.50 / 4.20 / 7.00 with ~5% margin: the power method should strip more
    # implied probability from the longshot than proportional division does
    prices = [1.50, 4.20, 7.00]
    nv = _no_vig(prices)
    assert abs(sum(nv) - 1) < 1e-9
    raw = [1 / p for p in prices]
    s = sum(raw)
    proportional = [x / s for x in raw]
    assert nv[2] < proportional[2]          # longshot devigged harder
    assert nv[0] > proportional[0]          # favourite keeps more probability
    # missing outcome / no-margin input falls back to plain rescale
    assert _no_vig([2.0, None])[1] is None
    print(f"power devig ok (longshot {nv[2]:.4f} < proportional {proportional[2]:.4f})")


def test_paper_picks_settleable():
    rows = snapshot_feed([_feed_match(b365_home=2.6, pin_home=2.5)], ts=1.0)
    picks = pick_paper_bets(rows, weight=0.3, threshold=0.02)
    assert picks, "the generous home price should qualify"
    # every market name the paper trader writes must be one the settler
    # resolves — otherwise paper bets sit open forever
    result = {"home_goals": 2, "away_goals": 0,
              "home_team": "Strongland", "away_team": "Weakia", "scorers": []}
    for p in picks:
        outcome = resolve_outcome(p["market"], p["selection"], result)
        assert outcome in ("won", "lost"), (p["market"], p["selection"])
    home = next(p for p in picks if p["selection"] == "Strongland")
    assert resolve_outcome(home["market"], home["selection"], result) == "won"
    assert home["price"] == 2.6 and 0 < home["model_prob"] < 1
    # a tighter threshold filters everything
    assert pick_paper_bets(rows, threshold=5.0) == []
    print(f"paper picks ok ({len(picks)} picks, all settleable)")


def test_push():
    feed = _feed_match()
    feed["markets"][1]["outcomes"][0]["label"] = "Over 3"   # whole-goal line
    feed["markets"][1]["outcomes"][1]["label"] = "Under 3"
    snaps = snapshot_feed([feed], ts=1.0)
    rep = evaluate(snaps, {"m1": (2, 1)}, edge_threshold=-1)  # 3 goals = push
    totals = [b for b in rep["picks"] if b["market"] == "ou25"]
    assert totals and all(b["pnl"] == 0.0 for b in totals)
    print("push ok (total == line returns stake)")


if __name__ == "__main__":
    test_matrix()
    test_dixon_coles()
    test_snapshot_rows()
    test_evaluate()
    test_implied_lambdas_roundtrip()
    test_power_devig()
    test_paper_picks_settleable()
    test_push()
    print("\nALL BACKTEST TESTS PASSED\n")
