"""Structured Bet365 export: one CSV with every market for one match.

Shape: event, event_datetime, market_group, market, selection, player_number,
player_name, line, option, odds. Produces two things, in OUR naming:

  - player prop prices  {name_key: {prop label: price}}      -> player bookOdds
  - match market prices {"<catalog market>|<outcome>": price} -> match bookPrices

The frontend prefills its inputs from both, so every CSV price lands next to a
model hit rate / fair price. Team names inside selections are rewritten to
Home/Away using the event string ("Canada v Bosnia-Herzegovina").
"""

from __future__ import annotations

import csv
import unicodedata
from typing import Optional

from .lineups import name_key
from .csvprops import lookup as props_lookup

# player market -> option -> our prop label ({n} = ladder rung)
PLAYER_OPTION_LABELS = {
    "Goalscorers": {"Anytime": "Anytime goalscorer", "First": "First goalscorer",
                    "Last": "Last goalscorer"},
    "Player to Score or Assist": {"Score": "Anytime goalscorer", "Assist": "To assist",
                                  "Score or Assist": "To score or assist"},
    "Player Shots On Target": {f"{n}+": f"Shots on target {n}+" for n in range(1, 9)},
    "Player Shots": {f"{n}+": f"Shots {n}+" for n in range(1, 9)},
    "Player Tackles": {f"{n}+": f"Tackles {n}+" for n in range(1, 9)},
    "Player Fouls Committed": {f"{n}+": f"Fouls committed {n}+" for n in range(1, 9)},
    "Player to be Fouled": {f"{n}+": f"To be fouled {n}+" for n in range(1, 9)},
    "Player Cards": {"Booked": "To be booked", "Red Card": "To be sent off"},
    # "1st Card" (first player booked) has no model -> ignored
}


def _team_norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " ").replace("-", " ")
    # "Bosnia & Herzegovina" == "Bosnia-Herzegovina" == "Bosnia and Herzegovina"
    return " ".join(t for t in s.split() if t != "and")


def is_structured(path: str) -> bool:
    with open(path, newline="", encoding="utf-8-sig") as f:
        header = f.readline()
    return "market_group" in header


def load_structured(path: str) -> dict:
    """Parse into {event_home, event_away, players, match_prices}."""
    players: dict[str, dict[str, float]] = {}
    raw_match: list[dict] = []
    home = away = ""
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not home and " v " in (row.get("event") or ""):
                home, away = [t.strip() for t in row["event"].split(" v ", 1)]
            try:
                price = float(row.get("odds") or 0)
            except ValueError:
                continue
            if price <= 1.0:
                continue
            market = (row.get("market") or "").strip()
            pname = (row.get("player_name") or "").strip()
            if market in PLAYER_OPTION_LABELS and pname:
                label = PLAYER_OPTION_LABELS[market].get((row.get("option") or "").strip())
                if label:
                    players.setdefault(name_key(pname), {}).setdefault(label, price)
                    # remember a display name for placeholder injection
                    players[name_key(pname)].setdefault("_name", pname)  # type: ignore[arg-type]
            elif market:
                raw_match.append(row)
    return {"home": home, "away": away, "players": players,
            "match_prices": _map_match_rows(raw_match, home, away)}


def _side(sel: str, home: str, away: str) -> Optional[str]:
    t = _team_norm(sel)
    if t == _team_norm(home):
        return "Home"
    if t == _team_norm(away):
        return "Away"
    if t == "draw":
        return "Draw"
    return None


