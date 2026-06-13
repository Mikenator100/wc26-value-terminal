"""Validate odds normalisation + merge against a mock The Odds API v4 payload
(shape taken from the live docs). No network needed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.odds import normalize_event_markets, merge_odds_into_feed  # noqa: E402


MOCK_EVENT = {
    "id": "abc123",
    "sport_key": "soccer_fifa_world_cup",
    "commence_time": "2026-06-20T18:00:00Z",
    "home_team": "Brazil",
    "away_team": "Morocco",
    "bookmakers": [
        {
            "key": "pinnacle",
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Brazil", "price": 1.88},
                    {"name": "Morocco", "price": 4.30},
                    {"name": "Draw", "price": 3.70},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": 2.02, "point": 2.5},
                    {"name": "Under", "price": 1.82, "point": 2.5},
                    {"name": "Over", "price": 3.40, "point": 3.5},
                    {"name": "Under", "price": 1.33, "point": 3.5},
                ]},
                {"key": "btts", "outcomes": [
                    {"name": "Yes", "price": 1.86},
                    {"name": "No", "price": 1.98},
                ]},
            ],
        },
        {
            "key": "bet365",
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Brazil", "price": 1.95},
                    {"name": "Morocco", "price": 4.50},
                    {"name": "Draw", "price": 3.60},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": 2.10, "point": 2.5},
                    {"name": "Under", "price": 1.78, "point": 2.5},
                ]},
                {"key": "btts", "outcomes": [
                    {"name": "Yes", "price": 1.83},
                    {"name": "No", "price": 2.05},
                ]},
            ],
        },
    ],
}


def test_normalize():
    mk = normalize_event_markets(MOCK_EVENT)
    keys = [m["key"] for m in mk]
    assert keys == ["1x2", "ou25", "btts"], keys

    one = next(m for m in mk if m["key"] == "1x2")
    # ordered home, draw, away
    assert [o["label"] for o in one["outcomes"]] == ["Brazil", "Draw", "Morocco"]
    assert one["outcomes"][0]["bet365"] == 1.95 and one["outcomes"][0]["pinnacle"] == 1.88

    ou = next(m for m in mk if m["key"] == "ou25")
    # must lock onto the 2.5 line, not 3.5
    assert "2.5" in ou["name"]
    assert ou["outcomes"][0]["label"] == "Over 2.5"
    assert ou["outcomes"][0]["bet365"] == 2.10 and ou["outcomes"][0]["pinnacle"] == 2.02

    bt = next(m for m in mk if m["key"] == "btts")
    assert bt["outcomes"][0]["bet365"] == 1.83 and bt["outcomes"][1]["pinnacle"] == 1.98
    print("normalize ok:", keys)


def test_best_au_price():
    # bet365 has Brazil 1.95; sportsbet beats it at 2.05 -> best wins + bestBook
    ev = {k: v for k, v in MOCK_EVENT.items()}
    ev["bookmakers"] = MOCK_EVENT["bookmakers"] + [{
        "key": "sportsbet",
        "markets": [{"key": "h2h", "outcomes": [
            {"name": "Brazil", "price": 2.05},
            {"name": "Morocco", "price": 4.20},
            {"name": "Draw", "price": 3.55},
        ]}],
    }]
    one = next(m for m in normalize_event_markets(ev) if m["key"] == "1x2")
    brazil = one["outcomes"][0]
    assert brazil["bet365"] == 2.05 and brazil["bestBook"] == "sportsbet"
    # the comparison dropdown carries every book, sorted high-to-low
    assert [b["book"] for b in brazil["books"]] == ["sportsbet", "bet365"]
    assert brazil["books"][0]["price"] == 2.05 and brazil["books"][1]["price"] == 1.95
    # draw: bet365 3.60 still beats sportsbet 3.55 and pinnacle isn't retail
    draw = one["outcomes"][1]
    assert draw["bet365"] == 3.60 and draw["bestBook"] == "bet365"
    # sharp anchor untouched
    assert brazil["pinnacle"] == 1.88
    print("best AU price ok (Brazil -> sportsbet 2.05, books listed for dropdown)")


def test_sharp_fallback():
    # pinnacle missing -> should fall back to betfair for the sharp line
    ev = {
        "home_team": "X", "away_team": "Y",
        "bookmakers": [
            {"key": "bet365", "markets": [{"key": "h2h", "outcomes": [
                {"name": "X", "price": 2.0}, {"name": "Y", "price": 4.0}, {"name": "Draw", "price": 3.5}]}]},
            {"key": "betfair_ex_eu", "markets": [{"key": "h2h", "outcomes": [
                {"name": "X", "price": 2.1}, {"name": "Y", "price": 4.2}, {"name": "Draw", "price": 3.6}]}]},
        ],
    }
    mk = normalize_event_markets(ev)
    one = next(m for m in mk if m["key"] == "1x2")
    assert one["outcomes"][0]["bet365"] == 2.0      # retail kept
    assert one["outcomes"][0]["pinnacle"] == 2.1    # sharp from betfair fallback
    print("sharp fallback ok")


def test_merge():
    feed = [
        {"home": "Brazil", "away": "Morocco", "markets": []},
        {"home": "Argentina", "away": "Croatia", "markets": []},
    ]
    merge_odds_into_feed(feed, [MOCK_EVENT])
    assert len(feed[0]["markets"]) == 3, "Brazil match should be filled"
    assert feed[1]["markets"] == [], "Argentina match has no odds event"
    # orientation-independent match
    feed2 = [{"home": "Morocco", "away": "Brazil", "markets": []}]
    merge_odds_into_feed(feed2, [MOCK_EVENT])
    assert len(feed2[0]["markets"]) == 3, "should match regardless of home/away order"
    print("merge ok")


if __name__ == "__main__":
    test_normalize()
    test_best_au_price()
    test_sharp_fallback()
    test_merge()
    print("\nALL ODDS TESTS PASSED")
