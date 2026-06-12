"""Count-market engine: corners, cards, shots, offsides, tackles, fouls (team)
and shots/SOT/tackles/fouls/passes/saves (player).

Mirrors the frontend engine. Counts are overdispersed, so the default is a
negative binomial (variance = mu + mu^2/r); r=None gives Poisson.

The expected counts themselves come from rate data (per-game team rates,
per-90 player rates) fetched by the data layer — this module turns those
expectations into priced markets.
"""

from __future__ import annotations

import math
from typing import Optional

# default dispersion (r) by metric — smaller = fatter tail (team-level counts).
# cards r=8: overdispersion adds mass at ZERO cards, and the book's both-teams-
# carded prices imply P(team blanks) ~9%, which a fatter tail badly overstates.
DISP = {"corners": 10, "cards": 8, "shots": 15, "sot": 8, "offsides": 4,
        "tackles": 20, "fouls": 25, "passes": 25, "saves": 6,
        "throwins": 18, "freekicks": 14, "goalkicks": 8}

# player-level dispersion, tuned to the shape of real Bet365 prop ladders:
# their prices decay near-geometrically up the ladder (a fat NB tail, r≈3-4),
# while near-Poisson r values priced 3+/4+ lines at absurd hundreds-to-one.
# Mirrors the JSX engine.
PDISP = {"shots": 3.5, "sot": 2.5, "tackles": 3.5, "fouls": 3.0,
         "fouled": 4.0, "passes": 25, "saves": 4.0}

# throw-ins: no data source exposes them and team variance is small —
# a flat per-match prior is the honest model (≈40 total per game)
THROWINS_TOTAL = 40.0
# goal kicks: awarded when attackers put the ball over the goal line, so they
# track off-target shot volume on top of a routine baseline
GOALKICKS_BASE, GOALKICKS_PER_OFFTARGET = 6.5, 0.5
# free kicks: one per foul and per offside by law, plus a few other
# infringements (handballs, obstruction)
FREEKICKS_EXTRA = 1.5


def count_dist(mu: float, r: Optional[float] = None, max_k: int = 80) -> list[float]:
    out = [0.0] * (max_k + 1)
    if r is None:
        for k in range(max_k + 1):
            out[k] = math.exp(-mu) * mu ** k / math.factorial(k)
    else:
        p = r / (r + mu)
        out[0] = p ** r
        for k in range(1, max_k + 1):
            out[k] = out[k - 1] * (k + r - 1) * (1 - p) / k
    s = sum(out) or 1.0
    return [x / s for x in out]


def over(dist: list[float], line: float) -> float:
    return sum(dist[k] for k in range(math.floor(line) + 1, len(dist)))


def at_least(dist: list[float], n: int) -> float:
    return sum(dist[k] for k in range(n, len(dist)))


def team_most(dist_h: list[float], dist_a: list[float]) -> dict:
    home = tie = 0.0
    for i, ph in enumerate(dist_h):
        for j, pa in enumerate(dist_a):
            if i > j:
                home += ph * pa
            elif i == j:
                tie += ph * pa
    return {"home": home, "away": 1 - home - tie, "tie": tie}


def _fair(outcomes: list[tuple[str, float]]) -> list[dict]:
    return [{"label": lbl, "prob": round(p, 4), "fair": round(1 / p, 2) if p > 0 else None} for lbl, p in outcomes]