def _map_match_rows(rows: list[dict], home: str, away: str) -> dict[str, float]:
    """CSV match rows -> {"<catalog market>|<outcome label>": price}."""
    out: dict[str, float] = {}

    def put(market: str, label: str, price_s: str):
        try:
            price = float(price_s)
        except (TypeError, ValueError):
            return
        if price > 1.0:
            out.setdefault(f"{market}|{label}", price)

    for r in rows:
        market, sel = r["market"].strip(), (r.get("selection") or "").strip()
        line, opt, odds = (r.get("line") or "").strip(), (r.get("option") or "").strip(), r.get("odds")

        if market == "Full Time Result":
            side = _side(sel, home, away)
            if side:
                put("Match result", side, odds)
        elif market == "Draw No Bet":
            side = _side(sel, home, away)
            if side in ("Home", "Away"):
                put("Draw no bet", side, odds)
        elif market == "Double Chance":
            parts = [p.strip() for p in sel.split(" or ")]
            sides = [_side(p, home, away) for p in parts]
            if None not in sides:
                order = {"Home": 0, "Draw": 1, "Away": 2}
                a, b = sorted(sides, key=lambda s: order[s])
                put("Double chance", f"{a} or {b.lower()}", odds)
        elif market in ("Goals Over/Under", "Alternative Total Goals"):
            if line and opt in ("Over", "Under"):
                put(f"Over/Under {line}", f"{opt} {line}", odds)
        elif market == "Both Teams To Score":
            if sel in ("Yes", "No"):
                put("Both teams to score", sel, odds)
        elif market == "Match Result/Both Teams To Score":
            side = _side(sel.split("&")[0].strip(), home, away)
            if side and opt in ("Yes", "No"):
                put("Result + BTTS", f"{side} & {opt}", odds)
        elif market == "Half Time/Full Time":
            parts = [p.strip() for p in sel.split(" - ")]
            if len(parts) == 2:
                ht, ft = _side(parts[0], home, away), _side(parts[1], home, away)
                if ht and ft:
                    put("Half-time / Full-time", f"{ht} / {ft}", odds)
        elif market in ("Corners", "Alternative Corners"):
            if line and opt in ("Over", "Exactly", "Under"):
                put(f"Corners 3-way {line}", f"{opt} {line}", odds)
        elif market == "Match Goals Range":
            if line and opt in ("Yes", "No"):
                put(f"Goals {line}", opt, odds)
        elif market == "Team Goals Range":
            side = _side(sel.split(line)[0].strip() if line else "", home, away)
            if side in ("Home", "Away") and opt in ("Yes", "No"):
                put(f"{side} goals {line}", opt, odds)
        elif market == "2nd Half Goals Range":
            if line and opt in ("Yes", "No"):
                put(f"2nd half goals {line}", opt, odds)
        elif market == "Results/Goals Range":
            side = _side(sel.split("&")[0].strip(), home, away)
            if side and line and opt in ("Yes", "No"):
                put(f"Result + goals {line}", f"{side} & {opt}", odds)
        elif market == "Double Chance/Goals Range":
            dc = sel.split("&")[0].strip()
            parts = [p.strip() for p in dc.split(" or ")]
            sides = [_side(p, home, away) for p in parts]
            if None not in sides and line and opt in ("Yes", "No"):
                order = {"Home": 0, "Draw": 1, "Away": 2}
                a, b = sorted(sides, key=lambda s: order[s])
                put(f"Double chance + goals {line}", f"{a} or {b.lower()} & {opt}", odds)
        elif market == "Both Teams to Receive Cards":
            if opt in ("Yes", "No"):
                put("Both teams to be carded", opt, odds)
        # Team To Kick Off: a coin flip, no model -> ignored

    return out


def merge_structured_into_feed(feed: list[dict], book: dict) -> dict:
    """Attach player bookOdds + match bookPrices to the matching fixture.

    Every CSV player must end up in the feed: players the roster fetch missed
    get a placeholder profile (role baselines price their ladders)."""
    stats = {"matched_players": 0, "placeholders": 0, "match_found": False}
    th, ta = _team_norm(book["home"]), _team_norm(book["away"])
    for match in feed:
        if {_team_norm(match.get("home", "")), _team_norm(match.get("away", ""))} != {th, ta}:
            continue
        stats["match_found"] = True
        if book["match_prices"]:
            match["bookPrices"] = book["match_prices"]

        used_keys: set[str] = set()
        for p in match.get("players", []):
            prices = props_lookup(book["players"], p.get("name", ""))
            if prices:
                p["bookOdds"] = {k: v for k, v in prices.items() if not k.startswith("_")}
                used_keys.add(next(k for k, v in book["players"].items() if v is prices))
                stats["matched_players"] += 1

        for nk, prices in book["players"].items():
            if nk in used_keys:
                continue
            # the slip doesn't say which side a player is on, so placeholders
            # carry team "" — the terminal shows them under their own tab
            display = prices.get("_name") or nk.title()
            match.setdefault("players", []).append({
                "name": display, "team": "",
                "clubRole": "CM", "countryRole": "CM", "predictedPos": "CM",
                "confirmedPos": "CM", "startProb": 0.4, "confirmedIn": False,
                "club": {k: None for k in ("shots", "sot", "goals", "assists", "cards",
                                           "tackles", "fouls", "fouled", "passes", "saves")},
                "country": {k: None for k in ("shots", "sot", "goals", "assists", "cards",
                                              "tackles", "fouls", "fouled", "passes", "saves")},
                "csvOnly": True,
                "bookOdds": {k: v for k, v in prices.items() if not k.startswith("_")},
            })
            stats["placeholders"] += 1
        break
    return stats
