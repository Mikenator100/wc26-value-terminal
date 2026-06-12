"""CSV prop-price ingestion: ladder parsing, label mapping, name matching
(exact, order-insensitive, and initial-aware), and the feed merge."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.csvprops import load_props_csv, lookup, merge_props_into_feed  # noqa: E402

CSV = """Player Name,Market,1+ Shots on Target,2+ Shots on Target,3+ Shots on Target,4+ Shots on Target,1+ Shots,2+ Shots,1+ Fouls,2+ Fouls,1+ Tackles,2+ Tackles
Heung-Min Son,Shots on Target,1.4,2.75,8.0,21.0,,,,,,
Heung-Min Son,Total Shots,,,,,1.03,1.18,,,,
Michal Sadilek,Tackles,,,,,,,,,1.2,1.83
Patrik Schick,Fouls Committed,,,,,,,1.2,1.9,,
Bad Row,Junk,,not-a-price,0.5,,,,,,,
"""


def _write_csv():
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
    f.write(CSV)
    f.close()
    return f.name


def test_load_and_lookup():
    path = _write_csv()
    try:
        props = load_props_csv(path)
        # rows for the same player merge (SOT row + shots row)
        son = lookup(props, "Son Heung-Min")  # roster order differs from CSV
        assert son["Shots on target 1+"] == 1.4 and son["Shots on target 2+"] == 2.75
        assert son["Shots 1+"] == 1.03 and son["Shots 2+"] == 1.18
        # initial-aware: roster abbreviates, CSV spells out (and accents differ)
        sad = lookup(props, "M. Sadílek")
        assert sad and sad["Tackles 1+"] == 1.2 and sad["Tackles 2+"] == 1.83
        assert lookup(props, "P. Schick")["Fouls committed 2+"] == 1.9
        # initials must anchor on a real surname token
        assert lookup(props, "M. Different") is None
        # junk values dropped: non-numeric and prices <= 1.0
        bad = lookup(props, "Bad Row")
        assert bad is None  # nothing valid survived
        print("load/lookup ok (Son SOT1+ 1.4, Sadílek via initials)")
    finally:
        os.unlink(path)


def test_merge_into_feed():
    path = _write_csv()
    try:
        props = load_props_csv(path)
        feed = [{"players": [
            {"name": "Son Heung-Min"},
            {"name": "M. Sadílek"},
            {"name": "Kim Seung-Gyu"},  # not in CSV
        ]}]
        n = merge_props_into_feed(feed, props)
        assert n == 2
        assert feed[0]["players"][0]["bookOdds"]["Shots on target 1+"] == 1.4
        assert "bookOdds" not in feed[0]["players"][2]
        print(f"merge ok ({n} players matched)")
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_load_and_lookup()
    test_merge_into_feed()
    print("\nALL CSV-PROPS TESTS PASSED\n")
