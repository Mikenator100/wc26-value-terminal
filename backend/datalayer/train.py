"""Fit the goals/result model on historical football, validate on the World Cup.

The model's prediction mapping has a few free parameters that were hand-set:
  - ELO_PER_GOAL   how many Elo points equal one goal of supremacy
  - TOTAL_GOALS    the baseline match total
  - DC_RHO         the Dixon-Coles low-score correction
  - HOME_ADV       home/venue multiplier (1.0 at neutral venues)

This tool learns them properly instead of guessing:
  1. collect historical international results (the fixtures endpoint gives
     goals with no per-match stat calls — cheap)
  2. compute Elo walk-forward from a flat start, recording each match's
     PRE-match ratings (no leakage — a match is predicted before it updates
     the table)
  3. fit the parameters by minimising result log-loss on a validation split
  4. hold out the World Cup entirely as the test set and report metrics for
     the current vs the fitted parameters

Run:  python -m datalayer.train --key $API_FOOTBALL_KEY
Writes the winning parameters to model_params.json (loaded by elo.py) only
when they beat the current ones on the held-out WC test.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from typing import Optional

from .providers import ApiFootballProvider, FileCache

# historical international competitions (league id, label) — same domain as a
# World Cup: national teams, mixed venues. Goals only, so 1-2 calls each.
TRAIN_LEAGUES = [(10, "Friendlies"), (5, "Nations League"), (4, "Euro"),
                 (9, "Copa America"), (32, "WCQ Europe"), (34, "WCQ S.America"),
                 (29, "WCQ Africa"), (30, "WCQ Asia"), (31, "WCQ N.America")]
# deep enough that the past World Cups have real pre-tournament Elo
TRAIN_SEASONS = list(range(2016, 2026))
WC_LEAGUE, WC_SEASON = 1, 2026
# past World Cups: neutral-venue tournaments — the cleanest data for fitting
# favourite strength (no qualifier home-advantage confound)
PAST_WCS = [2010, 2014, 2018, 2022]

ELO_START = 1500.0
ELO_K = 40.0
MAX_GOALS = 10
CAP_D = 2.6  # max goal supremacy the rating gap can imply

# current (hand-set) params, for the baseline comparison
CURRENT = {"per_goal": 220.0, "total": 2.6, "rho": -0.13, "home_adv": 1.0}


# --------------------------------------------------------------------------- #
def _poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def matrix(lh: float, la: float, rho: float) -> list:
    ph = [_poisson(i, lh) for i in range(MAX_GOALS + 1)]
    pa = [_poisson(j, la) for j in range(MAX_GOALS + 1)]
    m = [[ph[i] * pa[j] for j in range(MAX_GOALS + 1)] for i in range(MAX_GOALS + 1)]
    # Dixon-Coles low-score tau
    m[0][0] *= 1 - lh * la * rho
    m[0][1] *= 1 + lh * rho
    m[1][0] *= 1 + la * rho
    m[1][1] *= 1 - rho
    s = sum(sum(r) for r in m) or 1.0
    return [[x / s for x in r] for r in m]


def lambdas(eh: float, ea: float, prm: dict, neutral: bool) -> tuple:
    d = max(-CAP_D, min(CAP_D, (eh - ea) / prm["per_goal"]))
    v = 1.0 if neutral else prm["home_adv"]
    lh = max(0.15, (prm["total"] + d) / 2 * v)
    la = max(0.15, (prm["total"] - d) / 2 / v)
    return lh, la


def result_probs(m: list) -> tuple:
    n = len(m)
    home = sum(m[i][j] for i in range(n) for j in range(n) if i > j)
    draw = sum(m[i][i] for i in range(n))
    return home, draw, 1 - home - draw


def over_prob(m: list, line: float) -> float:
    n = len(m)
    return sum(m[i][j] for i in range(n) for j in range(n) if i + j > line)


# --------------------------------------------------------------------------- #
def _margin_mult(diff: int) -> float:
    if diff <= 1:
        return 1.0
    if diff == 2:
        return 1.5
    return (11 + diff) / 8


def collect(provider, leagues, seasons) -> list:
    """Finished fixtures sorted by date -> [{date, h, a, gh, ga, neutral}]."""
    out = []
    for lg, _ in leagues:
        for s in seasons:
            try:
                fx = provider.fixtures(lg, s)
            except Exception:
                continue
            for f in fx:
                st = ((f.get("fixture") or {}).get("status") or {}).get("short")
                g = f.get("goals") or {}
                if st not in ("FT", "AET", "PEN") or g.get("home") is None:
                    continue
                out.append({
                    "date": (f.get("fixture") or {}).get("date") or "",
                    "h": f["teams"]["home"]["id"], "a": f["teams"]["away"]["id"],
                    "hn": f["teams"]["home"]["name"], "an": f["teams"]["away"]["name"],
                    "gh": g["home"], "ga": g["away"],
                    # friendlies (10) are treated as neutral; the rest carry venue
                    "neutral": lg in (10, 4, 9, 1),
                })
    out.sort(key=lambda r: r["date"])
    return out


def walk_forward_elo(matches: list) -> list:
    """Attach each match's PRE-match (eh, ea); update the table after. No leak."""
    elo: dict = {}
    for m in matches:
        eh = elo.get(m["h"], ELO_START)
        ea = elo.get(m["a"], ELO_START)
        m["eh"], m["ea"] = eh, ea
        exp_h = 1 / (1 + 10 ** ((ea - eh) / 400))
        score = 1.0 if m["gh"] > m["ga"] else 0.0 if m["gh"] < m["ga"] else 0.5
        delta = ELO_K * _margin_mult(abs(m["gh"] - m["ga"])) * (score - exp_h)
        elo[m["h"]] = eh + delta
        elo[m["a"]] = ea - delta
    return matches


