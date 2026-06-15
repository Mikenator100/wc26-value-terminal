"""Real-rate wiring: team per-game rates from fixture statistics, player per-90
count rates from API-Football blocks, and the fallbacks when data is missing.

No network — mock payloads mirror API-Football response shapes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.teamrates import team_rates  # noqa: E402
from datalayer.normalize import build_player_profile  # noqa: E402
from datalayer.countmarkets import player_expectations, ROLE_COUNTS  # noqa: E402
from datalayer.build import build_match  # noqa: E402


# --- mock fixtures/statistics payloads (two-team blocks, API shape) -------- #
def stats_payload(team_id, corners, cards, shots, sot, offsides, fouls, red=0):
    mine = {"team": {"id": team_id, "name": "Us"}, "statistics": [
        {"type": "Corner Kicks", "value": corners},
        {"type": "Yellow Cards", "value": cards},
        {"type": "Red Cards", "value": red},
        {"type": "Total Shots", "value": shots},
        {"type": "Shots on Goal", "value": sot},
        {"type": "Offsides", "value": offsides},
        {"type": "Fouls", "value": fouls},
        {"type": "Ball Possession", "value": "55%"},  # untracked type: ignored
    ]}
    other = {"team": {"id": 9999, "name": "Them"}, "statistics": []}
    return [mine, other]


def test_team_rates():
    payloads = [
        stats_payload(6, corners=6, cards=1, shots=15, sot=6, offsides=2, fouls=10, red=0),
        stats_payload(6, corners=4, cards=3, shots=11, sot=4, offsides=1, fouls=14, red=1),
    ]
    r = team_rates(6, payloads)
    # 2 measured games shrink toward the priors with 4 pseudo-games
    # (cards use 8 — friendlies under-card vs competitive matches):
    # corners (10+4*5)/6 = 5.0, cards (4+8*2.4)/10 = 2.32
    assert r["corners"] == 5.0 and abs(r["cards"] - 2.32) < 0.01
    assert abs(r["shots"] - 12.67) < 0.01 and abs(r["sot"] - 4.6) < 0.01
    assert abs(r["offsides"] - 1.83) < 0.01 and abs(r["fouls"] - 12.33) < 0.01
    assert r["_games"] == 2
    assert 12.0 <= r["tackles"] <= 21.0  # estimated from fouls, bounded
    # 1 red in 2 games no longer reads as a 30%-a-game team: (1+0.2)/6 = 0.2
    assert abs(r["redProb"] - 0.2) < 1e-9
    # team absent from every payload -> None (caller omits teamRates)
    assert team_rates(7, payloads) is None
    assert team_rates(6, []) is None
    print(f"team rates ok (corners {r['corners']}, shots {r['shots']}, redProb {r['redProb']})")


# --- player per-90 count rates from real API-Football blocks ---------------- #
def midfielder_blocks():
    return [
        {  # club block with the full count stats API-Football returns
            "team": {"id": 50, "name": "Manchester City", "national": False},
            "games": {"appearences": 30, "minutes": 2700, "position": "Midfielder", "grid": "3:2"},
            "shots": {"total": 30, "on": 12},
            "goals": {"total": 5, "assists": 9, "saves": None},
            "cards": {"yellow": 6},
            "tackles": {"total": 60},
            "fouls": {"committed": 33, "drawn": 45},
            "passes": {"total": 1950},
        },
        {  # country block missing the count stats (sparse international data)
            "team": {"id": 6, "name": "Brazil", "national": True},
            "games": {"appearences": 9, "minutes": 720, "position": "Midfielder", "grid": "3:2"},
            "shots": {"total": 8, "on": 3},
            "goals": {"total": 1, "assists": 3},
            "cards": {"yellow": 2},
        },
    ]


def test_player_count_rates():
    p = build_player_profile("Test Mid", "Brazil", midfielder_blocks())
    # club: 60 tackles / 2700 min -> 2.0 per 90; 1950 passes -> 65 per 90
    assert abs(p["club"]["tackles"] - 2.0) < 1e-6
    assert abs(p["club"]["passes"] - 65.0) < 1e-6
    assert abs(p["club"]["fouls"] - 1.1) < 1e-6
    assert abs(p["club"]["fouled"] - 1.5) < 1e-6
    assert p["club"]["saves"] is None
    # country block had no count stats -> None, not 0
    assert p["country"]["tackles"] is None and p["country"]["passes"] is None
    print(f"player count rates ok (tackles/90 {p['club']['tackles']}, passes/90 {p['club']['passes']})")
    return p


def test_expectations_use_measured_rates():
    p = test_player_count_rates()
    exp = player_expectations(p, "CM", start_prob=1.0, team_xg=1.35, opp_xg=1.35)
    # country side missing -> blend falls back to the club's measured 65/90;
    # the big sample (3420 min) keeps it close, with a slight pull toward the
    # CM baseline of 55
    assert 63.5 < exp["passes"] < 65.0
    assert 1.95 < exp["tackles"] <= 2.0
    # a profile with no count data at all still prices off role baselines
    bare = {"club": {"shots": 1.0, "sot": 0.4, "goals": 0.1, "assists": 0.2, "cards": 0.2},
            "country": {"shots": 1.0, "sot": 0.4, "goals": 0.1, "assists": 0.2, "cards": 0.2}}
    exp2 = player_expectations(bare, "CM", start_prob=1.0, team_xg=1.35, opp_xg=1.35)
    assert abs(exp2["passes"] - ROLE_COUNTS["CM"]["passes"]) < 1e-6
    print(f"expectations ok (measured passes {exp['passes']:.1f} vs baseline {exp2['passes']:.1f})")


# --- end-to-end: build_match attaches teamRates ----------------------------- #
class RatesProvider:
    FIXTURE = {
        "fixture": {"id": 999, "date": "2026-06-20T18:00:00+00:00", "status": {"short": "NS"}},
        "teams": {"home": {"id": 6, "name": "Brazil"}, "away": {"id": 10, "name": "Morocco"}},
        "league": {"round": "Group C"},
    }

    def team_statistics(self, league, season, team):
        return {"goals": {"for": {"average": {"total": "1.5"}}, "against": {"average": {"total": "1.0"}}}}

    def predictions(self, fixture_id):
        return None

    def lineups(self, fixture_id):
        return []

    def team_players(self, team, season):
        return []

    def player_seasons(self, player_id, seasons):
        return []

    def team_recent_fixtures(self, team, last=5):
        # team 6 in strong form (3-0s), team 10 losing (1-2s); the upcoming
        # fixture must be skipped by both the rates and the form averages
        gh, ga = (3, 0) if team == 6 else (1, 2)
        return [{"fixture": {"id": 100 + team + i, "status": {"short": "FT"}},
                 "teams": {"home": {"id": team}, "away": {"id": 9999}},
                 "goals": {"home": gh, "away": ga}} for i in range(2)] + \
               [{"fixture": {"id": 555, "status": {"short": "NS"}}}]  # upcoming: skipped

    def fixture_statistics(self, fixture_id):
        team = 6 if fixture_id < 110 else 10
        c = 7 if team == 6 else 3
        return stats_payload(team, corners=c, cards=2, shots=12, sot=5, offsides=2, fouls=12)


def test_build_match_team_rates():
    m = build_match(RatesProvider(), RatesProvider.FIXTURE, league=1, season=2026)
    tr = m["teamRates"]
    # measured 7 vs 3 corners/game, shrunk toward the 5.0 prior (2 games vs 4)
    assert tr["home"]["corners"] > 5.0 > tr["away"]["corners"]
    assert abs(tr["home"]["corners"] - 5.67) < 0.01 and abs(tr["away"]["corners"] - 4.33) < 0.01
    assert tr["home"]["_games"] == 2  # only the finished fixtures counted
    # league-season stats are empty (0 played) -> xG anchors on the Elo
    # matchup (both fake teams are rated), tilted by recent form: the in-form
    # home side prices above the away side, but never blowout lambdas
    assert m["xgHome"] > m["xgAway"]
    assert 1.0 < m["xgHome"] < 2.2 and 0.5 < m["xgAway"] < 1.5
    # team_form=0 skips the calls entirely (and xG degrades to neutral)
    m0 = build_match(RatesProvider(), RatesProvider.FIXTURE, league=1, season=2026, team_form=0)
    assert "teamRates" not in m0
    print(f"build_match teamRates ok (home corners {tr['home']['corners']}, away {tr['away']['corners']}; "
          f"form xg {m['xgHome']}-{m['xgAway']})")


def test_opponent_adjusted_rates():
    # same raw counts in two games; opponent-normalise with a weak (0.7) and a
    # strong (1.4) opponent factor -> attacking counts deflate toward the weak
    # game, discipline counts the other way
    payloads = [
        stats_payload(6, corners=8, cards=1, shots=18, sot=7, offsides=2, fouls=8),
        stats_payload(6, corners=8, cards=1, shots=18, sot=7, offsides=2, fouls=8),
    ]
    raw = team_rates(6, payloads)
    adj = team_rates(6, payloads, opp_factors=[0.7, 0.7])  # both vs minnows
    # corners/shots earned vs minnows are discounted below the raw average
    assert adj["corners"] < raw["corners"] and adj["shots"] < raw["shots"]
    # fouls (discipline) divided by the weak factor -> higher than raw
    assert adj["fouls"] > raw["fouls"]
    print(f"opponent-adjusted rates ok (corners {raw['corners']} -> {adj['corners']} vs minnows)")


def test_team_form_stats():
    from datalayer.teamrates import team_form_stats

    def fx(home_id, away_id, gh, ga, date):
        return {"fixture": {"status": {"short": "FT"}, "date": date},
                "teams": {"home": {"id": home_id}, "away": {"id": away_id}},
                "goals": {"home": gh, "away": ga}}

    # team 6: home win 2-0, home loss 0-1, away win 3-1, away draw 1-1
    fixtures = [
        fx(6, 9, 2, 0, "2026-06-10"),
        fx(6, 9, 0, 1, "2026-06-07"),
        fx(9, 6, 1, 3, "2026-06-04"),
        fx(9, 6, 1, 1, "2026-06-01"),
    ]
    rates = {"corners": 5.5, "cards": 2.0, "redProb": 0.1}
    s = team_form_stats(6, fixtures, rates)
    assert s["form"] == ["W", "L", "W", "D"]          # newest first
    assert s["homeForm"] == ["W", "L"] and s["awayForm"] == ["W", "D"]
    assert s["avgGoalsFor"] == 1.5 and s["avgGoalsAgainst"] == 0.75
    assert s["cleanSheet"] == 0.25                     # 1 of 4 (the 2-0)
    assert s["failedToScore"] == 0.25                  # 1 of 4 (the 0-1)
    assert s["avgCorners"] == 5.5
    assert s["bookingPoints"] == round(10 * 2.0 + 25 * 0.1, 1)  # 22.5
    print(f"team form stats ok ({s['form']}, CS {s['cleanSheet']}, booking {s['bookingPoints']})")


def test_recent_player_counts():
    from datalayer.normalize import recent_player_counts

    def fp_payload(lines):
        # one fixtures/players response: [{team, players:[{player, statistics}]}]
        return [{"team": {"id": 6}, "players": [
            {"player": {"id": pid}, "statistics": [{
                "games": {"minutes": mins},
                "shots": {"total": sh, "on": so},
                "tackles": {"total": tk},
                "fouls": {"committed": fo},
            }]} for pid, mins, sh, so, tk, fo in lines]}]

    payloads = [
        fp_payload([(11, 90, 4, 2, 1, 1), (12, 0, None, None, None, None)]),  # newest
        fp_payload([(11, 85, 2, 1, 0, 2)]),
    ]
    counts = recent_player_counts(payloads)
    assert [g["shots"] for g in counts[11]] == [4, 2]   # newest first
    assert counts[11][0]["sot"] == 2 and counts[11][1]["fouls"] == 2
    assert 12 not in counts                              # 0 minutes = didn't play
    print(f"recent player counts ok ({counts[11]})")


def test_elo_lambdas():
    from datalayer.elo import elo_lambdas, rating, load_table
    table = load_table()
    lh, la = elo_lambdas(rating("Brazil", table), rating("Morocco", table))
    assert lh > la                              # better side scores more
    assert abs((lh + la) - 2.6) < 1e-6          # neutral venue keeps the total
    assert 1.0 < lh < 2.0 and 0.8 < la < 1.5    # sane, never blowout lambdas
    # a huge rating gap stays bounded
    bh, ba = elo_lambdas(2150, 1380)
    assert bh <= 2.4 + 1e-9 and ba >= 0.25
    # venue multiplier shifts, doesn't explode
    vh, va = elo_lambdas(1800, 1800, venue_mult=1.08)
    assert vh > 1.3 > va
    print(f"elo lambdas ok (BRA-MAR {lh}/{la}, capped {bh}/{ba})")


def test_elo_apply_results():
    from datalayer.elo import apply_results, BASELINE

    def fx(home, away, gh, ga, date):
        return {"fixture": {"status": {"short": "FT"}, "date": date},
                "teams": {"home": {"name": home}, "away": {"name": away}},
                "goals": {"home": gh, "away": ga}}

    base = dict(BASELINE)
    # an upset: Canada beat Brazil — Canada gains, Brazil drops
    t1 = apply_results(base, [fx("Canada", "Brazil", 2, 0, "2026-06-13")])
    assert t1["canada"] > base["canada"] and t1["brazil"] < base["brazil"]
    # a bigger margin moves ratings more than a narrow one
    narrow = apply_results(base, [fx("Canada", "Brazil", 1, 0, "2026-06-13")])
    assert t1["canada"] - base["canada"] > narrow["canada"] - base["canada"]
    # expected wins barely move; draws against stronger sides gain
    fav = apply_results(base, [fx("Brazil", "El Salvador", 3, 0, "2026-06-13")])
    assert fav["brazil"] - base["brazil"] < 8
    drew = apply_results(base, [fx("El Salvador", "Brazil", 1, 1, "2026-06-13")])
    assert drew["el salvador"] > base["el salvador"]
    # unknown teams are skipped, unfinished matches ignored
    same = apply_results(base, [fx("Atlantis", "Brazil", 9, 0, "2026-06-13"),
                                {"fixture": {"status": {"short": "NS"}, "date": ""},
                                 "teams": {"home": {"name": "Canada"}, "away": {"name": "Brazil"}},
                                 "goals": {"home": None, "away": None}}])
    assert same == base
    print(f"elo results ok (upset +{t1['canada'] - base['canada']:.1f}, routine win +{fav['brazil'] - base['brazil']:.1f})")


def test_elo_weighting():
    from datalayer.elo import rating, goal_factor, load_table
    from datalayer.teamrates import form_goal_averages
    table = load_table()
    assert rating("El Salvador", table) < rating("France", table)
    assert goal_factor(rating("El Salvador", table)) < 1.0 < goal_factor(rating("France", table))
    assert goal_factor(None) == 1.0  # unknown opponent stays neutral
    # same 2-0 wins, but against a minnow vs a giant
    def fx(opp):
        return {"fixture": {"id": 1, "status": {"short": "FT"}},
                "teams": {"home": {"id": 6, "name": "Us"}, "away": {"id": 9, "name": opp}},
                "goals": {"home": 2, "away": 0}}
    factor = lambda opp: goal_factor(rating(opp, table))
    weak_gf, _ = form_goal_averages(6, [fx("El Salvador")], elo_factor=factor)
    strong_gf, _ = form_goal_averages(6, [fx("France")], elo_factor=factor)
    assert weak_gf < 2.0 < strong_gf
    print(f"elo weighting ok (2-0 vs ELS -> gf {weak_gf}, vs FRA -> gf {strong_gf})")


def test_minutes_exposure():
    from datalayer.countmarkets import _exposure
    # 'starts but comes off on 60' starter: exposure well below a full match
    rotated = {"last5": [{"minutes": 62}, {"minutes": 58}, {"minutes": 65}]}
    e_rot = _exposure(rotated, 1.0)
    assert 0.68 <= e_rot <= 0.75
    # 90-minute anchor stays ~1
    anchor = {"last5": [{"minutes": 90}, {"minutes": 90}, {"minutes": 90}]}
    assert _exposure(anchor, 1.0) == 1.0
    # super-sub: appears for ~20 minutes -> low exposure even at startProb 0.4
    supersub = {"last5": [{"minutes": 20}, {"minutes": 18}, {"minutes": 25}]}
    e_sub = _exposure(supersub, 0.4)
    assert e_sub < 0.55
    # no minutes data (sample path): legacy structural defaults
    assert _exposure({}, 1.0) == 1.0
    assert abs(_exposure({}, 0.0) - 0.0) < 1e-9
    print(f"minutes exposure ok (rotated {e_rot:.2f}, super-sub {e_sub:.2f})")


def test_shot_based_scoring():
    from datalayer.countmarkets import player_expectations
    base = {"sot": 1.0, "shots": 2.4, "assists": 0.2, "cards": 0.1,
            "tackles": 0.5, "fouls": 1.0, "fouled": 1.2, "passes": 25.0, "saves": None}
    # two strikers, same SOT volume, wildly different (small-sample) goal luck
    cold = {"club": {**base, "goals": 0.05}, "country": {**base, "goals": 0.05},
            "_minutes": {"club": 1800, "country": 600}}
    hot = {"club": {**base, "goals": 1.1}, "country": {**base, "goals": 1.1},
           "_minutes": {"club": 1800, "country": 600}}
    ec = player_expectations(cold, "ST", 1.0, team_xg=1.35, opp_xg=1.35)
    eh = player_expectations(hot, "ST", 1.0, team_xg=1.35, opp_xg=1.35)
    # shot-based pricing pulls both toward SOT x conversion: the cold finisher
    # prices well above his raw 0.05 goals/90, the hot one below his raw 1.1
    assert ec["goals"] > 0.15
    assert eh["goals"] < 0.85
    assert eh["goals"] > ec["goals"]  # finishing signal isn't erased
    print(f"shot-based scoring ok (cold {ec['goals']:.2f}, hot {eh['goals']:.2f})")


def test_effective_country_weight():
    from datalayer.countmarkets import effective_country_weight
    # thin international sample loses say against a big club sample
    p = {"_minutes": {"club": 3000, "country": 300}}
    w = effective_country_weight(p, 0.6)
    assert w < 0.45
    # both well-sampled -> close to the stated preference
    p2 = {"_minutes": {"club": 3000, "country": 2500}}
    assert abs(effective_country_weight(p2, 0.6) - 0.6) < 0.05
    # no minutes info (sample data) -> unchanged
    assert effective_country_weight({}, 0.6) == 0.6
    print(f"effective weight ok (thin country {w:.2f}, no info 0.60)")


class AutoLineupProvider(RatesProvider):
    """RatesProvider + a roster and recent confirmed XIs for the home side."""

    def team_players(self, team, season):
        if team == 6:
            return [{"player": {"id": 11, "name": "Star Forward"}},
                    {"player": {"id": 12, "name": "Bench Guy"}}]
        return []

    def player_seasons(self, player_id, seasons):
        return midfielder_blocks()

    def lineups(self, fixture_id, ttl=300):
        if fixture_id in (106, 107):  # home side's finished form fixtures
            return [
                {"team": {"id": 6, "name": "Brazil"}, "startXI": [
                    # name spelled differently than the roster on purpose —
                    # the id match must carry it
                    {"player": {"id": 11, "name": "Forward Star", "pos": "F", "grid": "1:3"}},
                ]},
                {"team": {"id": 10, "name": "Morocco"}, "startXI": []},
            ]
        return []  # no confirmed XI for the upcoming fixture


def test_build_match_auto_lineups():
    m = build_match(AutoLineupProvider(), RatesProvider.FIXTURE, league=1, season=2026,
                    auto_lineups=True)
    star = next(p for p in m["players"] if p["name"] == "Star Forward")
    bench = next(p for p in m["players"] if p["name"] == "Bench Guy")
    assert star["startProb"] == 0.75      # started 2 of 2 recent -> (2+1)/(2+2)
    assert star["predictedPos"] == "ST"   # central forward slot
    assert bench["startProb"] == 0.25     # outside the predicted XI -> bench default
    # without the flag, nobody is predicted and the generic default applies
    m0 = build_match(AutoLineupProvider(), RatesProvider.FIXTURE, league=1, season=2026)
    assert all(p["startProb"] == 0.7 for p in m0["players"])
    print(f"auto lineups ok (starter {star['startProb']} {star['predictedPos']}, bench {bench['startProb']})")


if __name__ == "__main__":
    test_team_rates()
    test_player_count_rates()
    test_expectations_use_measured_rates()
    test_build_match_team_rates()
    test_opponent_adjusted_rates()
    test_team_form_stats()
    test_recent_player_counts()
    test_elo_lambdas()
    test_elo_apply_results()
    test_elo_weighting()
    test_minutes_exposure()
    test_shot_based_scoring()
    test_effective_country_weight()
    test_build_match_auto_lineups()
    print("\nALL TEAM-RATE TESTS PASSED\n")
