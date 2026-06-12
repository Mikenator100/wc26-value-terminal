"""HTTP service that makes the betting tool persistent and deployable.

Endpoints
  GET  /api/feed                 -> latest feed.json (built by the feed job)
  POST /api/bets                 -> log a bet  {match_id, market, selection, model_prob, price, stake, bankroll?}
  GET  /api/bets                 -> all bets
  POST /api/bets/<id>/settle     -> manual settle {result, closing_price?}
  POST /api/settle/auto          -> auto-settle finished fixtures + capture CLV
  GET  /api/performance          -> profitability AND hit rate, calibration, segments

Persistence is the SQLite ledger (mount the .db on a volume). The React app's
"Log bet" button POSTs to /api/bets instead of holding bets in memory.
"""

from __future__ import annotations

import os

import hmac

from flask import Flask, Response, jsonify, request, send_file

from .betlog import Ledger, performance, fit_calibrator, segment_stats, calibration_table


def create_app(db_path: str = "bets.db", feed_path: str = "feed.json",
               static_dir: str = "", access_code: str = "") -> Flask:
    app = Flask(__name__)
    ledger = Ledger(db_path)

    # optional friends-only gate: set ACCESS_CODE and the whole app (except the
    # health check) asks for it as a password — any username works
    if access_code:
        @app.before_request
        def gate():
            if request.path == "/api/health":
                return None
            auth = request.authorization
            if auth and auth.password and hmac.compare_digest(auth.password, access_code):
                return None
            return Response(
                "Enter any username and the access code as the password.",
                401, {"WWW-Authenticate": 'Basic realm="value-terminal"'})

    # serve the built terminal (frontend/dist) when present — one origin for
    # app + API, so a deploy is a single container
    sd = os.path.abspath(static_dir) if static_dir else ""
    if sd and os.path.isdir(sd):
        @app.get("/")
        def index():
            return send_file(os.path.join(sd, "index.html"))

        @app.get("/<path:asset>")
        def assets(asset):
            full = os.path.normpath(os.path.join(sd, asset))
            if full.startswith(sd + os.sep) and os.path.isfile(full):
                return send_file(full)
            return jsonify({"error": "not found"}), 404

    # the terminal is served from a different origin (dev server / static host);
    # personal tool, so a permissive CORS policy is fine
    @app.after_request
    def cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/api/feed")
    def feed():
        if os.path.exists(feed_path):
            return send_file(os.path.abspath(feed_path), mimetype="application/json")
        return jsonify([])

    @app.post("/api/bets")
    def log_bet():
        d = request.get_json(force=True)
        bid = ledger.record(
            match_id=str(d["match_id"]),
            market=d["market"],
            selection=d["selection"],
            model_prob=float(d["model_prob"]),
            price=float(d["price"]),
            stake=float(d["stake"]),
            bankroll=float(d.get("bankroll", 100)),
        )
        return jsonify({"id": bid}), 201

    @app.get("/api/bets")
    def list_bets():
        return jsonify(ledger.all())

    @app.post("/api/bets/<bid>/settle")
    def settle_bet(bid):
        d = request.get_json(force=True) or {}
        ledger.settle(bid, d["result"], d.get("closing_price"))
        return jsonify({"ok": True})

    @app.post("/api/settle/auto")
    def settle_auto():
        key = os.environ.get("API_FOOTBALL_KEY")
        if not key:
            return jsonify({"error": "API_FOOTBALL_KEY not set"}), 400
        from .providers import ApiFootballProvider, FileCache
        from .settler import Settler, ApiFootballResults

        results = ApiFootballResults(ApiFootballProvider(key, cache=FileCache()))
        out = Settler(ledger, results).settle_open()
        return jsonify(out)

    @app.get("/api/performance")
    def perf():
        settled = ledger.settled()
        m = performance(settled)
        cal = fit_calibrator(settled)
        return jsonify({
            # profitability is reported first and on equal footing with hit rate
            "profitability": {
                "pnl": m.get("pnl"),
                "roi": m.get("roi"),
                "staked": m.get("staked"),
                "avg_clv": m.get("avg_clv"),
                "pct_positive_clv": m.get("pct_positive_clv"),
            },
            "hit_rate": m.get("hit_rate"),
            "n": m.get("n"),
            "brier": m.get("brier"),
            "log_loss": m.get("log_loss"),
            "calibration_active": cal.active,
            "calibration_table": calibration_table(settled),
            "segments": segment_stats(settled),
            "open": len(ledger.open_bets()),
        })

    return app


# `flask --app datalayer.service run`  /  gunicorn 'datalayer.service:app'
app = create_app(
    db_path=os.environ.get("LEDGER_DB", "bets.db"),
    feed_path=os.environ.get("FEED_PATH", "feed.json"),
    static_dir=os.environ.get("STATIC_DIR", ""),
    access_code=os.environ.get("ACCESS_CODE", ""),
)


def _start_feed_thread() -> None:
    """In-process scheduler for single-container hosts (Render free tier has
    no background workers). Same loop as `python -m datalayer.jobs`, including
    the fast first pass that gets a feed up quickly after a cold start."""
    import threading
    from .jobs import run_loop

    threading.Thread(target=run_loop, daemon=True, name="feedjob").start()


if os.environ.get("ENABLE_FEED_JOB") and os.environ.get("API_FOOTBALL_KEY"):
    _start_feed_thread()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
