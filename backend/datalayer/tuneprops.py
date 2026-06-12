"""Measure model-vs-book bias per prop family, from a built feed.

The props CSV is the only real Bet365 prop data we have, so it doubles as the
tuning signal: for every player the CSV matched (bookOdds on the feed), price
the same ladders with the Python engine and compare. A family whose ratio sits
far from 1.0 has a systematic engine bias worth fixing (rates level, exposure,
or dispersion shape) — that's a model edit, not an automatic fudge, so this
tool reports rather than adjusts.

    python -m datalayer.tuneprops feed.json [--margin 1.06] [--country-weight 0.6]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict

from .countmarkets import player_expectations, player_markets

FAMILIES = ("Shots on target", "Shots", "Tackles", "Fouls committed", "Saves")


def _family(label: str) -> str:
    for f in FAMILIES:
        if label.startswith(f):
            return f
    return "Other"


def _team_scales(match: dict, country_weight: float) -> dict:
    """Mirror of the terminal's team-mass normalisation: per-team scale
    factors so the squad shares the match's goal/shot budget."""
    sums: dict[str, dict[str, float]] = {}
    for p in match.get("players", []):
        team = p.get("team")
        if not team:
            continue
        is_home = team == match.get("home")
        exp = player_expectations(
            p, p.get("predictedPos", "CM"), p.get("startProb", 0.7),
            team_xg=match["xgHome"] if is_home else match["xgAway"],
            opp_xg=match["xgAway"] if is_home else match["xgHome"],
            country_weight=country_weight,
        )
        t = sums.setdefault(team, {"goals": 0, "assists": 0, "shots": 0, "sot": 0})
        for k in t:
            t[k] += exp.get(k, 0)

    out: dict[str, dict[str, float]] = {}
    clamp = lambda x: max(0.25, min(1.5, x))
    for team, t in sums.items():
        is_home = team == match.get("home")
        xg = match["xgHome"] if is_home else match["xgAway"]
        rates = (match.get("teamRates") or {}).get("home" if is_home else "away")
        a_f = max(0.6, min(1.7, xg / 1.35))
        out[team] = {
            "goals": clamp(xg / t["goals"]) if t["goals"] else 1.0,
            "assists": clamp(0.8 * xg / t["assists"]) if t["assists"] else 1.0,
            "shots": clamp(rates["shots"] * a_f / t["shots"]) if rates and t["shots"] else 1.0,
            "sot": clamp(rates["sot"] * a_f / t["sot"]) if rates and t["sot"] else 1.0,
        }
    return out


def report(feed: list[dict], margin: float = 1.06, country_weight: float = 0.6) -> dict:
    rows = defaultdict(list)
    for match in feed:
        scales = _team_scales(match, country_weight)
        for p in match.get("players", []):
            book = p.get("bookOdds")
            if not book:
                continue
            is_home = p.get("team") == match.get("home")
            exp = player_expectations(
                p, p.get("predictedPos", "CM"), p.get("startProb", 0.7),
                team_xg=match["xgHome"] if is_home else match["xgAway"],
                opp_xg=match["xgAway"] if is_home else match["xgHome"],
                set_pieces={"pen": p.get("pen"), "fk": p.get("fk")},
                country_weight=country_weight,
                team_scales=scales.get(p.get("team")),
            )
            fair = player_markets(exp)
            for label, price in book.items():
                if label not in fair or not price or price <= 1:
                    continue
                model_p = fair[label][0]["prob"]
                book_p = min(0.99, 1 / (price * margin))  # rough margin strip
                rows[_family(label)].append((model_p, book_p))

    out = {}
    for fam, pairs in sorted(rows.items()):
        mp = sum(a for a, _ in pairs) / len(pairs)
        bp = sum(b for _, b in pairs) / len(pairs)
        out[fam] = {"n": len(pairs), "model_avg": round(mp, 4),
                    "book_avg": round(bp, 4),
                    "ratio": round(mp / bp, 3) if bp else None}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Model-vs-book prop bias per family.")
    ap.add_argument("feed")
    ap.add_argument("--margin", type=float, default=1.06)
    ap.add_argument("--country-weight", type=float, default=0.6)
    args = ap.parse_args()
    with open(args.feed) as f:
        feed = json.load(f)
    rep = report(feed, margin=args.margin, country_weight=args.country_weight)
    if not rep:
        print("no players with bookOdds in this feed — collect a props CSV for an upcoming match")
        return
    print(f"{'family':<18} {'n':>4} {'model':>8} {'book':>8} {'ratio':>7}")
    for fam, r in rep.items():
        print(f"{fam:<18} {r['n']:>4} {r['model_avg']:>8.4f} {r['book_avg']:>8.4f} {r['ratio']:>7}")
    print("\nratio < 1: model prices the family below the book (too pessimistic); > 1: above.")


if __name__ == "__main__":
    main()
