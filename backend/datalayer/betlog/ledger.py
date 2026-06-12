"""Append-only ledger of recommended bets, backed by SQLite (stdlib).

Records every recommendation with the model's probability and the price taken,
then settles it with the result and the *closing* price so we can measure
closing line value (CLV) — the fastest honest signal of real edge.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional


def odds_band(price: float) -> str:
    """Bucket decimal odds into segments the recommender reasons over."""
    if price < 1.5:
        return "<1.5"
    if price < 2.0:
        return "1.5-2.0"
    if price < 3.0:
        return "2.0-3.0"
    if price < 5.0:
        return "3.0-5.0"
    return "5.0+"


@dataclass
class Bet:
    id: str
    ts: float
    match_id: str
    market: str          # e.g. "1x2", "ou25", "player_gs", "sgm"
    selection: str       # human label
    model_prob: float    # probability the model assigned
    price: float         # decimal odds taken
    fair_odds: float
    edge: float
    stake: float
    bankroll: float
    status: str = "open"  # open | won | lost | void
    closing_price: Optional[float] = None
    pnl: Optional[float] = None
    settled_ts: Optional[float] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
  id TEXT PRIMARY KEY, ts REAL, match_id TEXT, market TEXT, selection TEXT,
  model_prob REAL, price REAL, fair_odds REAL, edge REAL, stake REAL,
  bankroll REAL, status TEXT, closing_price REAL, pnl REAL, settled_ts REAL
);
"""


class Ledger:
    def __init__(self, path: str = "bets.db"):
        parent = os.path.dirname(path)
        if parent:  # e.g. /data/bets.db on a host without a mounted volume
            os.makedirs(parent, exist_ok=True)
        # the Flask service hits this from request worker threads; one shared
        # connection guarded by a lock keeps sqlite3 happy at this scale
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.execute(SCHEMA)
            self.conn.commit()

    # -- write ----------------------------------------------------------- #
    def record(
        self,
        match_id: str,
        market: str,
        selection: str,
        model_prob: float,
        price: float,
        stake: float,
        bankroll: float,
        fair_odds: Optional[float] = None,
    ) -> str:
        bet = Bet(
            id=uuid.uuid4().hex[:12],
            ts=time.time(),
            match_id=match_id,
            market=market,
            selection=selection,
            model_prob=model_prob,
            price=price,
            fair_odds=fair_odds if fair_odds is not None else (1 / model_prob if model_prob else 0),
            edge=price * model_prob - 1,
            stake=stake,
            bankroll=bankroll,
        )
        with self._lock:
            self.conn.execute(
                "INSERT INTO bets VALUES (:id,:ts,:match_id,:market,:selection,:model_prob,"
                ":price,:fair_odds,:edge,:stake,:bankroll,:status,:closing_price,:pnl,:settled_ts)",
                bet.__dict__,
            )
            self.conn.commit()
        return bet.id

    def settle(self, bet_id: str, result: str, closing_price: Optional[float] = None) -> None:
        """result: 'won' | 'lost' | 'void'."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
            if row is None:
                raise KeyError(bet_id)
            stake, price = row["stake"], row["price"]
            if result == "won":
                pnl = stake * (price - 1)
            elif result == "lost":
                pnl = -stake
            else:  # void / push
                pnl = 0.0
            self.conn.execute(
                "UPDATE bets SET status=?, pnl=?, closing_price=?, settled_ts=? WHERE id=?",
                (result, pnl, closing_price, time.time(), bet_id),
            )
            self.conn.commit()

    # -- read ------------------------------------------------------------ #
    def settled(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM bets WHERE status IN ('won','lost')"
            ).fetchall()
        return [dict(r) for r in rows]

    def open_bets(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM bets WHERE status='open'").fetchall()
        return [dict(r) for r in rows]

    def all(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM bets").fetchall()
        return [dict(r) for r in rows]
