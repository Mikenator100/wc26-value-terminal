"""Bet365 player-prop prices from a hand-collected CSV (no scraping).

The CSV is pasted together by hand from the Bet365 match page: one row per
(player, market family), ladder columns like "2+ Shots on Target" holding the
decimal price. This module maps those onto the prop labels the engines price
and attaches them to feed player objects as `bookOdds`, so the terminal shows
real edge instead of waiting for a pasted price.

Name matching: exact order-insensitive key first (see lineups.name_key), then
an initial-aware pass — rosters abbreviate ("M. Sadílek") where the slip
spells it out ("Michal Sadilek").
"""

from __future__ import annotations

import csv

from .lineups import name_key

# CSV ladder column -> the prop label the engines use
COLUMN_LABELS = {
    **{f"{n}+ Shots on Target": f"Shots on target {n}+" for n in range(1, 5)},
    **{f"{n}+ Shots": f"Shots {n}+" for n in range(1, 9)},
    **{f"{n}+ Fouls": f"Fouls committed {n}+" for n in range(1, 6)},
    **{f"{n}+ Tackles": f"Tackles {n}+" for n in range(1, 6)},
}


def load_props_csv(path: str) -> dict[str, dict[str, float]]:
    """name_key -> {prop label: decimal price}."""
    out: dict[str, dict[str, float]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            nk = name_key(row.get("Player Name", ""))
            if not nk:
                continue
            prices: dict[str, float] = {}
            for col, label in COLUMN_LABELS.items():
                v = (row.get(col) or "").strip()
                if not v:
                    continue
                try:
                    price = float(v)
                except ValueError:
                    continue
                if price > 1.0:
                    prices[label] = price
            if prices:  # players whose rows held no usable price don't index
                out.setdefault(nk, {}).update(prices)
    return out


def _initials_match(a: str, b: str) -> bool:
    """'m sadilek' matches 'michal sadilek' (initials), and 'johnston' matches
    'johnstone' (slip typos: long-token prefix)."""
    ta, tb = a.split(), b.split()
    if len(ta) != len(tb):
        return False
    anchor = False  # at least one real (multi-letter) token must agree
    for x, y in zip(ta, tb):
        if x == y:
            anchor = anchor or len(x) >= 3
            continue
        if len(x) == 1 and y.startswith(x):
            continue
        if len(y) == 1 and x.startswith(y):
            continue
        if min(len(x), len(y)) >= 5 and (x.startswith(y) or y.startswith(x)):
            anchor = True
            continue
        return False
    return anchor


def lookup(props: dict[str, dict[str, float]], player_name: str) -> dict[str, float] | None:
    nk = name_key(player_name)
    if nk in props:
        return props[nk]
    for k, prices in props.items():
        if _initials_match(nk, k):
            return prices
    return None


def merge_props_into_feed(feed: list[dict], props: dict[str, dict[str, float]]) -> int:
    """Attach bookOdds to every player a CSV row matches. Returns match count."""
    matched = 0
    for match in feed:
        for p in match.get("players", []):
            prices = lookup(props, p.get("name", ""))
            if prices:
                p["bookOdds"] = prices
                matched += 1
    return matched
