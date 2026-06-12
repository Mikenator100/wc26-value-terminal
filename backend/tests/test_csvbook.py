"""Structured Bet365 CSV: parsing, our-naming translation, feed merge with
placeholder injection. No network."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.csvbook import is_structured, load_structured, merge_structured_into_feed  # noqa: E402

CSV = """event,event_datetime,market_group,market,selection,player_number,player_name,line,option,odds
Canada v Bosnia-Herzegovina,13 Jun 06:00,Result,Full Time Result,Canada,,,,,1.80
Canada v Bosnia-Herzegovina,13 Jun 06:00,Result,Full Time Result,Draw,,,,,3.50
Canada v Bosnia-Herzegovina,13 Jun 06:00,Result,Full Time Result,Bosnia-Herzegovina,,,,,4.75
Canada v Bosnia-Herzegovina,13 Jun 06:00,Result,Double Chance,Draw or Bosnia-Herzegovina,,,,,1.95
Canada v Bosnia-Herzegovina,13 Jun 06:00,Result,Team To Kick Off,Canada,,,,,1.66
Canada v Bosnia-Herzegovina,13 Jun 06:00,Goals,Alternative Total Goals,Over 3.5,,,3.5,Over,4.33
Canada v Bosnia-Herzegovina,13 Jun 06:00,Corners,Alternative Corners,Exactly 8,,,8,Exactly,7.50
Canada v Bosnia-Herzegovina,13 Jun 06:00,Goals,Team Goals Range,Canada 2-4 Yes,,,2-4,Yes,2.20
Canada v Bosnia-Herzegovina,13 Jun 06:00,Half,Half Time/Full Time,Draw - Canada,,,,,4.50
Canada v Bosnia-Herzegovina,13 Jun 06:00,Goalscorers,Goalscorers,Jonathan David,10,Jonathan David,Anytime,Anytime,2.62
Canada v Bosnia-Herzegovina,13 Jun 06:00,Goalscorers,Goalscorers,Jonathan David,10,Jonathan David,First,First,5.50
Canada v Bosnia-Herzegovina,13 Jun 06:00,Player to Score or Assist,Player to Score or Assist,Jonathan David,10,Jonathan David,Assist,Assist,6.00
Canada v Bosnia-Herzegovina,13 Jun 06:00,Player Cards,Player Cards,Jonathan David,10,Jonathan David,Booked,Booked,7.50
Canada v Bosnia-Herzegovina,13 Jun 06:00,Player Cards,Player Cards,Jonathan David,10,Jonathan David,1st Card,1st Card,19.00
Canada v Bosnia-Herzegovina,13 Jun 06:00,Player Shots,Player Shots,Alistair Johnstone,2,Alistair Johnstone,1+,1+,2.50
Canada v Bosnia-Herzegovina,13 Jun 06:00,Player Shots,Player Shots,Mystery Man,99,Mystery Man,1+,1+,3.00
"""


def _write():
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
    f.write(CSV)
    f.close()
    return f.name


def test_parse():
    path = _write()
    try:
        assert is_structured(path)
        book = load_structured(path)
        assert book["home"] == "Canada" and book["away"] == "Bosnia-Herzegovina"
        jd = book["players"]["david jonathan"]
        assert jd["Anytime goalscorer"] == 2.62 and jd["First goalscorer"] == 5.5
        assert jd["To assist"] == 6.0 and jd["To be booked"] == 7.5
        assert "1st Card" not in str(jd)  # unmodelled option dropped
        mp = book["match_prices"]
        assert mp["Match result|Home"] == 1.8 and mp["Match result|Away"] == 4.75
        assert mp["Double chance|Draw or away"] == 1.95
        assert mp["Over/Under 3.5|Over 3.5"] == 4.33
        assert mp["Corners 3-way 8|Exactly 8"] == 7.5
        assert mp["Home goals 2-4|Yes"] == 2.2
        assert mp["Half-time / Full-time|Draw / Home"] == 4.5
        assert not any("Kick Off" in k for k in mp)  # coin flips dropped
        print(f"parse ok ({len(book['players'])} players, {len(mp)} match prices)")
    finally:
        os.unlink(path)


def test_merge():
    path = _write()
    try:
        book = load_structured(path)
        feed = [{
            "home": "Canada", "away": "Bosnia & Herzegovina",  # API spelling differs
            "players": [
                {"name": "Jonathan David"},
                {"name": "A. Johnston"},  # initial + slip typo ('Johnstone')
            ],
        }]
        st = merge_structured_into_feed(feed, book)
        assert st["match_found"] and st["matched_players"] == 2
        assert st["placeholders"] == 1  # Mystery Man injected
        m = feed[0]
        assert m["bookPrices"]["Match result|Home"] == 1.8
        assert m["players"][0]["bookOdds"]["Anytime goalscorer"] == 2.62
        assert m["players"][1]["bookOdds"]["Shots 1+"] == 2.5  # typo matched
        ghost = next(p for p in m["players"] if p.get("csvOnly"))
        assert ghost["name"] == "Mystery Man" and ghost["bookOdds"]["Shots 1+"] == 3.0
        assert "_name" not in ghost["bookOdds"]
        print(f"merge ok ({st})")
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_parse()
    test_merge()
    print("\nALL CSVBOOK TESTS PASSED\n")