def team_markets(rates: dict) -> dict:
    """rates = {"home": {corners, cards, shots, sot, offsides, tackles, fouls, redProb}, "away": {...}}."""
    h, a = rates["home"], rates["away"]
    out: dict[str, list[dict]] = {}

    def ou(metric, lines, label):
        d = count_dist(h[metric] + a[metric], DISP[metric])
        for l in lines:
            o = over(d, l)
            out[f"{label} O/U {l}"] = _fair([(f"Over {l}", o), (f"Under {l}", 1 - o)])

    def most(metric, label):
        m = team_most(count_dist(h[metric], DISP[metric]), count_dist(a[metric], DISP[metric]))
        out[f"{label} — team with most"] = _fair([("Home", m["home"]), ("Away", m["away"]), ("Tie", m["tie"])])

    ou("corners", [8.5, 9.5, 10.5, 11.5], "Corners"); most("corners", "Corners")
    ou("cards", [2.5, 3.5, 4.5], "Cards"); most("cards", "Cards")
    dhc, dac = count_dist(h["cards"], DISP["cards"]), count_dist(a["cards"], DISP["cards"])
    both = (1 - dhc[0]) * (1 - dac[0])
    out["Both teams to be carded"] = _fair([("Yes", both), ("No", 1 - both)])
    red = 1 - (1 - h.get("redProb", 0.05)) * (1 - a.get("redProb", 0.05))
    out["Red card in match"] = _fair([("Yes", red), ("No", 1 - red)])
    ou("shots", [21.5, 23.5, 25.5], "Total shots")
    ou("sot", [7.5, 8.5, 9.5], "Shots on target")
    ou("offsides", [2.5, 3.5, 4.5], "Offsides")
    ou("tackles", [15.5, 17.5], "Tackles")
    ou("fouls", [20.5, 22.5, 24.5], "Fouls")

    # derived count markets — no direct feed, modelled from the rates above
    def ou_mu(mu, key, lines, label):
        d = count_dist(mu, DISP[key])
        for l in lines:
            o = over(d, l)
            out[f"{label} O/U {l}"] = _fair([(f"Over {l}", o), (f"Under {l}", 1 - o)])

    fk_mu = h["fouls"] + a["fouls"] + h["offsides"] + a["offsides"] + FREEKICKS_EXTRA
    ou_mu(fk_mu, "freekicks", [23.5, 26.5, 29.5], "Free kicks")
    off_target = (h["shots"] - h["sot"]) + (a["shots"] - a["sot"])
    gk_mu = GOALKICKS_BASE + GOALKICKS_PER_OFFTARGET * max(0.0, off_target)
    ou_mu(gk_mu, "goalkicks", [13.5, 15.5, 17.5], "Goal kicks")
    ou_mu(THROWINS_TOTAL, "throwins", [36.5, 39.5, 42.5], "Throw-ins")
    return out


def player_markets(exp: dict) -> dict:
    """exp = expected per-match counts: {shots, sot, goals, assists, tackles,
    fouls, fouled, passes, saves, card_prob, is_gk}."""
    out: dict[str, list[dict]] = {}
    mk = lambda label, p: out.__setitem__(label, _fair([(label, max(0.002, min(0.998, p)))]))

    if exp.get("is_gk"):
        ds = count_dist(exp.get("saves", 0), PDISP["saves"])
        for n in (2, 3, 4):
            mk(f"Saves {n}+", at_least(ds, n))
        mk("To be booked", exp.get("card_prob", 0.1))
        return out

    g, asst = exp.get("goals", 0), exp.get("assists", 0)
    mk("Anytime goalscorer", 1 - math.exp(-g))
    mk("To assist", 1 - math.exp(-asst))
    mk("To score or assist", 1 - math.exp(-(g + asst)))

    # first/last goalscorer: the player's share of his team's goals times the
    # chance his team scores the first (resp. last) goal — by symmetry the two
    # probabilities match, which is also how books price them
    tx, ox = exp.get("_team_xg") or 1.3, exp.get("_opp_xg") or 1.3
    share = min(0.9, g / tx) if tx else 0.0
    p_team_first = tx / (tx + ox) * (1 - math.exp(-(tx + ox)))
    mk("First goalscorer", share * p_team_first)
    mk("Last goalscorer", share * p_team_first)

    dsh = count_dist(exp.get("shots", 0), PDISP["shots"])
    for n in range(1, 7):
        mk(f"Shots {n}+", at_least(dsh, n))
    dso = count_dist(exp.get("sot", 0), PDISP["sot"])
    for n in range(1, 5):
        mk(f"Shots on target {n}+", at_least(dso, n))
    dtk = count_dist(exp.get("tackles", 0), PDISP["tackles"])
    mk("Tackles 1+", at_least(dtk, 1)); mk("Tackles 2+", at_least(dtk, 2)); mk("Tackles 3+", at_least(dtk, 3))
    dfo = count_dist(exp.get("fouls", 0), PDISP["fouls"])
    for n in range(1, 6):
        mk(f"Fouls committed {n}+", at_least(dfo, n))
    pl = max(4.5, round(exp.get("passes", 0) / 5) * 5 - 0.5)
    mk(f"Passes over {pl}", over(count_dist(exp.get("passes", 0), PDISP["passes"]), pl))
    dfd = count_dist(exp.get("fouled", 0), PDISP["fouled"])
    for n in range(1, 5):
        mk(f"To be fouled {n}+", at_least(dfd, n))
    mk("To be booked", exp.get("card_prob", 0.1))
    # red cards run ~10-15% of bookings at this level
    mk("To be sent off", min(0.08, exp.get("card_prob", 0.1) * 0.12))
    return out