# --------------------------------------------------------------------------- #
def metrics(matches: list, prm: dict) -> dict:
    n = len(matches)
    if not n:
        return {"n": 0}
    rll = rbrier = tot_brier = hit = 0.0
    for m in matches:
        lh, la = lambdas(m["eh"], m["ea"], prm, m["neutral"])
        mat = matrix(lh, la, prm["rho"])
        ph, pd, pa = result_probs(mat)
        gh, ga = m["gh"], m["ga"]
        aH, aD, aA = (1, 0, 0) if gh > ga else (0, 1, 0) if gh == ga else (0, 0, 1)
        rbrier += (ph - aH) ** 2 + (pd - aD) ** 2 + (pa - aA) ** 2
        pwin = ph if aH else pd if aD else pa
        rll += -math.log(min(max(pwin, 1e-9), 1))
        pick = max((ph, "h"), (pd, "d"), (pa, "a"))[1]
        actual = "h" if gh > ga else "d" if gh == ga else "a"
        hit += 1 if pick == actual else 0
        po = over_prob(mat, 2.5)
        tot_brier += (po - (1 if gh + ga > 2.5 else 0)) ** 2
    return {"n": n, "log_loss": rll / n, "brier_1x2": rbrier / n,
            "brier_ou25": tot_brier / n, "hit": hit / n}


def fit(train: list, val: list) -> dict:
    """Coordinate descent on val log-loss over the four parameters."""
    prm = dict(CURRENT)
    grids = {
        "per_goal": [160, 180, 200, 220, 250, 280, 320],
        "total": [2.3, 2.45, 2.6, 2.75, 2.9, 3.05],
        "rho": [-0.20, -0.16, -0.13, -0.10, -0.06, 0.0],
        "home_adv": [1.0, 1.05, 1.1, 1.15, 1.2],
    }
    for _ in range(3):  # a few sweeps to converge
        for key, grid in grids.items():
            best_v, best = float("inf"), prm[key]
            for cand in grid:
                trial = dict(prm); trial[key] = cand
                v = metrics(val, trial)["log_loss"]
                if v < best_v:
                    best_v, best = v, cand
            prm[key] = best
    return prm


def main() -> None:
    ap = argparse.ArgumentParser(description="Fit goals/result model; WC = test.")
    ap.add_argument("--key", default=os.environ.get("API_FOOTBALL_KEY"))
    ap.add_argument("--out", default="model_params.json")
    ap.add_argument("--write", action="store_true", help="save params even if they don't win")
    args = ap.parse_args()
    provider = ApiFootballProvider(args.key, cache=FileCache())

    hist = collect(provider, TRAIN_LEAGUES, TRAIN_SEASONS)
    past_wc = collect(provider, [(WC_LEAGUE, "World Cup")], PAST_WCS)
    wc = collect(provider, [(WC_LEAGUE, "World Cup")], [WC_SEASON])
    print(f"history: {len(hist)} | past WCs: {len(past_wc)} | WC2026 test: {len(wc)}")

    # walk-forward Elo over EVERYTHING in date order, so each match's ratings
    # come only from prior results — no leakage, past WCs included
    allm = sorted(hist + past_wc + wc, key=lambda r: r["date"])
    walk_forward_elo(allm)
    wc_ids = {id(m) for m in wc}

    # fit on NEUTRAL matches only (past WCs + Euros + Copas + friendlies),
    # excluding WC2026 — neutral venues remove the home-advantage confound
    # that made the qualifier-heavy fit want artificially strong favourites
    past_wc_ids = {id(m) for m in past_wc}
    neutral = [m for m in allm if m["neutral"] and id(m) not in wc_ids]
    wc = [m for m in allm if id(m) in wc_ids]
    cut = int(len(neutral) * 0.85)
    train, val = neutral[:cut], neutral[cut:]
    n_pw = sum(1 for m in neutral if id(m) in past_wc_ids)
    print(f"neutral fit pool {len(neutral)} (incl {n_pw} past-WC) | "
          f"train {len(train)} | val {len(val)} | test WC2026 {len(wc)}")

    fitted = fit(train, val)
    print("\nfitted params:", {k: round(v, 3) for k, v in fitted.items()})
    print("current params:", CURRENT)

    def show(tag, prm):
        for name, ds in (("val", val), ("WC-test", wc)):
            m = metrics(ds, prm)
            if m["n"]:
                print(f"  {tag:<8} {name:<8} n={m['n']:<4} logloss {m['log_loss']:.3f} "
                      f"brier1x2 {m['brier_1x2']:.3f} ou25 {m['brier_ou25']:.3f} hit {m['hit']:.0%}")
    print("\nmetrics (lower logloss/brier = better):")
    show("current", CURRENT)
    show("fitted", fitted)

    cur_wc = metrics(wc, CURRENT)["log_loss"] if wc else 1e9
    fit_wc = metrics(wc, fitted)["log_loss"] if wc else 1e9
    won = fit_wc < cur_wc
    print(f"\nfitted {'BEATS' if won else 'does NOT beat'} current on the held-out WC test "
          f"(logloss {fit_wc:.3f} vs {cur_wc:.3f})")
    if won or args.write:
        with open(args.out, "w") as f:
            json.dump(fitted, f, indent=2)
        print(f"wrote {args.out}")
    else:
        print("not writing — keeping current params (no overfit to chase)")


if __name__ == "__main__":
    main()
