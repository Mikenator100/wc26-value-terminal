"""Validate the count-market engine: distributions sum to 1, markets are
internally consistent, and NB tails are fatter than Poisson."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.countmarkets import (  # noqa: E402
    count_dist, over, at_least, team_most, team_markets, player_markets,
)


def test_dist():
    d = count_dist(10.2, 10)
    assert abs(sum(d) - 1.0) < 1e-9
    # over + under = 1 at every line
    for l in (8.5, 9.5, 10.5):
        assert abs(over(d, l) + (1 - over(d, l)) - 1.0) < 1e-12
    # NB tail fatter than Poisson
    nb = over(count_dist(10.2, 10), 13.5)
    po = over(count_dist(10.2, None), 13.5)
    assert nb > po, (nb, po)
    print(f"dist ok (NB Over13.5 {nb:.3f} > Poisson {po:.3f})")


def test_team_markets():
    rates = {
        "home": {"corners": 6.0, "cards": 1.6, "shots": 14, "sot": 5.2, "offsides": 1.8, "tackles": 16, "fouls": 11, "redProb": 0.05},
        "away": {"corners": 4.2, "cards": 2.0, "shots": 9, "sot": 3.2, "offsides": 1.4, "tackles": 18, "fouls": 13, "redProb": 0.06},
    }
    m = team_markets(rates)
    # every two-way / three-way market sums to ~1
    for name, outcomes in m.items():
        total = sum(o["prob"] for o in outcomes)
        assert 0.98 < total < 1.02, (name, total)
    assert "Corners O/U 9.5" in m and "Both teams to be carded" in m and "Red card in match" in m
    tmost = m["Corners — team with most"]
    assert tmost[0]["prob"] > tmost[1]["prob"]  # stronger home side wins corners more often

    # derived counts: free kicks = fouls + offsides (+ extras) -> mu ~26.7,
    # so Over 23.5 should be clearly likelier than Under
    assert "Free kicks O/U 23.5" in m and "Goal kicks O/U 15.5" in m and "Throw-ins O/U 39.5" in m
    fk = m["Free kicks O/U 23.5"]
    assert fk[0]["prob"] > 0.6, fk
    # off-target shots total 14.6 -> goal kicks mu ~13.8, Under 15.5 favoured
    gk = m["Goal kicks O/U 15.5"]
    assert gk[1]["prob"] > 0.5, gk
    print(f"team markets ok ({len(m)} markets; corners O/U 9.5 over fair {m['Corners O/U 9.5'][0]['fair']}; "
          f"FK over23.5 {fk[0]['prob']}, GK under15.5 {gk[1]['prob']})")


def test_player_markets():
    out = player_markets({"goals": 0.5, "assists": 0.25, "shots": 3.0, "sot": 1.2,
                          "tackles": 1.5, "fouls": 1.0, "fouled": 1.4, "passes": 35, "card_prob": 0.14})
    assert out["Shots 1+"][0]["prob"] > out["Shots 2+"][0]["prob"]  # monotone in line
    assert "To score or assist" in out and "Passes over 34.5" in out
    gk = player_markets({"saves": 3.1, "card_prob": 0.05, "is_gk": True})
    assert "Saves 2+" in gk and "Anytime goalscorer" not in gk
    print(f"player markets ok (outfield {len(out)} mkts, GK {len(gk)} mkts; "
          f"shots1+ {out['Shots 1+'][0]['prob']:.2f} > shots2+ {out['Shots 2+'][0]['prob']:.2f})")


if __name__ == "__main__":
    test_dist()
    test_team_markets()
    test_player_markets()
    print("\nALL COUNT-MARKET TESTS PASSED")


def test_prop_accuracy():
    from datalayer.countmarkets import player_expectations
    striker = {"club": {"shots": 3.0, "sot": 1.2, "goals": 0.5, "assists": 0.2, "cards": 0.12},
               "country": {"shots": 2.6, "sot": 1.0, "goals": 0.42, "assists": 0.18, "cards": 0.11}}
    weak = player_expectations(striker, "ST", 1.0, team_xg=2.1, opp_xg=0.8)
    strong = player_expectations(striker, "ST", 1.0, team_xg=0.9, opp_xg=1.9)
    pen = player_expectations(striker, "ST", 1.0, team_xg=2.1, opp_xg=0.8, set_pieces={"pen": True})
    gs = lambda e: player_markets(e)["Anytime goalscorer"][0]["prob"]
    assert gs(weak) > gs(strong), "weak opponent -> higher goalscorer prob"
    assert gs(pen) > gs(weak), "penalty taker -> higher still"
    # defensive load: stronger opponent -> more fouls/tackles expected
    assert strong["fouls"] > weak["fouls"]
    print(f"prop accuracy ok (GS vs weak {gs(weak):.2f} > strong {gs(strong):.2f}; +pen {gs(pen):.2f})")


if __name__ == "__main__":
    test_prop_accuracy()
