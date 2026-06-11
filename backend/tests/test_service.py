"""Auto-settlement + API tests. No network: fake results provider + test client."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.betlog import Ledger  # noqa: E402
from datalayer.settler import resolve_outcome, Settler  # noqa: E402
from datalayer.service import create_app  # noqa: E402


def test_resolver():
    res = {"home_goals": 2, "away_goals": 1, "home_team": "Brazil", "away_team": "Morocco",
           "scorers": ["Vinicius", "Rodrygo"]}
    assert resolve_outcome("Match result", "Brazil", res) == "won"
    assert resolve_outcome("Match result", "Draw", res) == "lost"
    assert resolve_outcome("Total goals — Over/Under 2.5", "Over 2.5", res) == "won"
    assert resolve_outcome("Total goals — Over/Under 3.5", "Over 3.5", res) == "lost"
    assert resolve_outcome("Both teams to score", "Yes", res) == "won"
    assert resolve_outcome("Anytime goalscorer", "Vinicius", res) == "won"
    assert resolve_outcome("Anytime goalscorer", "Messi", res) == "lost"
    assert resolve_outcome("Double chance", "Home or draw", res) == "won"
    assert resolve_outcome("Corners — Over 9.5", "Over 9.5", res) is None  # unsupported -> manual
    print("resolver ok")


class FakeResults:
    def result(self, match_id):
        return {"status": "FT", "home_goals": 2, "away_goals": 1,
                "home_team": "Brazil", "away_team": "Morocco", "scorers": ["Vinicius"]}


def test_settler_with_clv():
    db = tempfile.mktemp(suffix=".db")
    led = Ledger(db)
    # took 2.05 on Over 2.5; market closed at 1.95 -> positive CLV
    b1 = led.record("999", "Total goals — Over/Under 2.5", "Over 2.5", 0.55, 2.05, 5.0, 200.0)
    b2 = led.record("999", "Match result", "Morocco", 0.25, 4.5, 2.0, 200.0)  # loses

    closing = {("999", "Total goals — Over/Under 2.5", "Over 2.5"): 1.95}
    settler = Settler(led, FakeResults(), closing_lookup=lambda mid, mk, sel: closing.get((mid, mk, sel)))
    out = settler.settle_open()
    assert out["settled"] == 2, out

    rows = {r["id"]: r for r in led.all()}
    assert rows[b1]["status"] == "won" and rows[b1]["closing_price"] == 1.95
    assert rows[b2]["status"] == "lost"
    # CLV computed downstream from price/closing
    clv = rows[b1]["price"] / rows[b1]["closing_price"] - 1
    assert clv > 0
    print(f"settler ok (auto-settled 2, won-bet CLV {clv:+.3f})")


def test_api():
    db = tempfile.mktemp(suffix=".db")
    app = create_app(db_path=db, feed_path="/nonexistent.json")
    c = app.test_client()

    assert c.get("/api/health").get_json()["ok"] is True
    assert c.get("/api/feed").get_json() == []  # no feed file yet

    r = c.post("/api/bets", json={"match_id": "m1", "market": "Match result",
                                  "selection": "Brazil", "model_prob": 0.55, "price": 1.9, "stake": 5})
    assert r.status_code == 201
    bid = r.get_json()["id"]
    assert len(c.get("/api/bets").get_json()) == 1

    c.post(f"/api/bets/{bid}/settle", json={"result": "won", "closing_price": 1.8})
    perf = c.get("/api/performance").get_json()
    assert perf["n"] == 1 and perf["hit_rate"] == 1.0
    assert perf["profitability"]["pnl"] == round(5 * 0.9, 2)  # 4.5
    assert perf["profitability"]["avg_clv"] is not None
    print(f"api ok (logged+settled via HTTP; pnl={perf['profitability']['pnl']}, roi={perf['profitability']['roi']})")


if __name__ == "__main__":
    test_resolver()
    test_settler_with_clv()
    test_api()
    print("\nALL SERVICE TESTS PASSED")