# role baselines (per 90) — mirror the frontend ROLE table; also the shrink
# target for thin per-90 samples
ROLE_COUNTS = {
    "ST": {"shots": 3.0, "sot": 1.2, "goals": 0.55, "assists": 0.2, "passes": 22, "tackles": 0.4, "fouls": 1.0, "fouled": 1.3, "saves": 0},
    "SS": {"shots": 2.4, "sot": 1.0, "goals": 0.45, "assists": 0.28, "passes": 28, "tackles": 0.6, "fouls": 1.0, "fouled": 1.2, "saves": 0},
    "W": {"shots": 2.2, "sot": 0.85, "goals": 0.35, "assists": 0.3, "passes": 28, "tackles": 0.8, "fouls": 0.9, "fouled": 1.4, "saves": 0},
    "CAM": {"shots": 1.8, "sot": 0.7, "goals": 0.3, "assists": 0.4, "passes": 38, "tackles": 1.0, "fouls": 1.0, "fouled": 1.5, "saves": 0},
    "CM": {"shots": 1.1, "sot": 0.4, "goals": 0.15, "assists": 0.22, "passes": 55, "tackles": 1.8, "fouls": 1.2, "fouled": 1.1, "saves": 0},
    "DM": {"shots": 0.7, "sot": 0.25, "goals": 0.08, "assists": 0.15, "passes": 60, "tackles": 2.5, "fouls": 1.6, "fouled": 0.9, "saves": 0},
    "FB": {"shots": 0.6, "sot": 0.2, "goals": 0.06, "assists": 0.2, "passes": 45, "tackles": 2.0, "fouls": 1.1, "fouled": 0.8, "saves": 0},
    "CB": {"shots": 0.5, "sot": 0.18, "goals": 0.07, "assists": 0.05, "passes": 50, "tackles": 1.4, "fouls": 0.9, "fouled": 0.5, "saves": 0},
    "GK": {"shots": 0.02, "sot": 0, "goals": 0.005, "assists": 0.02, "passes": 30, "tackles": 0.1, "fouls": 0.1, "fouled": 0.2, "saves": 3.0},
}
_BASE_G = 1.35


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


# minutes a non-starter plays *when he does appear* (sub ~30'), as a share of 90
_SUB_MINUTES_SHARE = 0.35


def _exposure(profile: dict, start_prob: float) -> float:
    """Void-aware expected minutes share, measured from the player's recent
    matches when the feed carries them (last5): a 'starts but comes off on
    60' player and a 15-minute super-sub stop being priced like 90-minute
    anchors. Without recent data, the structural defaults apply."""
    if start_prob <= 0:
        return 0.0
    l5 = profile.get("last5") or []
    starts = [g["minutes"] for g in l5 if (g.get("minutes") or 0) >= 60]
    subs = [g["minutes"] for g in l5 if 0 < (g.get("minutes") or 0) < 60]
    if not l5:
        start_share = 1.0  # legacy/sample path: no minutes data at all
    else:
        start_share = _clamp(sum(starts) / len(starts) / 90.0, 0.7, 1.0) if starts else 0.93
    sub_share = _clamp(sum(subs) / len(subs) / 90.0, 0.1, 0.6) if subs else _SUB_MINUTES_SHARE
    return start_prob * start_share + (1 - start_prob) * sub_share

# sample-size half-life for the club/country blend: a source with 540 measured
# minutes (~6 matches) earns half its full say
_CONF_MINUTES = 540.0


def effective_country_weight(profile: dict, country_weight: float) -> float:
    """Shrink the club/country blend toward the better-sampled source.

    The slider weight states a *preference* (international form matters more
    here); this scales each side's say by how much data backs it, so 300
    international minutes can't outvote 3000 club minutes. Without minutes
    info (sample data) the preference passes through unchanged.
    """
    mins = profile.get("_minutes") or {}
    conf = lambda m: m / (m + _CONF_MINUTES) if m else 0.0
    wc = country_weight * conf(mins.get("country", 0))
    wk = (1 - country_weight) * conf(mins.get("club", 0))
    return wc / (wc + wk) if (wc + wk) > 0 else country_weight


