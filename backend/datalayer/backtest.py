"""Replay recorded odds snapshots into a flat-stake backtest.

Strategy under test = the terminal's: blend model prob with the sharp no-vig
prob, bet 1u on an outcome the first time its Bet365 price clears the edge
threshold. The last snapshot per outcome is the closing line, so every bet
gets a CLV reading — the early honest signal — alongside settled P&L.

Pure evaluator + a CLI:
    python -m datalayer.backtest /data/history.jsonl --key $API_FOOTBALL_KEY
    python -m datalayer.backtest history.jsonl --results results.json

results JSON: {match_id: [goals_home, goals_away]}.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from typing import Optional


def _settle(row: dict, goals: tuple[int, int]) -> Optional[float]:
    """Profit on a 1u stake, or None when unsettleable. Pushes return 0."""
    gh, ga = goals
    price = row.get("bet365")
    if not price:
        return None
    market, outcome, line = row["market"], row["outcome"], row.get("line")
    if market == "1x2":
        won = {"home": gh > ga, "draw": gh == ga, "away": ga > gh}[outcome]
    elif market == "ou25":
        if line is None:
            return None
        total = gh + ga
        if total == line:
            return 0.0  # push on whole-goal lines
        won = (total > line) if outcome == "over" else (total < line)
    elif market == "btts":
        won = (gh > 0 and ga > 0) if outcome == "yes" else not (gh > 0 and ga > 0)
    else:
        return None
    return price - 1 if won else -1.0


def evaluate(snapshots: list[dict], results: dict[str, tuple[int, int]],
             edge_threshold: float = 0.02, model_weight: float = 0.3) -> dict:
    """Flat-stake replay. Returns headline metrics + per-edge-bucket breakdown."""
    series: dict[tuple, list[dict]] = defaultdict(list)
    for r in snapshots:
        series[(r["match_id"], r["market"], r["outcome"])].append(r)

    bets = []
    for key, rows in series.items():
        rows.sort(key=lambda r: r["ts"])
        closing = rows[-1]
        close_fair = (1 / closing["sharp_prob"]) if closing.get("sharp_prob") else None
        for r in rows:
            mp, sp, price = r.get("model_prob"), r.get("sharp_prob"), r.get("bet365")
            if mp is None or sp is None or not price:
                continue
            blended = model_weight * mp + (1 - model_weight) * sp
            edge = price * blended - 1
            if edge < edge_threshold:
                continue
            goals = results.get(r["match_id"])
            if goals is None:
                continue
            pnl = _settle(r, tuple(goals))
            if pnl is None:
                continue
            clv = (price / close_fair - 1) if close_fair else None
            bets.append({"edge": edge, "pnl": pnl, "clv": clv, **{k: r[k] for k in ("match_id", "market", "label", "bet365")}})
            break  # one bet per outcome: the first time the edge appears

    n = len(bets)
    if not n:
        return {"bets": 0, "note": "no qualifying bets (threshold/results coverage)"}
    pnl = sum(b["pnl"] for b in bets)
    clvs = [b["clv"] for b in bets if b["clv"] is not None]
    buckets: dict[str, list] = defaultdict(list)
    for b in bets:
        lo = int(b["edge"] * 100) // 5 * 5
        buckets[f"{lo}-{lo + 5}%"].append(b["pnl"])
    return {
        "bets": n,
        "staked": float(n),
        "pnl": round(pnl, 3),
        "roi": round(pnl / n, 4),
        "win_rate": round(sum(1 for b in bets if b["pnl"] > 0) / n, 4),
        "avg_clv": round(sum(clvs) / len(clvs), 4) if clvs else None,
        "pct_positive_clv": round(sum(1 for c in clvs if c > 0) / len(clvs), 4) if clvs else None,
        "by_edge_bucket": {k: {"n": len(v), "pnl": round(sum(v), 3)} for k, v in sorted(buckets.items())},
        "picks": bets[:50],
    }


# snapshot market key -> ledger market name, phrased so settler.resolve_outcome
# recognises it ("result" / "goal" / "both teams to score" keywords)
PAPER_MARKET_NAMES = {"1x2": "Match result", "ou25": "Total goals O/U", "btts": "Both teams to score"}


def pick_paper_bets(rows: list[dict], weight: float = 0.3, threshold: float = 0.03) -> list[dict]:
    """The terminal's strategy as an automatic paper trader: blend model with
    sharp, flag every outcome whose Bet365 price clears the edge threshold.
    Feeding these through the ledger gives the calibrator settled volume
    without staking anything."""
    picks = []
    for r in rows:
        mp, sp, price = r.get("model_prob"), r.get("sharp_prob"), r.get("bet365")
        name = PAPER_MARKET_NAMES.get(r.get("market"))
        if mp is None or sp is None or not price or not name:
            continue
        blended = weight * mp + (1 - weight) * sp
        edge = price * blended - 1
        if edge >= threshold:
            picks.append({"match_id": r["match_id"], "market": name,
                          "selection": r["label"], "model_prob": round(blended, 5),
                          "price": price, "edge": round(edge, 4)})
    return picks


def _load_results_from_api(match_ids: set[str], key: str) -> dict[str, tuple[int, int]]:
    from .providers import ApiFootballProvider, FileCache
    provider = ApiFootballProvider(key, cache=FileCache())
    out: dict[str, tuple[int, int]] = {}
    for mid in match_ids:
        try:
            fx = provider.fixture(int(mid))
        except Exception:
            continue
        if not fx:
            continue
        status = ((fx.get("fixture") or {}).get("status") or {}).get("short")
        goals = fx.get("goals") or {}
        if status in ("FT", "AET", "PEN") and goals.get("home") is not None:
            out[mid] = (goals["home"], goals["away"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest recorded odds snapshots.")
    ap.add_argument("history", help="history.jsonl written by the jobs loop")
    ap.add_argument("--results", default=None, help="JSON {match_id: [gh, ga]}; otherwise fetched with --key")
    ap.add_argument("--key", default=None, help="API-Football key for fetching results")
    ap.add_argument("--edge", type=float, default=0.02)
    ap.add_argument("--weight", type=float, default=0.3, help="model weight in the blend")
    args = ap.parse_args()

    with open(args.history) as f:
        snapshots = [json.loads(line) for line in f if line.strip()]
    if args.results:
        with open(args.results) as f:
            results = {k: tuple(v) for k, v in json.load(f).items()}
    elif args.key:
        results = _load_results_from_api({r["match_id"] for r in snapshots}, args.key)
    else:
        raise SystemExit("need --results or --key")

    report = evaluate(snapshots, results, edge_threshold=args.edge, model_weight=args.weight)
    report.pop("picks", None)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
