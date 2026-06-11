"""Validate the pure transforms against mock API-Football payloads.

No network: a FakeProvider returns realistic response fragments so we can prove
per-90 maths, the club/country split, role inference and the final match shape.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.normalize import (  # noqa: E402
    per90,
    card_probability,
    infer_role,
    split_club_country,
    build_player_profile,
    team_lambdas,
)
from datalayer.build import build_match  # noqa: E402


# --- mock statistics blocks: one striker, club + country ------------------- #
def striker_blocks():
    return [
        {  # club: Real Madrid, big sample
            "team": {"id": 541, "name": "Real Madrid", "national": False},
            "games": {"appearences": 30, "minutes": 2600, "position": "Attacker", "grid": "1:3"},
            "shots": {"total": 96, "on": 40},
            "goals": {"total": 22, "assists": 7},
            "cards": {"yellow": 4, "red": 0},
        },
        {  # country: Brazil, small sample, slightly lower output
            "team": {"id": 6, "name": "Brazil", "national": True},
            "games": {"appearences": 8, "minutes": 640, "position": "Attacker", "grid": "1:3"},
            "shots": {"total": 18, "on": 7},
            "goals": {"total": 3, "assists": 2},
            "cards": {"yellow": 1, "red": 0},
        },
    ]


def test_per90_and_cards():
    assert abs(per90(22, 2600) - 0.7615) < 1e-3
    # 4 yellows in 2600 min -> low booking prob
    assert 0.12 < card_probability(4, 2600) < 0.15
    print("per90 / card_probability ok")


def test_split():
    club, country = split_club_country(striker_blocks(), "Brazil")
    assert len(club) == 1 and club[0]["team"]["name"] == "Real Madrid"
    assert len(country) == 1 and country[0]["team"]["name"] == "Brazil"
    print("club/country split ok")


def test_role_inference():
    central = {"goals": {"total": 22, "assists": 7}, "shots": {"total": 96}, "games": {"minutes": 2600, "appearences": 30}}
    assert infer_role("Attacker", "1:3", central) in ("ST", "SS")
    wide = {"goals": {"total": 8, "assists": 14}, "shots": {"total": 60}, "games": {"minutes": 2600, "appearences": 30}}
    assert infer_role("Attacker", "1:5", wide) == "W"
    assert infer_role("Defender", "2:5", {"games": {"minutes": 1}}) == "FB"
    assert infer_role("Defender", "2:2", {"games": {"minutes": 1}}) == "CB"
    print("role inference ok")


def test_profile_shape():
    p = build_player_profile("Test Striker", "Brazil", striker_blocks(), predicted_start_prob=0.85)
    # exact key set the terminal expects
    assert set(p) >= {"name", "team", "clubRole", "countryRole", "predictedPos",
                      "confirmedPos", "startProb", "confirmedIn", "club", "country"}
    for ctx in ("club", "country"):
        assert set(p[ctx]) == {"shots", "sot", "goals", "assists", "cards",
                               "tackles", "fouls", "fouled", "passes", "saves"}
        # blocks without count data carry None, so pricing falls back to role baselines
        assert p[ctx]["passes"] is None and p[ctx]["saves"] is None
    # club goal rate ~0.76/90, country lower ~0.42/90 — the split is preserved
    assert p["club"]["goals"] > p["country"]["goals"]
    assert p["clubRole"] in ("ST", "SS")
    print(f"profile shape ok  (club g/90={p['club']['goals']}, country g/90={p['country']['goals']})")
    return p


def test_lambdas():
    strong = {"goals": {"for": {"average": {"total": "2.4"}}, "against": {"average": {"total": "0.6"}}}}
    weak = {"goals": {"for": {"average": {"total": "0.8"}}, "against": {"average": {"total": "1.9"}}}}
    lh, la = team_lambdas(strong, weak)
    assert lh > la and lh > 2.0  # strong home side dominates
    # provider prediction overrides the strength model
    lh2, la2 = team_lambdas(strong, weak, predictions={"predictions": {"goals": {"home": "2.1", "away": "0.7"}}})
    assert abs(lh2 - 2.1) < 1e-6 and abs(la2 - 0.7) < 1e-6
    print(f"lambdas ok  (model {lh}-{la}, override {lh2}-{la2})")


# --- end-to-end build with a fake provider --------------------------------- #
class FakeProvider:
    FIXTURE = {
        "fixture": {"id": 999, "date": "2026-06-20T18:00:00+00:00", "status": {"short": "NS"}},
        "teams": {"home": {"id": 6, "name": "Brazil"}, "away": {"id": 10, "name": "Morocco"}},
        "league": {"round": "Group C"},
    }

    def fixtures(self, league, season):
        return [self.FIXTURE]

    def team_statistics(self, league, season, team):
        if team == 6:
            return {"goals": {"for": {"average": {"total": "1.9"}}, "against": {"average": {"total": "0.8"}}}}
        return {"goals": {"for": {"average": {"total": "1.1"}}, "against": {"average": {"total": "1.3"}}}}

    def predictions(self, fixture_id):
        return None

    def lineups(self, fixture_id):
        # confirmed XI released, Brazil striker starting
        return [{"team": {"name": "Brazil"}, "startXI": [{"player": {"name": "Test Striker", "pos": "F"}}]}]

    def team_players(self, team, season):
        if team == 6:
            return [{"player": {"id": 101, "name": "Test Striker"}}]
        return [{"player": {"id": 201, "name": "Away Forward"}}]

    def player_seasons(self, player_id, seasons):
        if player_id == 101:
            return striker_blocks()
        return [{
            "team": {"id": 10, "name": "Morocco", "national": True},
            "games": {"appearences": 10, "minutes": 800, "position": "Attacker", "grid": "1:3"},
            "shots": {"total": 22, "on": 9}, "goals": {"total": 5, "assists": 1}, "cards": {"yellow": 2},
        }]


def test_build_match():
    m = build_match(FakeProvider(), FakeProvider.FIXTURE, league=1, season=2026)
    assert m["home"] == "Brazil" and m["away"] == "Morocco"
    assert m["group"] == "Group C"
    assert m["xgHome"] > m["xgAway"]  # stronger home side
    assert isinstance(m["players"], list) and len(m["players"]) == 2
    striker = next(p for p in m["players"] if p["name"] == "Test Striker")
    assert striker["confirmedIn"] is True and striker["startProb"] == 1.0  # picked up from lineup
    away = next(p for p in m["players"] if p["name"] == "Away Forward")
    assert away["confirmedIn"] is False  # not in the released XI
    print(f"build_match ok  (xg {m['xgHome']}-{m['xgAway']}, {len(m['players'])} players, "
          f"striker confirmed={striker['confirmedIn']})")
    return m


if __name__ == "__main__":
    test_per90_and_cards()
    test_split()
    test_role_inference()
    prof = test_profile_shape()
    test_lambdas()
    match = test_build_match()
    print("\nALL TESTS PASSED\n")
    import json
    print("sample player object the terminal receives:")
    print(json.dumps({k: v for k, v in prof.items() if not k.startswith("_")}, indent=2))