def player_expectations(profile: dict, pos: str, start_prob: float, team_xg: float,
                        opp_xg: float, set_pieces: Optional[dict] = None,
                        country_weight: float = 0.6,
                        team_scales: Optional[dict] = None) -> dict:
    """Expected per-match counts with opponent-strength and set-piece adjustments.

    Attacking output scales with the team's xG (opponent defence is baked into
    team_xg); defensive/discipline output scales with opponent attack (opp_xg).

    Exposure is void-aware: bookmaker player props are voided when the player
    takes no part, so the fair price is conditional on appearing. A predicted
    starter plays the full match; if he doesn't start but appears, it's sub
    minutes — multiplying raw start_prob in would systematically underprice
    every prop against the book.
    """
    attack = _clamp(team_xg / _BASE_G, 0.6, 1.7)
    defend = _clamp(opp_xg / _BASE_G, 0.6, 1.7)
    start_prob = _exposure(profile, start_prob)
    country_weight = effective_country_weight(profile, country_weight)
    sp = set_pieces or {}
    club, country = profile.get("club", {}), profile.get("country", {})

    def blend(metric, default=0.0):
        c = club.get(metric, default)
        n = country.get(metric, default) if country else c
        return country_weight * n + (1 - country_weight) * c

    rc = ROLE_COUNTS.get(pos, ROLE_COUNTS["CM"])

    # measured per-90 count rates when the feed carries them (None = the
    # source didn't record the metric); role baselines otherwise
    def measured(metric):
        c, n = club.get(metric), country.get(metric) if country else None
        if c is None and n is None:
            return rc[metric]
        c = c if c is not None else n
        n = n if n is not None else c
        return country_weight * n + (1 - country_weight) * c

    # Predictive grounding (Poisson-Gamma): the role baseline is the prior,
    # the measured per-90 tilts it in proportion to how informative the sample
    # is. Prior strength = the minutes needed to expect ~3 events at the
    # baseline rate, so rare events (a fullback's goals) need thousands of
    # minutes to outvote the prior while common ones (passes) need almost
    # none — zero goals in a thin sample stops reading as zero ability.
    # Minutes counted are those of the data the blend actually uses.
    mins = profile.get("_minutes") or {}
    eff_mins = (country_weight * (mins.get("country") or 0)
                + (1 - country_weight) * (mins.get("club") or 0))
    PRIOR_EVENTS = 3.0

    def grounded(metric, value):
        if not eff_mins:
            return value  # sample data carries no minutes info
        prior = rc.get(metric, value)
        k = PRIOR_EVENTS * 90.0 / max(prior, 0.02)
        w = eff_mins / (eff_mins + k)
        return w * value + (1 - w) * prior

    # shot-based scoring: goals are the noisiest stat in football; shots on
    # target happen ~5x more often, so SOT-rate x finishing stabilises much
    # faster than raw goals/90. Finishing shrinks toward the role's conversion
    # over ~12 observed SOT; a 30% direct-goals mix keeps genuine finishing
    # signal (penalty/free-kick duty is added separately by set_pieces).
    sot_rate = grounded("sot", blend("sot"))
    conv_prior = (rc.get("goals", 0.15) / rc["sot"]) if rc.get("sot") else 0.3
    b_sot, b_goals = blend("sot"), blend("goals")
    n_sot = b_sot * eff_mins / 90.0 if eff_mins else 0.0
    w_conv = n_sot / (n_sot + 12.0)
    conv_data = (b_goals / b_sot) if b_sot > 0 else conv_prior
    conv = _clamp(w_conv * conv_data + (1 - w_conv) * conv_prior, 0.15, 0.7)
    goals_rate = 0.7 * sot_rate * conv + 0.3 * grounded("goals", b_goals)

    # team-mass normalisation (mirrors the JSX second pass): callers that see
    # the whole squad pass scales so players share the team's goal/shot budget
    sc = team_scales or {}
    exp = {
        "goals": goals_rate * start_prob * attack * sc.get("goals", 1.0),
        "sot": grounded("sot", blend("sot")) * start_prob * attack * sc.get("sot", 1.0),
        "shots": grounded("shots", blend("shots")) * start_prob * attack * sc.get("shots", 1.0),
        "assists": grounded("assists", blend("assists")) * start_prob * attack * sc.get("assists", 1.0),
        "passes": grounded("passes", measured("passes")) * start_prob * math.sqrt(attack),
        "tackles": grounded("tackles", measured("tackles")) * start_prob * defend,
        "fouls": grounded("fouls", measured("fouls")) * start_prob * defend,
        "fouled": grounded("fouled", measured("fouled")) * start_prob * attack,
        "card_prob": blend("cards", 0.1) * defend * start_prob,
        "is_gk": pos == "GK",
        "_team_xg": team_xg,  # carried for first/last-goalscorer pricing
        "_opp_xg": opp_xg,
    }
    if pos == "GK":
        exp["saves"] = grounded("saves", measured("saves")) * start_prob

    if sp.get("pen"):
        p_pen = _clamp(0.18 * attack, 0.05, 0.4) * start_prob
        exp["goals"] += p_pen * 0.76
        exp["sot"] += p_pen
        exp["shots"] += p_pen
    if sp.get("fk"):
        exp["shots"] += 0.4 * start_prob
        exp["sot"] += 0.15 * start_prob
        exp["goals"] += 0.03 * start_prob
        exp["assists"] += 0.06 * start_prob
    return exp
