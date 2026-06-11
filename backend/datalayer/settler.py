"""Auto-settlement and closing-line capture.

When a fixture finishes, resolve every open bet on it from the final score (and
goal scorers for player markets), settle it, and record the closing price so the
betlog can compute CLV. Score-based markets resolve automatically; anything the
resolver doesn't understand is left open for manual settling.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Protocol

from .betlog import Ledger

FINISHED = {"FT", "AET", "PEN"}


def _line(selection: str) -> Optional[float]:
    m = re.search(r"(\d+\.?\d*)", selection or "")
    return float(m.group(1)) if m else None


def resolve_outcome(market: str, selection: str, result: dict) -> Optional[str]:
    """'won' | 'lost' | None (unsupported -> leave open for manual settle)."""
    hg, ag = result.get("home_goals"), result.get("away_goals")
    if hg is None or ag is None:
        return None
    home, away = result.get("home_team", ""), result.get("away_team", "")
    total = hg + ag
    mk = (market or "").lower()
    sel = (selection or "").strip()
    sl = sel.lower()

    # match result / 1X2 — only when the market is actually the result market
    if "result" in mk or sl in ("home", "draw", "away"):
        winner = home if hg > ag else away if ag > hg else "Draw"
        pick = "Draw" if sl == "draw" else home if sl == "home" else away if sl == "away" else sel
        return "won" if pick == winner else "lost"

    # goal totals over/under — guard against corners/cards/shots over/under
    if (sl.startswith("over") or sl.startswith("under")) and "goal" in mk:
        ln = _line(sel)
        if ln is None:
            return None
        return ("won" if total > ln else "lost") if sl.startswith("over") else ("won" if total < ln else "lost")

    # both teams to score
    if "both teams to score" in mk or "btts" in mk:
        yes = hg >= 1 and ag >= 1
        if sl == "yes":
            return "won" if yes else "lost"
        if sl == "no":
            return "won" if not yes else "lost"

    # double chance
    if "double chance" in mk:
        res = "home" if hg > ag else "away" if ag > hg else "draw"
        ok = {"home or draw": res in ("home", "draw"), "home or away": res in ("home", "away"),
              "draw or away": res in ("draw", "away")}.get(sl)
        return None if ok is None else ("won" if ok else "lost")

    # anytime goalscorer (needs scorers list from events)
    if "goalscorer" in mk or "to score" in mk:
        scorers = [s.lower() for s in result.get("scorers", [])]
        return "won" if sl in scorers else "lost"

    return None


class ResultsProvider(Protocol):
    def result(self, match_id: str) -> Optional[dict]: ...


class ApiFootballResults:
    """Wraps ApiFootballProvider to return a normalised fixture result."""

    def __init__(self, provider):
        self.p = provider

    def result(self, match_id: str) -> Optional[dict]:
        fx = self.p.fixture(int(match_id))
        if not fx:
            return None
        status = (fx.get("fixture", {}).get("status", {}) or {}).get("short")
        goals = fx.get("goals", {}) or {}
        scorers = []
        if status in FINISHED:
            for e in self.p.events(int(match_id)):
                if e.get("type") == "Goal" and (e.get("detail") != "Missed Penalty"):
                    name = (e.get("player", {}) or {}).get("name")
                    if name:
                        scorers.append(name)
        return {
            "status": status,
            "home_goals": goals.get("home"),
            "away_goals": goals.get("away"),
            "home_team": fx.get("teams", {}).get("home", {}).get("name", ""),
            "away_team": fx.get("teams", {}).get("away", {}).get("name", ""),
            "scorers": scorers,
        }


class Settler:
    def __init__(
        self,
        ledger: Ledger,
        results: ResultsProvider,
        closing_lookup: Optional[Callable[[str, str, str], Optional[float]]] = None,
    ):
        self.ledger = ledger
        self.results = results
        self.closing_lookup = closing_lookup

    def settle_open(self) -> dict:
        settled = skipped = 0
        for b in self.ledger.open_bets():
            res = self.results.result(b["match_id"])
            if not res or res.get("status") not in FINISHED:
                skipped += 1
                continue
            outcome = resolve_outcome(b["market"], b["selection"], res)
            if outcome is None:
                skipped += 1
                continue
            closing = None
            if self.closing_lookup:
                closing = self.closing_lookup(b["match_id"], b["market"], b["selection"])
            self.ledger.settle(b["id"], outcome, closing_price=closing)
            settled += 1
        return {"settled": settled, "skipped": skipped}
