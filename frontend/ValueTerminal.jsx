import { useState, useMemo, useEffect } from "react";

/* ================================================================== *
 *  WORLD CUP 2026 — VALUE TERMINAL  (v2: + Same Game Multis)
 *
 *  Engine
 *  ------
 *  Each match is modelled as a Poisson SCORE MATRIX from two expected
 *  -goals inputs (xG home / away). Every market AND every same-game
 *  multi is computed by summing the matrix cells where all selected
 *  legs are true — so correlation between legs is captured exactly,
 *  which is the whole point of an SGM.
 *
 *  For every bet we surface BOTH:
 *    HIT RATE  – true probability it lands (from the model)
 *    VALUE     – expected-value edge vs the bookmaker price
 *
 *  Single-market fair odds also blend in the sharp no-vig market line.
 *  Live mode: App fetches the feed from datalayer.service (see API_BASE)
 *  and falls back to SAMPLE_MATCHES; the maths downstream is unchanged.
 * ================================================================== */

/* ---------- core maths ---------- */
const impliedProb = (o) => 1 / o;
const fact = (n) => (n <= 1 ? 1 : n * fact(n - 1));
const poisson = (k, l) => (Math.exp(-l) * Math.pow(l, k)) / fact(k);

/* ---------- count-market engine (corners, cards, shots, player props) ----------
 * Counts like corners/cards are overdispersed, so we use a negative binomial
 * (variance = mu + mu^2/r); r = Infinity collapses to Poisson. */
function countDist(mu, r = Infinity, max = 80) {
  const out = [];
  if (!isFinite(r)) {
    for (let k = 0; k <= max; k++) out[k] = poisson(k, mu);
  } else {
    const p = r / (r + mu);
    out[0] = Math.pow(p, r);
    for (let k = 1; k <= max; k++) out[k] = (out[k - 1] * (k + r - 1) * (1 - p)) / k;
  }
  const s = out.reduce((a, b) => a + b, 0) || 1;
  return out.map((x) => x / s);
}
const distOver = (dist, line) => {
  let s = 0;
  for (let k = Math.floor(line) + 1; k < dist.length; k++) s += dist[k];
  return s;
};
const distAtLeast = (dist, n) => {
  let s = 0;
  for (let k = n; k < dist.length; k++) s += dist[k];
  return s;
};
const atLeast = (n, mu, r = Infinity) => distAtLeast(countDist(mu, r), n);
function teamMost(distH, distA) {
  let home = 0, tie = 0;
  for (let i = 0; i < distH.length; i++)
    for (let j = 0; j < distA.length; j++) {
      const p = distH[i] * distA[j];
      if (i > j) home += p;
      else if (i === j) tie += p;
    }
  return { home, away: 1 - home - tie, tie };
}

// Dixon-Coles low-score correction: independent Poisson misprices draws and
// low-scoring games; tau reweights the 0-0/1-0/0-1/1-1 cells (rho < 0 adds
// mass to 0-0 and 1-1). Mirrors the Python engine in datalayer/snapshots.py.
const DC_RHO = -0.13;
function dcTau(i, j, lh, la, rho = DC_RHO) {
  if (i === 0 && j === 0) return 1 - lh * la * rho;
  if (i === 0 && j === 1) return 1 + lh * rho;
  if (i === 1 && j === 0) return 1 + la * rho;
  if (i === 1 && j === 1) return 1 - rho;
  return 1;
}

function scoreMatrix(lh, la, n = 8) {
  const m = [];
  let total = 0;
  for (let i = 0; i <= n; i++) {
    m[i] = [];
    for (let j = 0; j <= n; j++) {
      const p = poisson(i, lh) * poisson(j, la) * dcTau(i, j, lh, la);
      m[i][j] = p;
      total += p;
    }
  }
  for (let i = 0; i <= n; i++)
    for (let j = 0; j <= n; j++) m[i][j] /= total; // renormalise tail + tau
  return m;
}

// probability mass of a predicate over the score matrix
function probOf(matrix, pred) {
  let s = 0;
  for (let i = 0; i < matrix.length; i++)
    for (let j = 0; j < matrix[i].length; j++)
      if (pred(i, j)) s += matrix[i][j];
  return s;
}

// strip the bookmaker margin. Proportional division spreads the vig evenly,
// which overstates longshots (the favourite-longshot bias); the power method
// (raise implied probs to k>=1 so they sum to 1) pushes the margin onto the
// outcomes where books actually park it. Mirrors datalayer/snapshots.py.
function noVigProbs(oddsArr) {
  const raw = oddsArr.map(impliedProb);
  const sum = raw.reduce((a, b) => a + b, 0);
  if (sum <= 1) return raw.map((p) => p / sum); // arb/odd input: plain rescale
  let lo = 1, hi = 5;
  for (let it = 0; it < 60; it++) {
    const k = (lo + hi) / 2;
    raw.reduce((a, p) => a + Math.pow(p, k), 0) > 1 ? (lo = k) : (hi = k);
  }
  const adj = raw.map((p) => Math.pow(p, (lo + hi) / 2));
  const s = adj.reduce((a, b) => a + b, 0);
  return adj.map((p) => p / s);
}
const margin = (oddsArr) =>
  oddsArr.map(impliedProb).reduce((a, b) => a + b, 0) - 1;
const edge = (o, p) => o * p - 1;
const kelly = (o, p) => {
  const e = edge(o, p);
  return e <= 0 ? 0 : e / (o - 1);
};

/* ---------- leg catalogue (predicates: i=home goals, j=away) ---------- */
const LEGS = {
  result_home: { group: "result", label: "Home win", pred: (i, j) => i > j },
  result_draw: { group: "result", label: "Draw", pred: (i, j) => i === j },
  result_away: { group: "result", label: "Away win", pred: (i, j) => i < j },
  dc_1x: { group: "result", label: "Home or draw", pred: (i, j) => i >= j },
  dc_x2: { group: "result", label: "Away or draw", pred: (i, j) => i <= j },
  over15: { group: "totals", label: "Over 1.5", pred: (i, j) => i + j >= 2 },
  over25: { group: "totals", label: "Over 2.5", pred: (i, j) => i + j >= 3 },
  under25: { group: "totals", label: "Under 2.5", pred: (i, j) => i + j <= 2 },
  over35: { group: "totals", label: "Over 3.5", pred: (i, j) => i + j >= 4 },
  btts_yes: { group: "btts", label: "BTTS yes", pred: (i, j) => i >= 1 && j >= 1 },
  btts_no: { group: "btts", label: "BTTS no", pred: (i, j) => i < 1 || j < 1 },
  home_cs: { group: "cs", label: "Home clean sheet", pred: (i, j) => j === 0 },
  away_cs: { group: "cs", label: "Away clean sheet", pred: (i, j) => i === 0 },
};

// map a leg to a real Bet365 price when one exists, else null
function legBet365Odds(legId, match) {
  const mk = (k) => match.markets.find((m) => m.key === k);
  const r = mk("1x2"),
    t = mk("ou25"),
    b = mk("btts");
  const map = {
    result_home: r?.outcomes[0].bet365,
    result_draw: r?.outcomes[1].bet365,
    result_away: r?.outcomes[2].bet365,
    over25: t?.outcomes[0].bet365,
    under25: t?.outcomes[1].bet365,
    btts_yes: b?.outcomes[0].bet365,
    btts_no: b?.outcomes[1].bet365,
  };
  return map[legId] ?? null;
}


/* ---------- player-prop engine ---------- */
// typical per-90 output by role; used to rescale a player's rates when the
// position they play in THIS match differs from where their rates were measured
const ROLE = {
  ST: { shots: 3.0, sot: 1.2, goals: 0.55, assists: 0.2, cards: 0.12, passes: 22, tackles: 0.4, fouls: 1.0, fouled: 1.3, saves: 0 },
  SS: { shots: 2.4, sot: 1.0, goals: 0.45, assists: 0.28, cards: 0.13, passes: 28, tackles: 0.6, fouls: 1.0, fouled: 1.2, saves: 0 },
  W: { shots: 2.2, sot: 0.85, goals: 0.35, assists: 0.3, cards: 0.14, passes: 28, tackles: 0.8, fouls: 0.9, fouled: 1.4, saves: 0 },
  CAM: { shots: 1.8, sot: 0.7, goals: 0.3, assists: 0.4, cards: 0.15, passes: 38, tackles: 1.0, fouls: 1.0, fouled: 1.5, saves: 0 },
  CM: { shots: 1.1, sot: 0.4, goals: 0.15, assists: 0.22, cards: 0.2, passes: 55, tackles: 1.8, fouls: 1.2, fouled: 1.1, saves: 0 },
  DM: { shots: 0.7, sot: 0.25, goals: 0.08, assists: 0.15, cards: 0.28, passes: 60, tackles: 2.5, fouls: 1.6, fouled: 0.9, saves: 0 },
  FB: { shots: 0.6, sot: 0.2, goals: 0.06, assists: 0.2, cards: 0.24, passes: 45, tackles: 2.0, fouls: 1.1, fouled: 0.8, saves: 0 },
  CB: { shots: 0.5, sot: 0.18, goals: 0.07, assists: 0.05, cards: 0.22, passes: 50, tackles: 1.4, fouls: 0.9, fouled: 0.5, saves: 0 },
  GK: { shots: 0.02, sot: 0, goals: 0.005, assists: 0.02, cards: 0.06, passes: 30, tackles: 0.1, fouls: 0.1, fouled: 0.2, saves: 3.0 },
};
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

// price one player's props given country/club blend, lineup status, and match
// context (opponent strength via team/opponent xG, plus set-piece duty)
function priceProps(p, countryWeight, lineupStatus, ctx = {}) {
  const confirmed = lineupStatus === "confirmed";
  const startProb = confirmed ? (p.confirmedIn ? 1 : 0) : p.startProb;
  const pos = confirmed ? p.confirmedPos || p.predictedPos : p.predictedPos;

  // shrink the club/country blend toward the better-sampled source (mirrors
  // the Python engine): the slider states a preference, the data earns its
  // say — 300 international minutes can't outvote 3000 club minutes
  if (p._minutes) {
    const conf = (m) => (m ? m / (m + 540) : 0);
    const wc = countryWeight * conf(p._minutes.country);
    const wk = (1 - countryWeight) * conf(p._minutes.club);
    if (wc + wk > 0) countryWeight = wc / (wc + wk);
  }
  const posChanged =
    confirmed && p.confirmedPos && p.confirmedPos !== p.predictedPos;

  // rescale each source rate from its measured role to the match role
  const f = (metric, fromRole) => ROLE[pos][metric] / ROLE[fromRole][metric];
  const blend = (metric) =>
    countryWeight * (p.country[metric] * f(metric, p.countryRole)) +
    (1 - countryWeight) * (p.club[metric] * f(metric, p.clubRole));

  // opponent strength: attacking output scales with the team's xG vs baseline,
  // defensive/discipline output with how much the team is under pressure
  const BASE_G = 1.35;
  const attackF = clamp((ctx.teamXg ?? BASE_G) / BASE_G, 0.6, 1.7);
  const defenceF = clamp((ctx.oppXg ?? BASE_G) / BASE_G, 0.6, 1.7);

  // measured per-90 count rates when the feed carries them (null/absent =
  // the source didn't record the metric); role baselines otherwise — keeps
  // the sample data and thin international samples working unchanged
  const measured = (metric) => {
    const c = p.club?.[metric], n = p.country?.[metric];
    if (c == null && n == null) return ROLE[pos][metric];
    const cv = c ?? n, nv = n ?? c;
    return countryWeight * nv + (1 - countryWeight) * cv;
  };

  // void-aware exposure (mirrors the Python engine): book props void when the
  // player takes no part, so fair prices are conditional on appearing — a
  // non-starter who does appear plays sub minutes (~0.35 of a match). Raw
  // startProb would systematically underprice every prop against the book.
  const expo = startProb <= 0 ? 0 : startProb + (1 - startProb) * 0.35;

  let expGoals = blend("goals") * expo * attackF;
  let expSot = blend("sot") * expo * attackF;
  let expShots = blend("shots") * expo * attackF;
  let expAssists = blend("assists") * expo * attackF;
  const expPasses = measured("passes") * expo * Math.sqrt(attackF);
  const expTackles = measured("tackles") * expo * defenceF;
  const expFouls = measured("fouls") * expo * defenceF;
  const expFouled = measured("fouled") * expo * attackF;
  const expSaves = measured("saves") * expo;
  const cardFactor = Math.sqrt(ROLE[pos].cards / ROLE[p.clubRole].cards);
  let cardP =
    (countryWeight * p.country.cards + (1 - countryWeight) * p.club.cards) *
    cardFactor * defenceF * expo;

  // set-piece duty: penalties and direct free kicks add to the taker's numbers
  const pPen = clamp(0.18 * attackF, 0.05, 0.4) * expo; // chance team wins a pen
  if (p.pen) {
    expGoals += pPen * 0.76; // ~76% conversion
    expSot += pPen;
    expShots += pPen;
  }
  if (p.fk) {
    expShots += 0.4 * expo;
    expSot += 0.15 * expo;
    expGoals += 0.03 * expo;
    expAssists += 0.06 * expo;
  }

  const mk = (name, prob) => {
    const pr = clamp(prob, 0.002, 0.998);
    return { name, hitRate: pr, fairOdds: 1 / pr };
  };

  let props;
  if (pos === "GK") {
    props = [
      mk("Saves 2+", atLeast(2, expSaves, 6)),
      mk("Saves 3+", atLeast(3, expSaves, 6)),
      mk("Saves 4+", atLeast(4, expSaves, 6)),
      mk("To be booked", cardP),
    ];
  } else {
    const passLine = Math.max(4.5, Math.round(expPasses / 5) * 5 - 0.5);
    props = [
      mk("Anytime goalscorer", 1 - Math.exp(-expGoals)),
      mk("To score or assist", 1 - Math.exp(-(expGoals + expAssists))),
      mk("Shots 1+", atLeast(1, expShots, 8)),
      mk("Shots 2+", atLeast(2, expShots, 8)),
      mk("Shots 3+", atLeast(3, expShots, 8)),
      mk("Shots on target 1+", atLeast(1, expSot, 6)),
      mk("Shots on target 2+", atLeast(2, expSot, 6)),
      mk("Shots on target 3+", atLeast(3, expSot, 6)),
      mk("Tackles 1+", atLeast(1, expTackles, 10)),
      mk("Tackles 2+", atLeast(2, expTackles, 10)),
      mk("Tackles 3+", atLeast(3, expTackles, 10)),
      mk("Fouls committed 1+", atLeast(1, expFouls, 12)),
      mk("Fouls committed 2+", atLeast(2, expFouls, 12)),
      mk(`Passes over ${passLine}`, distOver(countDist(expPasses, 25), passLine)),
      mk("To be fouled 1+", atLeast(1, expFouled, 10)),
      mk("To be booked", cardP),
    ];
  }
  return {
    startProb, pos, posChanged, inXI: startProb > 0, pen: !!p.pen, fk: !!p.fk, props,
    // expected counts, exposed for the SGM builder's conditional repricing
    exp: { goals: expGoals, sot: expSot, shots: expShots },
  };
}

// collapsible market families inside a player card; statKey links a family to
// the feed's last-5 per-match counts (shots/sot/tackles/fouls only)
const PROP_FAMILIES = [
  { name: "Scoring", match: (n) => n === "Anytime goalscorer" || n === "To score or assist" },
  { name: "Shots", match: (n) => /^Shots \d/.test(n), statKey: "shots" },
  { name: "Shots on target", match: (n) => n.startsWith("Shots on target"), statKey: "sot" },
  { name: "Tackles", match: (n) => n.startsWith("Tackles"), statKey: "tackles" },
  { name: "Fouls committed", match: (n) => n.startsWith("Fouls committed"), statKey: "fouls" },
  { name: "Saves", match: (n) => n.startsWith("Saves") },
  { name: "Other", match: () => true },
];

function groupProps(props) {
  const used = new Set();
  return PROP_FAMILIES.map((f) => ({
    ...f,
    props: props.filter((pr) => {
      if (used.has(pr.name) || !f.match(pr.name)) return false;
      used.add(pr.name);
      return true;
    }),
  })).filter((f) => f.props.length);
}

/* ---------- sample feed (replace with adapter) ---------- */
const SAMPLE_MATCHES = [
  {
    id: "wc26-bra-mar",
    home: "Brazil",
    away: "Morocco",
    group: "Group C",
    kickoff: "Live · 58'",
    live: true,
    xgHome: 1.7,
    xgAway: 1.0,
    teamRates: {
      home: { corners: 6.0, cards: 1.6, shots: 14, sot: 5.2, offsides: 1.8, tackles: 16, fouls: 11, redProb: 0.05 },
      away: { corners: 4.2, cards: 2.0, shots: 9, sot: 3.2, offsides: 1.4, tackles: 18, fouls: 13, redProb: 0.06 },
    },
    players: [
      { name: "Alisson", team: "Brazil", clubRole: "GK", countryRole: "GK", predictedPos: "GK", confirmedPos: "GK", startProb: 0.97, confirmedIn: true,
        club: { shots: 0.02, sot: 0, goals: 0.005, assists: 0.02, cards: 0.05, saves: 3.1 }, country: { shots: 0.02, sot: 0, goals: 0.005, assists: 0.02, cards: 0.05, saves: 2.7 } },
      { name: "Vinícius Jr", team: "Brazil", clubRole: "W", countryRole: "W", predictedPos: "W", confirmedPos: "W", startProb: 0.92, confirmedIn: true,
        club: { shots: 3.4, sot: 1.3, goals: 0.62, assists: 0.35, cards: 0.14 }, country: { shots: 2.8, sot: 1.0, goals: 0.45, assists: 0.30, cards: 0.12 } },
      { name: "Rodrygo", team: "Brazil", clubRole: "W", countryRole: "SS", predictedPos: "SS", confirmedPos: "ST", startProb: 0.80, confirmedIn: true,
        club: { shots: 2.2, sot: 0.9, goals: 0.40, assists: 0.28, cards: 0.10 }, country: { shots: 1.9, sot: 0.8, goals: 0.33, assists: 0.24, cards: 0.09 } },
      { name: "Raphinha", team: "Brazil", clubRole: "W", countryRole: "W", predictedPos: "W", confirmedPos: "W", startProb: 0.85, confirmedIn: true, pen: true, fk: true,
        club: { shots: 2.6, sot: 1.0, goals: 0.45, assists: 0.40, cards: 0.15 }, country: { shots: 2.3, sot: 0.9, goals: 0.38, assists: 0.33, cards: 0.14 } },
      { name: "Marquinhos", team: "Brazil", clubRole: "CB", countryRole: "CB", predictedPos: "CB", confirmedPos: "CB", startProb: 0.90, confirmedIn: true,
        club: { shots: 0.4, sot: 0.15, goals: 0.06, assists: 0.04, cards: 0.18 }, country: { shots: 0.35, sot: 0.12, goals: 0.05, assists: 0.03, cards: 0.16 } },
      { name: "En-Nesyri", team: "Morocco", clubRole: "ST", countryRole: "ST", predictedPos: "ST", confirmedPos: "ST", startProb: 0.82, confirmedIn: true,
        club: { shots: 2.6, sot: 1.0, goals: 0.48, assists: 0.12, cards: 0.14 }, country: { shots: 2.4, sot: 0.95, goals: 0.50, assists: 0.10, cards: 0.13 } },
      { name: "Hakimi", team: "Morocco", clubRole: "FB", countryRole: "FB", predictedPos: "FB", confirmedPos: "FB", startProb: 0.90, confirmedIn: false,
        club: { shots: 1.0, sot: 0.35, goals: 0.10, assists: 0.25, cards: 0.20 }, country: { shots: 0.9, sot: 0.30, goals: 0.08, assists: 0.22, cards: 0.19 } },
    ],
    markets: [
      {
        key: "1x2",
        name: "Match result",
        outcomes: [
          { label: "Brazil", bet365: 1.95, pinnacle: 1.88 },
          { label: "Draw", bet365: 3.6, pinnacle: 3.7 },
          { label: "Morocco", bet365: 4.5, pinnacle: 4.3 },
        ],
      },
      {
        key: "ou25",
        name: "Total goals — Over/Under 2.5",
        outcomes: [
          { label: "Over 2.5", bet365: 2.1, pinnacle: 2.02 },
          { label: "Under 2.5", bet365: 1.78, pinnacle: 1.82 },
        ],
      },
      {
        key: "btts",
        name: "Both teams to score",
        outcomes: [
          { label: "Yes", bet365: 1.83, pinnacle: 1.86 },
          { label: "No", bet365: 2.05, pinnacle: 1.98 },
        ],
      },
    ],
  },
  {
    id: "wc26-arg-cro",
    home: "Argentina",
    away: "Croatia",
    group: "Group D",
    kickoff: "Today · 21:00",
    live: false,
    xgHome: 1.8,
    xgAway: 0.9,
    teamRates: {
      home: { corners: 6.2, cards: 1.5, shots: 15, sot: 5.5, offsides: 1.9, tackles: 15, fouls: 10, redProb: 0.04 },
      away: { corners: 4.5, cards: 2.1, shots: 10, sot: 3.5, offsides: 1.5, tackles: 17, fouls: 12, redProb: 0.06 },
    },
    players: [
      { name: "Lionel Messi", team: "Argentina", clubRole: "SS", countryRole: "CAM", predictedPos: "CAM", confirmedPos: "SS", startProb: 0.88, confirmedIn: true,
        club: { shots: 3.0, sot: 1.2, goals: 0.55, assists: 0.45, cards: 0.06 }, country: { shots: 2.6, sot: 1.0, goals: 0.42, assists: 0.40, cards: 0.05 } },
      { name: "Julián Álvarez", team: "Argentina", clubRole: "ST", countryRole: "ST", predictedPos: "ST", confirmedPos: "ST", startProb: 0.80, confirmedIn: true,
        club: { shots: 2.5, sot: 1.0, goals: 0.48, assists: 0.20, cards: 0.12 }, country: { shots: 2.2, sot: 0.9, goals: 0.40, assists: 0.18, cards: 0.11 } },
      { name: "Luka Modrić", team: "Croatia", clubRole: "CM", countryRole: "CM", predictedPos: "CM", confirmedPos: "CM", startProb: 0.85, confirmedIn: true,
        club: { shots: 1.2, sot: 0.4, goals: 0.10, assists: 0.25, cards: 0.18 }, country: { shots: 1.4, sot: 0.5, goals: 0.12, assists: 0.30, cards: 0.20 } },
    ],
    markets: [
      {
        key: "1x2",
        name: "Match result",
        outcomes: [
          { label: "Argentina", bet365: 1.7, pinnacle: 1.66 },
          { label: "Draw", bet365: 3.9, pinnacle: 3.95 },
          { label: "Croatia", bet365: 5.5, pinnacle: 5.2 },
        ],
      },
      {
        key: "ou25",
        name: "Total goals — Over/Under 2.5",
        outcomes: [
          { label: "Over 2.5", bet365: 2.25, pinnacle: 2.15 },
          { label: "Under 2.5", bet365: 1.67, pinnacle: 1.72 },
        ],
      },
      {
        key: "btts",
        name: "Both teams to score",
        outcomes: [
          { label: "Yes", bet365: 2.0, pinnacle: 1.95 },
          { label: "No", bet365: 1.85, pinnacle: 1.88 },
        ],
      },
    ],
  },
  {
    id: "wc26-eng-usa",
    home: "England",
    away: "USA",
    group: "Group B",
    kickoff: "Tomorrow · 18:00",
    live: false,
    xgHome: 1.9,
    xgAway: 0.8,
    teamRates: {
      home: { corners: 6.5, cards: 1.4, shots: 15.5, sot: 5.6, offsides: 2.0, tackles: 14, fouls: 9.5, redProb: 0.04 },
      away: { corners: 4.0, cards: 1.8, shots: 9.5, sot: 3.3, offsides: 1.4, tackles: 16, fouls: 11, redProb: 0.05 },
    },
    players: [
      { name: "Harry Kane", team: "England", clubRole: "ST", countryRole: "ST", predictedPos: "ST", confirmedPos: "ST", startProb: 0.95, confirmedIn: true,
        club: { shots: 3.2, sot: 1.4, goals: 0.70, assists: 0.25, cards: 0.10 }, country: { shots: 2.9, sot: 1.2, goals: 0.60, assists: 0.22, cards: 0.09 } },
      { name: "Bukayo Saka", team: "England", clubRole: "W", countryRole: "W", predictedPos: "W", confirmedPos: "CAM", startProb: 0.88, confirmedIn: true,
        club: { shots: 2.4, sot: 0.9, goals: 0.38, assists: 0.35, cards: 0.12 }, country: { shots: 2.1, sot: 0.8, goals: 0.30, assists: 0.30, cards: 0.11 } },
      { name: "Christian Pulisic", team: "USA", clubRole: "W", countryRole: "W", predictedPos: "W", confirmedPos: "W", startProb: 0.90, confirmedIn: true,
        club: { shots: 2.3, sot: 0.85, goals: 0.36, assists: 0.28, cards: 0.13 }, country: { shots: 2.5, sot: 0.95, goals: 0.42, assists: 0.32, cards: 0.14 } },
    ],
    markets: [
      {
        key: "1x2",
        name: "Match result",
        outcomes: [
          { label: "England", bet365: 1.62, pinnacle: 1.6 },
          { label: "Draw", bet365: 4.0, pinnacle: 4.1 },
          { label: "USA", bet365: 6.0, pinnacle: 5.6 },
        ],
      },
      {
        key: "ou25",
        name: "Total goals — Over/Under 2.5",
        outcomes: [
          { label: "Over 2.5", bet365: 2.05, pinnacle: 2.0 },
          { label: "Under 2.5", bet365: 1.82, pinnacle: 1.85 },
        ],
      },
      {
        key: "btts",
        name: "Both teams to score",
        outcomes: [
          { label: "Yes", bet365: 1.95, pinnacle: 1.92 },
          { label: "No", bet365: 1.9, pinnacle: 1.92 },
        ],
      },
    ],
  },
];

/* ---------- live data wiring ---------- */
// Where datalayer.service runs (feed + persistent ledger). Empty string =
// same origin: the deployed service serves the built app itself, and the
// vite dev server proxies /api to localhost:8000 (vite.config.js). Point it
// at a host only when the app is hosted away from the service. The terminal
// falls back to SAMPLE_MATCHES when nothing answers, so the preview always renders.
const API_BASE = "";

async function fetchFeed() {
  const r = await fetch(`${API_BASE}/api/feed`);
  if (!r.ok) throw new Error(`feed ${r.status}`);
  const feed = await r.json();
  if (!Array.isArray(feed) || !feed.length) throw new Error("empty feed");
  return feed;
}

// server ledger row -> the shape the Track-record views render
function fromServerBet(b) {
  return {
    id: b.id, serverId: b.id, fixture: b.match_id, market: b.market,
    selection: b.selection, modelProb: b.model_prob, price: b.price,
    stake: b.stake, status: b.status, closing: b.closing_price, pnl: b.pnl,
    ts: (b.ts || 0) * 1000,
  };
}

// the live feed carries ISO kickoffs; sample data carries display strings
function fmtKick(kickoff) {
  const d = new Date(kickoff);
  if (isNaN(d)) return kickoff;
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/* ---------- analyse single markets ---------- */
function analyseMarkets(match, matrix, modelWeight) {
  const modelKeyProb = {
    // model marginals for the markets we price
    "1x2": [
      probOf(matrix, LEGS.result_home.pred),
      probOf(matrix, LEGS.result_draw.pred),
      probOf(matrix, LEGS.result_away.pred),
    ],
    ou25: [
      probOf(matrix, LEGS.over25.pred),
      probOf(matrix, LEGS.under25.pred),
    ],
    btts: [
      probOf(matrix, LEGS.btts_yes.pred),
      probOf(matrix, LEGS.btts_no.pred),
    ],
  };

  const markets = match.markets.map((m) => {
    const b365 = m.outcomes.map((o) => o.bet365);
    const fairMarket = noVigProbs(m.outcomes.map((o) => o.pinnacle));
    const model = modelKeyProb[m.key];

    const blendedRaw = m.outcomes.map(
      (o, i) => modelWeight * model[i] + (1 - modelWeight) * fairMarket[i]
    );
    const sum = blendedRaw.reduce((a, b) => a + b, 0);
    const blended = blendedRaw.map((p) => p / sum);

    const outcomes = m.outcomes.map((o, i) => ({
      ...o,
      market: m.name,
      hitRate: blended[i],
      fairOdds: 1 / blended[i],
      b365Implied: impliedProb(o.bet365),
      edge: edge(o.bet365, blended[i]),
      kellyFull: kelly(o.bet365, blended[i]),
    }));
    return { ...m, outcomes, b365Margin: margin(b365) };
  });

  const bestBets = markets
    .flatMap((m) => m.outcomes)
    .filter((o) => o.edge > 0.005)
    .sort((a, b) => b.edge - a.edge)
    .slice(0, 4);

  return { markets, bestBets };
}

/* ---------- analyse a same-game multi ---------- */
// conditional goal environment: how each side's expected goals shift once the
// selected matrix legs are assumed true (e.g. Over 2.5 lifts both attacks)
function conditionalGoalFactors(matrix, preds) {
  let mass = 0, eh = 0, ea = 0, uh = 0, ua = 0;
  for (let i = 0; i < matrix.length; i++)
    for (let j = 0; j < matrix.length; j++) {
      const p = matrix[i][j];
      uh += i * p; ua += j * p;
      if (preds.every((f) => f(i, j))) { mass += p; eh += i * p; ea += j * p; }
    }
  if (mass <= 0 || !uh || !ua) return { fH: 1, fA: 1 };
  return { fH: eh / mass / uh, fA: ea / mass / ua };
}

function analyseSGM(legIds, match, matrix, playerLegs = {}) {
  if (legIds.length < 2) return null;
  const matrixLegs = legIds.filter((id) => LEGS[id]).map((id) => ({ id, ...LEGS[id] }));
  const pLegs = legIds.filter((id) => playerLegs[id]).map((id) => ({ id, ...playerLegs[id] }));
  if (matrixLegs.length + pLegs.length < 2) return null;
  const preds = matrixLegs.map((l) => l.pred);

  // matrix legs jointly, exactly from the score matrix; player legs repriced
  // in the goal environment those legs imply, then multiplied in (conditional
  // independence given the environment — an approximation, flagged in the UI)
  const jointMatrix = preds.length ? probOf(matrix, (i, j) => preds.every((f) => f(i, j))) : 1;
  const { fH, fA } = conditionalGoalFactors(matrix, preds);
  const playerJoint = pLegs.reduce((a, l) => a * l.prob(l.isHome ? fH : fA), 1);
  const jointProb = jointMatrix * playerJoint;

  // independent product of model marginals (for correlation comparison)
  const marginals = [
    ...matrixLegs.map((l) => probOf(matrix, l.pred)),
    ...pLegs.map((l) => l.prob(1)),
  ];
  const indepProb = marginals.reduce((a, b) => a * b, 1);

  // estimate the book SGM price from leg prices + correlation scaling
  const legBookOdds = [
    ...matrixLegs.map((l) => legBet365Odds(l.id, match) ?? 1 / probOf(matrix, l.pred) / 1.06),
    ...pLegs.map((l) => l.book ?? 1 / l.prob(1) / 1.08), // props carry more margin
  ];
  const indepBookOdds = legBookOdds.reduce((a, b) => a * b, 1);
  const estBookOdds =
    jointProb > 0 ? indepBookOdds * (indepProb / jointProb) : indepBookOdds;

  return {
    legs: [...matrixLegs, ...pLegs],
    hitRate: jointProb,
    fairOdds: jointProb > 0 ? 1 / jointProb : Infinity,
    indepOdds: 1 / indepProb,
    correlation: indepProb > 0 ? jointProb / indepProb : 1, // >1 positive
    estBookOdds,
    hasPlayerLegs: pLegs.length > 0,
  };
}

/* ---------- auto-suggest multis near a target odds band ---------- */
const SUGGEST_POOL = [
  "result_home",
  "result_away",
  "dc_1x",
  "over15",
  "over25",
  "under25",
  "btts_yes",
  "btts_no",
  "home_cs",
  "away_cs",
];

function suggestSGMs(match, matrix, targetCenter) {
  const ids = SUGGEST_POOL;
  const combos = [];
  const pushIfValid = (arr) => {
    const groups = arr.map((id) => LEGS[id].group);
    if (new Set(groups).size !== groups.length) return; // one per group
    combos.push(arr);
  };
  for (let a = 0; a < ids.length; a++)
    for (let b = a + 1; b < ids.length; b++) {
      pushIfValid([ids[a], ids[b]]);
      for (let c = b + 1; c < ids.length; c++)
        pushIfValid([ids[a], ids[b], ids[c]]);
    }

  const scored = combos
    .map((c) => {
      const r = analyseSGM(c, match, matrix);
      return {
        legIds: c,
        labels: c.map((id) => LEGS[id].label),
        hitRate: r.hitRate,
        fairOdds: r.fairOdds,
        estBookOdds: r.estBookOdds,
        edge: edge(r.estBookOdds, r.hitRate),
      };
    })
    .filter((c) => c.hitRate > 0.001 && isFinite(c.fairOdds));

  const filtered =
    targetCenter == null
      ? scored
      : scored.filter(
          (c) =>
            c.fairOdds >= targetCenter * 0.82 &&
            c.fairOdds <= targetCenter * 1.18
        );

  return filtered.sort((a, b) => b.hitRate - a.hitRate).slice(0, 8);
}

/* ---------- formatting ---------- */
const pct = (x) => `${(x * 100).toFixed(1)}%`;
const signedPct = (x) => `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%`;
const od = (x) => (isFinite(x) ? x.toFixed(2) : "—");
const edgeColor = (e) =>
  e > 0.03 ? "var(--val)" : e > 0.005 ? "var(--amber)" : "var(--neg)";

/* ---------- value bar ---------- */
function ValueBar({ fairProb, b365Implied, edge }) {
  const fairW = Math.min(fairProb * 100, 100);
  const bImpW = Math.min(b365Implied * 100, 100);
  const lo = Math.min(fairW, bImpW);
  const sliceW = Math.abs(fairW - bImpW);
  return (
    <div className="vbar" aria-hidden="true">
      <div className="vbar-track" />
      <div className="vbar-fair" style={{ width: `${fairW}%` }} />
      <div
        className="vbar-slice"
        style={{
          left: `${lo}%`,
          width: `${sliceW}%`,
          background: edge > 0 ? "var(--val)" : "var(--neg)",
        }}
      />
      <div className="vbar-tick" style={{ left: `${bImpW}%` }} />
    </div>
  );
}

/* ================================================================== */
/* ---------- full market catalogue derived from the score matrix ---------- */
// Every market below is an exact sum over the Poisson grid — no new data.
function deriveCatalog(M, xgHome, xgAway, teamRates) {
  const P = (pred) => probOf(M, pred);
  const mk = (name, outcomes, note) => ({ name, note, outcomes: outcomes.map(([label, p]) => ({ label, prob: p })) });

  const rH = P((i, j) => i > j), rD = P((i, j) => i === j), rA = P((i, j) => i < j);

  const result = [
    mk("Match result", [["Home", rH], ["Draw", rD], ["Away", rA]]),
    mk("Double chance", [["Home or draw", P((i, j) => i >= j)], ["Home or away", P((i, j) => i !== j)], ["Draw or away", P((i, j) => i <= j)]]),
    mk("Draw no bet", [["Home", rH / (rH + rA)], ["Away", rA / (rH + rA)]]),
    mk("Result + BTTS", [
      ["Home & BTTS", P((i, j) => i > j && i >= 1 && j >= 1)],
      ["Draw & BTTS", P((i, j) => i === j && i >= 1)],
      ["Away & BTTS", P((i, j) => i < j && i >= 1 && j >= 1)],
    ]),
    mk("Result / Over 2.5", [
      ["Home & Over", P((i, j) => i > j && i + j > 2.5)],
      ["Draw & Over", P((i, j) => i === j && i + j > 2.5)],
      ["Away & Over", P((i, j) => i < j && i + j > 2.5)],
    ]),
    mk("Result / Under 2.5", [
      ["Home & Under", P((i, j) => i > j && i + j < 2.5)],
      ["Draw & Under", P((i, j) => i === j && i + j < 2.5)],
      ["Away & Under", P((i, j) => i < j && i + j < 2.5)],
    ]),
  ];

  const lines = [0.5, 1.5, 2.5, 3.5, 4.5, 5.5];
  const goals = [
    ...lines.map((l) => mk(`Over/Under ${l}`, [[`Over ${l}`, P((i, j) => i + j > l)], [`Under ${l}`, P((i, j) => i + j < l)]])),
    mk("Both teams to score", [["Yes", P((i, j) => i >= 1 && j >= 1)], ["No", P((i, j) => i < 1 || j < 1)]]),
    mk("Total goals — exact", [
      ["0", P((i, j) => i + j === 0)], ["1", P((i, j) => i + j === 1)], ["2", P((i, j) => i + j === 2)],
      ["3", P((i, j) => i + j === 3)], ["4", P((i, j) => i + j === 4)], ["5+", P((i, j) => i + j >= 5)],
    ]),
    mk("Odd / even goals", [["Odd", P((i, j) => (i + j) % 2 === 1)], ["Even", P((i, j) => (i + j) % 2 === 0)]]),
    mk("Home team totals", [["Over 0.5", P((i) => i >= 1)], ["Over 1.5", P((i) => i >= 2)], ["Over 2.5", P((i) => i >= 3)]]),
    mk("Away team totals", [["Over 0.5", P((_i, j) => j >= 1)], ["Over 1.5", P((_i, j) => j >= 2)], ["Over 2.5", P((_i, j) => j >= 3)]]),
    mk("Goals range", [
      ["0-1", P((i, j) => i + j <= 1)], ["2-3", P((i, j) => i + j === 2 || i + j === 3)],
      ["4-6", P((i, j) => i + j >= 4 && i + j <= 6)], ["7+", P((i, j) => i + j >= 7)],
    ]),
    mk("Teams to score", [
      ["Both", P((i, j) => i >= 1 && j >= 1)], ["Home only", P((i, j) => i >= 1 && j === 0)],
      ["Away only", P((i, j) => i === 0 && j >= 1)], ["Neither", P((i, j) => i === 0 && j === 0)],
    ]),
  ];

  // correct score: every cell up to 4-4, plus "Any other"
  const cs = [];
  let csCovered = 0;
  for (let i = 0; i <= 4; i++)
    for (let j = 0; j <= 4; j++) {
      const p = M[i][j];
      cs.push([`${i}-${j}`, p]);
      csCovered += p;
    }
  cs.sort((a, b) => b[1] - a[1]);
  cs.push(["Any other", Math.max(0, 1 - csCovered)]);

  const score = [
    mk("Correct score", cs.slice(0, 13)),
    mk("Winning margin", [
      ["Home by 1", P((i, j) => i - j === 1)], ["Home by 2", P((i, j) => i - j === 2)], ["Home by 3+", P((i, j) => i - j >= 3)],
      ["Draw", rD],
      ["Away by 1", P((i, j) => j - i === 1)], ["Away by 2", P((i, j) => j - i === 2)], ["Away by 3+", P((i, j) => j - i >= 3)],
    ]),
    mk("Clean sheet", [["Home yes", P((_i, j) => j === 0)], ["Away yes", P((i) => i === 0)]]),
    mk("Win to nil", [["Home", P((i, j) => i > j && j === 0)], ["Away", P((i, j) => j > i && i === 0)]]),
  ];

  // half-time: approximate, ~45% of expected goals fall in the first half
  const Mh = scoreMatrix(xgHome * 0.45, xgAway * 0.45);
  const Mh2 = scoreMatrix(xgHome * 0.55, xgAway * 0.55);
  const Ph = (pred) => probOf(Mh, pred);

  // joint of the two (independent) halves -> HT/FT and highest scoring half
  const htft = {};
  const firstTot = {}, secondTot = {};
  for (let h1 = 0; h1 < Mh.length; h1++)
    for (let a1 = 0; a1 < Mh.length; a1++) {
      firstTot[h1 + a1] = (firstTot[h1 + a1] || 0) + Mh[h1][a1];
      for (let h2 = 0; h2 < Mh2.length; h2++)
        for (let a2 = 0; a2 < Mh2.length; a2++) {
          const p = Mh[h1][a1] * Mh2[h2][a2];
          const ht = h1 > a1 ? "H" : h1 < a1 ? "A" : "D";
          const H = h1 + h2, A = a1 + a2;
          const ft = H > A ? "H" : H < A ? "A" : "D";
          htft[`${ht}/${ft}`] = (htft[`${ht}/${ft}`] || 0) + p;
        }
    }
  for (let h2 = 0; h2 < Mh2.length; h2++)
    for (let a2 = 0; a2 < Mh2.length; a2++)
      secondTot[h2 + a2] = (secondTot[h2 + a2] || 0) + Mh2[h2][a2];
  let h1Most = 0, h2Most = 0, tie = 0;
  for (const k1 in firstTot)
    for (const k2 in secondTot) {
      const p = firstTot[k1] * secondTot[k2];
      if (+k1 > +k2) h1Most += p; else if (+k2 > +k1) h2Most += p; else tie += p;
    }

  const halves = [
    mk("Half-time result", [["Home", Ph((i, j) => i > j)], ["Draw", Ph((i, j) => i === j)], ["Away", Ph((i, j) => i < j)]], "approx"),
    mk("1st half Over/Under 0.5", [["Over 0.5", Ph((i, j) => i + j > 0.5)], ["Under 0.5", Ph((i, j) => i + j < 0.5)]], "approx"),
    mk("1st half Over/Under 1.5", [["Over 1.5", Ph((i, j) => i + j > 1.5)], ["Under 1.5", Ph((i, j) => i + j < 1.5)]], "approx"),
    mk("Highest scoring half", [["1st half", h1Most], ["2nd half", h2Most], ["Tie", tie]], "approx"),
    mk("Half-time / Full-time", [
      ["Home / Home", htft["H/H"] || 0], ["Draw / Home", htft["D/H"] || 0], ["Away / Home", htft["A/H"] || 0],
      ["Home / Draw", htft["H/D"] || 0], ["Draw / Draw", htft["D/D"] || 0], ["Away / Draw", htft["A/D"] || 0],
      ["Home / Away", htft["H/A"] || 0], ["Draw / Away", htft["D/A"] || 0], ["Away / Away", htft["A/A"] || 0],
    ], "approx"),
  ];

  const groups = [
    { group: "Result", markets: result },
    { group: "Goals", markets: goals },
    { group: "Score", markets: score },
    { group: "Half-time", markets: halves },
  ];

  // ---- team count markets (corners, cards, shots, SOT, offsides, tackles, fouls) ----
  if (teamRates) {
    const R = teamRates, disp = { corners: 10, cards: 5, shots: 15, sot: 8, offsides: 4, tackles: 20, fouls: 25, throwins: 18, freekicks: 14, goalkicks: 8 };
    // opponent strength via xG: attacking volume scales with own attack, while
    // discipline/defensive counts scale with how much the team must defend
    const BASE_G = 1.35;
    const aH = clamp(xgHome / BASE_G, 0.6, 1.7), aA = clamp(xgAway / BASE_G, 0.6, 1.7);
    const ATT = new Set(["corners", "shots", "sot", "offsides"]);
    const adjH = (m) => R.home[m] * (ATT.has(m) ? aH : aA);
    const adjA = (m) => R.away[m] * (ATT.has(m) ? aA : aH);
    const tm = [];
    const total = (metric) => countDist(adjH(metric) + adjA(metric), disp[metric]);
    const ou = (metric, lines, label) => {
      const d = total(metric);
      lines.forEach((l) => tm.push(mk(`${label} O/U ${l}`, [[`Over ${l}`, distOver(d, l)], [`Under ${l}`, 1 - distOver(d, l)]])));
    };
    const most = (metric, label) => {
      const { home, away, tie } = teamMost(countDist(adjH(metric), disp[metric]), countDist(adjA(metric), disp[metric]));
      tm.push(mk(`${label} — team with most`, [["Home", home], ["Away", away], ["Tie", tie]]));
    };
    ou("corners", [8.5, 9.5, 10.5, 11.5], "Corners");
    most("corners", "Corners");
    ou("cards", [2.5, 3.5, 4.5], "Cards");
    const dhc = countDist(adjH("cards"), disp.cards), dac = countDist(adjA("cards"), disp.cards);
    const both = (1 - dhc[0]) * (1 - dac[0]);
    tm.push(mk("Both teams to be carded", [["Yes", both], ["No", 1 - both]]));
    most("cards", "Cards");
    const red = 1 - (1 - R.home.redProb) * (1 - R.away.redProb);
    tm.push(mk("Red card in match", [["Yes", red], ["No", 1 - red]]));
    ou("shots", [21.5, 23.5, 25.5], "Total shots");
    ou("sot", [7.5, 8.5, 9.5], "Shots on target");
    ou("offsides", [2.5, 3.5, 4.5], "Offsides");
    ou("tackles", [15.5, 17.5], "Tackles");
    ou("fouls", [20.5, 22.5, 24.5], "Fouls");
    // derived counts (mirrors the Python engine): free kicks = fouls +
    // offsides by law (+ a few other infringements); goal kicks track
    // off-target shots; throw-ins are a flat prior (no source, low variance)
    const ouMu = (mu, key, lines, label) => {
      const d = countDist(mu, disp[key]);
      lines.forEach((l) => tm.push(mk(`${label} O/U ${l}`, [[`Over ${l}`, distOver(d, l)], [`Under ${l}`, 1 - distOver(d, l)]])));
    };
    ouMu(adjH("fouls") + adjA("fouls") + adjH("offsides") + adjA("offsides") + 1.5,
      "freekicks", [23.5, 26.5, 29.5], "Free kicks");
    ouMu(6.5 + 0.5 * Math.max(0, adjH("shots") - adjH("sot") + adjA("shots") - adjA("sot")),
      "goalkicks", [13.5, 15.5, 17.5], "Goal kicks");
    ouMu(40, "throwins", [36.5, 39.5, 42.5], "Throw-ins");
    groups.push({ group: "Team markets", markets: tm });
  }

  return groups;
}

function MarketsView({ matches = SAMPLE_MATCHES, feedNote = "", onLog = () => {} }) {
  const [activeId, setActiveId] = useState(matches[0].id);
  const [modelWeight, setModelWeight] = useState(0.3);
  const [stake, setStake] = useState(100);
  const [kellyFrac, setKellyFrac] = useState(0.25);

  const base = matches.find((m) => m.id === activeId) || matches[0];
  // editable xG (keyed by match so switching keeps its own values)
  const [xg, setXg] = useState({});
  const xgHome = xg[activeId]?.h ?? base.xgHome;
  const xgAway = xg[activeId]?.a ?? base.xgAway;
  const setXgHome = (v) =>
    setXg((s) => ({ ...s, [activeId]: { h: v, a: xgAway } }));
  const setXgAway = (v) =>
    setXg((s) => ({ ...s, [activeId]: { h: xgHome, a: v } }));

  const matrix = useMemo(() => scoreMatrix(xgHome, xgAway), [xgHome, xgAway]);
  const { markets, bestBets } = useMemo(
    () => analyseMarkets(base, matrix, modelWeight),
    [base, matrix, modelWeight]
  );

  // SGM builder: one leg per group
  const [picks, setPicks] = useState({});
  const [bookOverride, setBookOverride] = useState("");

  // suggestions
  const [target, setTarget] = useState(2.0);

  // player markets
  const [lineupStatus, setLineupStatus] = useState("predicted");
  const [playerCW, setPlayerCW] = useState(0.6); // country weight for props
  const [playerBook, setPlayerBook] = useState({}); // "name|market" -> odds
  const [openPlayers, setOpenPlayers] = useState({}); // name -> expanded card
  const [openFams, setOpenFams] = useState({}); // "name|family" -> expanded
  const players = useMemo(
    () =>
      base.players.map((p) => {
        const isHome = p.team === base.home;
        const ctx = { teamXg: isHome ? xgHome : xgAway, oppXg: isHome ? xgAway : xgHome };
        return { ...p, ...priceProps(p, playerCW, lineupStatus, ctx) };
      }),
    [base, playerCW, lineupStatus, xgHome, xgAway]
  );

  // player legs for the SGM builder: top attacking starters, repriced in the
  // goal environment of whatever matrix legs are picked (see analyseSGM)
  const playerLegs = useMemo(() => {
    const out = {};
    players
      .filter((p) => p.startProb >= 0.5 && p.pos !== "GK" && p.exp)
      .sort((a, b) => (b.exp.goals || 0) - (a.exp.goals || 0))
      .slice(0, 6)
      .forEach((p) => {
        const isHome = p.team === base.home;
        const env = (f) => clamp(f, 0.5, 1.7);
        [
          ["ags", "to score", (f) => clamp(1 - Math.exp(-p.exp.goals * env(f)), 0.002, 0.998),
           p.bookOdds?.["Anytime goalscorer"]],
          ["sot1", "SOT 1+", (f) => clamp(atLeast(1, p.exp.sot * env(f), 6), 0.002, 0.998),
           p.bookOdds?.["Shots on target 1+"]],
          ["sh2", "Shots 2+", (f) => clamp(atLeast(2, p.exp.shots * env(f), 8), 0.002, 0.998),
           p.bookOdds?.["Shots 2+"]],
        ].forEach(([k, lbl, prob, book]) => {
          out[`pl|${p.name}|${k}`] = {
            group: `pl|${p.name}`, label: `${p.name} ${lbl}`, prob, book: book ?? null, isHome,
          };
        });
      });
    return out;
  }, [players, base]);

  const togglePick = (id) => {
    const g = (LEGS[id] || playerLegs[id])?.group;
    if (!g) return;
    setPicks((p) => (p[g] === id ? { ...p, [g]: undefined } : { ...p, [g]: id }));
  };
  const pickedIds = Object.values(picks).filter(Boolean);
  const sgm = useMemo(
    () => analyseSGM(pickedIds, base, matrix, playerLegs),
    [pickedIds, base, matrix, playerLegs]
  );
  const sgmBookOdds =
    bookOverride !== "" && Number(bookOverride) > 1
      ? Number(bookOverride)
      : sgm?.estBookOdds;
  const sgmEdge = sgm ? edge(sgmBookOdds, sgm.hitRate) : 0;

  // full derived market catalogue (model fair odds for every goals market)
  const catalog = useMemo(() => deriveCatalog(matrix, xgHome, xgAway, base.teamRates), [matrix, xgHome, xgAway, base]);
  const [catBook, setCatBook] = useState({});
  const [openGroup, setOpenGroup] = useState("Result");
  const suggestions = useMemo(
    () => suggestSGMs(base, matrix, target),
    [base, matrix, target]
  );

  const legGroups = ["result", "totals", "btts", "cs"];
  const groupTitle = {
    result: "Result",
    totals: "Goals",
    btts: "Both teams",
    cs: "Clean sheet",
  };

  return (
    <div className="vt-root">
      <style>{CSS}</style>

      <header className="vt-head">
        <div className="vt-brand">
          <span className="vt-mark">▚</span>
          <div>
            <div className="vt-title">VALUE TERMINAL</div>
            <div className="vt-sub">FIFA World Cup 2026 · edge &amp; hit-rate{feedNote && ` · ${feedNote}`}</div>
          </div>
        </div>
        <div className="vt-controls">
          <label className="vt-ctrl">
            <span>Bankroll</span>
            <div className="vt-inputwrap">
              <em>$</em>
              <input
                type="number"
                value={stake}
                min={0}
                onChange={(e) => setStake(Number(e.target.value) || 0)}
              />
            </div>
          </label>
          <label className="vt-ctrl">
            <span>Kelly fraction · {Math.round(kellyFrac * 100)}%</span>
            <input
              type="range"
              min={0.1}
              max={1}
              step={0.05}
              value={kellyFrac}
              onChange={(e) => setKellyFrac(Number(e.target.value))}
            />
          </label>
        </div>
      </header>

      <div className="vt-grid">
        <aside className="vt-rail">
          <div className="vt-railhead">Fixtures</div>
          {matches.map((m) => (
            <button
              key={m.id}
              className={`vt-match ${m.id === activeId ? "is-active" : ""}`}
              onClick={() => setActiveId(m.id)}
            >
              <div className="vt-matchtop">
                <span className="vt-teams">
                  {m.home} <i>v</i> {m.away}
                </span>
                {m.live && <span className="vt-live">LIVE</span>}
              </div>
              <div className="vt-matchmeta">
                {m.group} · {fmtKick(m.kickoff)}
              </div>
            </button>
          ))}
        </aside>

        <main className="vt-main">
          {/* hero + xG model */}
          <div className="vt-matchhero">
            <div>
              <h2>
                {base.home} <span className="vt-vs">vs</span> {base.away}
              </h2>
              <div className="vt-herometa">
                {base.group} · {fmtKick(base.kickoff)}
                {base.live && <span className="vt-live sm">LIVE</span>}
              </div>
            </div>
            <div className="vt-model">
              <div className="vt-xg">
                <label>
                  <span>xG {base.home}</span>
                  <input
                    type="number"
                    step="0.05"
                    value={xgHome}
                    onChange={(e) => setXgHome(Number(e.target.value) || 0)}
                  />
                </label>
                <label>
                  <span>xG {base.away}</span>
                  <input
                    type="number"
                    step="0.05"
                    value={xgAway}
                    onChange={(e) => setXgAway(Number(e.target.value) || 0)}
                  />
                </label>
              </div>
              <label className="vt-blend">
                <span>
                  Fair source · {Math.round((1 - modelWeight) * 100)}% market /{" "}
                  {Math.round(modelWeight * 100)}% model
                </span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={modelWeight}
                  onChange={(e) => setModelWeight(Number(e.target.value))}
                />
              </label>
            </div>
          </div>

          {/* best singles */}
          <section className="vt-best">
            <div className="vt-besthead">
              <span>Best value singles</span>
              <span className="vt-bestnote">
                ranked by edge · stake = {Math.round(kellyFrac * 100)}% Kelly
              </span>
            </div>
            {bestBets.length === 0 ? (
              <div className="vt-empty">
                Nothing prices above fair right now. Lower the model weight or
                wait for the line to move.
              </div>
            ) : (
              <div className="vt-bestrow">
                {bestBets.map((o, i) => (
                  <div className="vt-card" key={i}>
                    <div className="vt-cardmkt">{o.market}</div>
                    <div className="vt-cardlabel">{o.label}</div>
                    <div className="vt-cardodds">{od(o.bet365)}</div>
                    <div className="vt-cardrow">
                      <span>Hit rate</span>
                      <b>{pct(o.hitRate)}</b>
                    </div>
                    <div className="vt-cardrow">
                      <span>Edge</span>
                      <b style={{ color: edgeColor(o.edge) }}>
                        {signedPct(o.edge)}
                      </b>
                    </div>
                    <div className="vt-cardstake">
                      stake ${(o.kellyFull * kellyFrac * stake).toFixed(2)}
                    </div>
                    <button
                      className="vt-logbtn"
                      onClick={() =>
                        onLog({
                          fixture: `${base.home} v ${base.away}`,
                          market: o.market,
                          selection: o.label,
                          modelProb: o.hitRate,
                          price: o.bet365,
                          stake: +(o.kellyFull * kellyFrac * stake).toFixed(2),
                        })
                      }
                    >
                      + Log bet
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* market tables */}
          {markets.map((m) => (
            <section className="vt-mkt" key={m.key}>
              <div className="vt-mkthead">
                <span>{m.name}</span>
                <span className="vt-juice">
                  Bet365 margin {pct(m.b365Margin)}
                </span>
              </div>
              <div className="vt-table">
                <div className="vt-trow vt-trow--head">
                  <span>Outcome</span>
                  <span className="vt-num">Bet365</span>
                  <span className="vt-num">Fair</span>
                  <span className="vt-num">Hit rate</span>
                  <span className="vt-bar">chance vs price</span>
                  <span className="vt-num">Edge</span>
                  <span className="vt-num">Stake</span>
                </div>
                {m.outcomes.map((o, i) => (
                  <div
                    className={`vt-trow ${o.edge > 0.005 ? "is-value" : ""}`}
                    key={i}
                  >
                    <span className="vt-outcome">{o.label}</span>
                    <span className="vt-num vt-b365">{od(o.bet365)}</span>
                    <span className="vt-num vt-fair">{od(o.fairOdds)}</span>
                    <span className="vt-num vt-hit">{pct(o.hitRate)}</span>
                    <span className="vt-bar">
                      <ValueBar
                        fairProb={o.hitRate}
                        b365Implied={o.b365Implied}
                        edge={o.edge}
                      />
                    </span>
                    <span className="vt-num" style={{ color: edgeColor(o.edge) }}>
                      {signedPct(o.edge)}
                    </span>
                    <span className="vt-num vt-stake">
                      {o.kellyFull > 0
                        ? `$${(o.kellyFull * kellyFrac * stake).toFixed(0)}`
                        : "—"}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          ))}

          {/* ---------- SAME GAME MULTI ---------- */}
          <section className="vt-sgm">
            <div className="vt-mkthead">
              <span>Same game multi — builder</span>
              <span className="vt-juice">correlation-aware · one leg per group</span>
            </div>

            <div className="vt-legpool">
              {legGroups.map((g) => (
                <div className="vt-leggroup" key={g}>
                  <div className="vt-leggrouphd">{groupTitle[g]}</div>
                  <div className="vt-legchips">
                    {Object.entries(LEGS)
                      .filter(([, l]) => l.group === g)
                      .map(([id, l]) => (
                        <button
                          key={id}
                          className={`vt-chip ${picks[g] === id ? "on" : ""}`}
                          onClick={() => togglePick(id)}
                        >
                          {l.label}
                        </button>
                      ))}
                  </div>
                </div>
              ))}
            </div>

            {Object.keys(playerLegs).length > 0 && (
              <div className="vt-legpool" style={{ gridTemplateColumns: "1fr" }}>
                <div className="vt-leggroup">
                  <div className="vt-leggrouphd">Player props · one per player</div>
                  <div className="vt-legchips">
                    {Object.entries(playerLegs).map(([id, l]) => (
                      <button
                        key={id}
                        className={`vt-chip ${picks[l.group] === id ? "on" : ""}`}
                        onClick={() => togglePick(id)}
                      >
                        {l.label}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {!sgm ? (
              <div className="vt-empty">
                Pick at least two legs to build a multi.
              </div>
            ) : (
              <div className="vt-sgmresult">
                <div className="vt-sgmlegs">
                  {sgm.legs.map((l) => (
                    <span className="vt-sgmleg" key={l.id}>
                      {l.label}
                    </span>
                  ))}
                </div>
                <div className="vt-sgmgrid">
                  <div className="vt-stat">
                    <span>Hit rate</span>
                    <b>{pct(sgm.hitRate)}</b>
                  </div>
                  <div className="vt-stat">
                    <span>Fair odds</span>
                    <b>{od(sgm.fairOdds)}</b>
                  </div>
                  <div className="vt-stat">
                    <span>If uncorrelated</span>
                    <b className="dim">{od(sgm.indepOdds)}</b>
                  </div>
                  <div className="vt-stat">
                    <span>Correlation</span>
                    <b
                      style={{
                        color:
                          sgm.correlation > 1.02
                            ? "var(--val)"
                            : sgm.correlation < 0.98
                            ? "var(--neg)"
                            : "var(--muted)",
                      }}
                    >
                      {sgm.correlation > 1.02
                        ? "positive"
                        : sgm.correlation < 0.98
                        ? "negative"
                        : "neutral"}
                    </b>
                  </div>
                  <div className="vt-stat">
                    <span>Bet365 price</span>
                    <div className="vt-bookinput">
                      <input
                        type="number"
                        step="0.05"
                        placeholder={od(sgm.estBookOdds)}
                        value={bookOverride}
                        onChange={(e) => setBookOverride(e.target.value)}
                      />
                    </div>
                  </div>
                  <div className="vt-stat">
                    <span>Edge</span>
                    <b style={{ color: edgeColor(sgmEdge) }}>
                      {signedPct(sgmEdge)}
                    </b>
                  </div>
                  <div className="vt-stat">
                    <span>Stake ({Math.round(kellyFrac * 100)}% Kelly)</span>
                    <b className="val">
                      $
                      {(
                        kelly(sgmBookOdds, sgm.hitRate) *
                        kellyFrac *
                        stake
                      ).toFixed(2)}
                    </b>
                  </div>
                </div>
                <p className="vt-sgmnote">
                  Price field shows the model estimate — paste the real figure
                  from the Bet365 bet slip for a true value read. Edge is
                  usually negative on multis; that's expected, which is why hit
                  rate is shown alongside.
                  {sgm.hasPlayerLegs &&
                    " Player legs are repriced in the goal environment the match legs imply (a scorer leg gets likelier inside an Over), then treated as independent given that environment — an approximation, honest but not exact."}
                </p>
              </div>
            )}
          </section>

          {/* ---------- suggested multis near a target ---------- */}
          <section className="vt-sgm">
            <div className="vt-mkthead">
              <span>Suggested multis near a target price</span>
              <span className="vt-juice">ranked by hit rate</span>
            </div>
            <div className="vt-targets">
              {[
                { l: "~2.00", v: 2.0 },
                { l: "~4.00", v: 4.0 },
                { l: "~8.00", v: 8.0 },
                { l: "All", v: null },
              ].map((t) => (
                <button
                  key={t.l}
                  className={`vt-tbtn ${target === t.v ? "on" : ""}`}
                  onClick={() => setTarget(t.v)}
                >
                  {t.l}
                </button>
              ))}
            </div>
            <div className="vt-suggrid">
              {suggestions.length === 0 ? (
                <div className="vt-empty">No combinations land in that band.</div>
              ) : (
                suggestions.map((s, i) => (
                  <div className="vt-sugcard" key={i}>
                    <div className="vt-suglegs">
                      {s.labels.map((l, k) => (
                        <span key={k}>{l}</span>
                      ))}
                    </div>
                    <div className="vt-sugfoot">
                      <div>
                        <em>Fair</em>
                        <b>{od(s.fairOdds)}</b>
                      </div>
                      <div>
                        <em>Hit</em>
                        <b>{pct(s.hitRate)}</b>
                      </div>
                      <div>
                        <em>Edge</em>
                        <b style={{ color: edgeColor(s.edge) }}>
                          {signedPct(s.edge)}
                        </b>
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          </section>

          {/* ---------- PLAYER MARKETS ---------- */}
          <section className="vt-sgm">
            <div className="vt-mkthead">
              <span>Player markets</span>
              <span className="vt-juice">role-adjusted · club + country form</span>
            </div>

            <div className="vt-plrcontrols">
              <div className="vt-lineuptoggle">
                {["predicted", "confirmed"].map((s) => (
                  <button
                    key={s}
                    className={`vt-lbtn ${lineupStatus === s ? "on" : ""}`}
                    onClick={() => setLineupStatus(s)}
                  >
                    {s === "predicted" ? "Predicted XI" : "Confirmed XI"}
                  </button>
                ))}
              </div>
              <label className="vt-blend">
                <span>
                  Form weight · {Math.round((1 - playerCW) * 100)}% club /{" "}
                  {Math.round(playerCW * 100)}% country
                </span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={playerCW}
                  onChange={(e) => setPlayerCW(Number(e.target.value))}
                />
              </label>
            </div>

            <div className="vt-plrgrid">
              {players
                .filter((p) => p.inXI)
                .map((p) => {
                  const open = !!openPlayers[p.name];
                  return (
                  <div className="vt-plrcard" key={p.name}>
                    <button
                      className="vt-plrhead vt-plrhead--toggle"
                      onClick={() => setOpenPlayers((s) => ({ ...s, [p.name]: !open }))}
                    >
                      <div style={{ textAlign: "left" }}>
                        <div className="vt-plrname">{p.name}</div>
                        <div className="vt-plrmeta">
                          {p.team} · {Math.round(p.startProb * 100)}% to start
                        </div>
                      </div>
                      <div className="vt-plrtags">
                        {p.pen && <span className="vt-sptag pen">PEN</span>}
                        {p.fk && <span className="vt-sptag fk">FK</span>}
                        <span className={`vt-poschip ${p.posChanged ? "moved" : ""}`}>
                          {p.pos}
                          {p.posChanged && <i> ← {p.predictedPos}</i>}
                        </span>
                        <span className="cat-gcount">{open ? "−" : "+"}</span>
                      </div>
                    </button>
                    {open && (
                    <div className="vt-proptable">
                      {groupProps(p.props).map((fam) => {
                        const fkey = `${p.name}|${fam.name}`;
                        const fopen = !!openFams[fkey];
                        // last-5 per-match counts, newest first (feed-supplied)
                        const l5 = fam.statKey && p.last5?.length
                          ? p.last5.map((g) => g[fam.statKey] ?? 0).join(" · ")
                          : null;
                        return (
                          <div className="vt-fam" key={fam.name}>
                            <button
                              className="vt-famhead"
                              onClick={() => setOpenFams((s) => ({ ...s, [fkey]: !fopen }))}
                            >
                              <span>{fam.name}</span>
                              <span className="vt-famr">
                                {l5 && <i className="vt-faml5">L5 {l5}</i>}
                                <i className="cat-gcount">{fopen ? "−" : "+"}</i>
                              </span>
                            </button>
                            {fopen && (
                              <>
                                <div className="vt-prow vt-prow--head">
                                  <span>Market</span>
                                  <span className="vt-num">Hit</span>
                                  <span className="vt-num">Fair</span>
                                  <span className="vt-num">Bet365</span>
                                  <span className="vt-num">Edge</span>
                                </div>
                                {fam.props.map((pr) => {
                                  const key = `${p.name}|${pr.name}`;
                                  const raw = playerBook[key];
                                  // feed-supplied slip price (props CSV) unless typed over
                                  const fromFeed = p.bookOdds?.[pr.name];
                                  const shown = raw !== undefined ? raw : fromFeed ?? "";
                                  const bookOdds = shown && Number(shown) > 1 ? Number(shown) : null;
                                  const e = bookOdds ? bookOdds * pr.hitRate - 1 : null;
                                  return (
                                    <div className="vt-prow" key={pr.name}>
                                      <span className="vt-pmkt">{pr.name}</span>
                                      <span className="vt-num vt-hit">{pct(pr.hitRate)}</span>
                                      <span className="vt-num vt-fair">{od(pr.fairOdds)}</span>
                                      <span className="vt-num">
                                        <input
                                          className="vt-pinput"
                                          type="number"
                                          step="0.05"
                                          placeholder="—"
                                          value={shown}
                                          onChange={(ev) =>
                                            setPlayerBook((b) => ({
                                              ...b,
                                              [key]: ev.target.value,
                                            }))
                                          }
                                        />
                                      </span>
                                      <span
                                        className="vt-num"
                                        style={{ color: e == null ? "var(--muted)" : edgeColor(e) }}
                                      >
                                        {e == null ? "—" : signedPct(e)}
                                      </span>
                                    </div>
                                  );
                                })}
                              </>
                            )}
                          </div>
                        );
                      })}
                    </div>
                    )}
                  </div>
                  );
                })}
            </div>
            <p className="vt-sgmnote">
              Rates are role-adjusted to each player's match position and blended
              across club and country form (weighted by each source's sample
              size). Click a player to expand their props. Switch to Confirmed
              XI when the team sheets drop — start probabilities lock and anyone
              in a new position re-prices. Prices prefill from the props CSV;
              type over them to read edge against a fresher slip.
            </p>
          </section>

          {/* ---------- ALL MARKETS (model fair odds) ---------- */}
          <section className="vt-sgm">
            <div className="vt-mkthead">
              <span>All markets — model fair odds</span>
              <span className="vt-juice">derived from the score matrix · paste a price for edge</span>
            </div>
            <div className="cat-groups">
              {catalog.map((g) => {
                const open = openGroup === g.group;
                return (
                  <div className="cat-group" key={g.group}>
                    <button
                      className={`cat-ghead ${open ? "open" : ""}`}
                      onClick={() => setOpenGroup(open ? "" : g.group)}
                    >
                      <span>{g.group}</span>
                      <span className="cat-gcount">{g.markets.length} markets {open ? "−" : "+"}</span>
                    </button>
                    {open && (
                      <div className="cat-gbody">
                        {g.markets.map((m) => (
                          <div className="cat-mkt" key={m.name}>
                            <div className="cat-mname">
                              {m.name}
                              {m.note && <i className="cat-note"> {m.note}</i>}
                            </div>
                            <div className="cat-table">
                              <div className="cat-row cat-row--head">
                                <span>Outcome</span>
                                <span className="vt-num">Hit</span>
                                <span className="vt-num">Fair</span>
                                <span className="vt-num">Bet365</span>
                                <span className="vt-num">Edge</span>
                              </div>
                              {m.outcomes.map((o) => {
                                const key = `${g.group}|${m.name}|${o.label}`;
                                const raw = catBook[key];
                                const book = raw && Number(raw) > 1 ? Number(raw) : null;
                                const e = book ? book * o.prob - 1 : null;
                                return (
                                  <div className="cat-row" key={o.label}>
                                    <span className="cat-olabel">{o.label}</span>
                                    <span className="vt-num cat-hit">{pct(o.prob)}</span>
                                    <span className="vt-num cat-fair">{od(o.prob > 0 ? 1 / o.prob : Infinity)}</span>
                                    <input
                                      className="cat-input"
                                      type="number"
                                      step="0.05"
                                      placeholder="—"
                                      value={raw || ""}
                                      onChange={(ev) => setCatBook((b) => ({ ...b, [key]: ev.target.value }))}
                                    />
                                    <span className="vt-num" style={{ color: e == null ? "var(--muted)" : edgeColor(e) }}>
                                      {e == null ? "—" : signedPct(e)}
                                    </span>
                                  </div>
                                );
                              })}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            <p className="vt-sgmnote">
              Every figure here is the model's own fair price from the score
              matrix — correct score, totals, combos, margins and an approximate
              half-time set. Where a market also comes from the live feed (1X2,
              O/U 2.5, BTTS) the edge is shown up top; for the rest, paste the
              Bet365 price to read value. Corners, cards and other non-goal
              markets need their own models and aren't shown here.
            </p>
          </section>

          <footer className="vt-foot">
            <span>
              {feedNote === "sample data"
                ? "Sample data shaped like an odds-aggregator feed. "
                : "Live feed from datalayer.service. "}
              SGM hit rate is the joint probability from the Poisson score
              matrix (correlation exact).
            </span>
            <span className="vt-disc">For analysis only · 18+ · gamble responsibly</span>
          </footer>
        </main>
      </div>
    </div>
  );
}

/* ================================================================== *
 *  TRACK RECORD — the learning loop, previewable on baked-in history
 *  Mirrors datalayer/betlog: ledger -> metrics -> calibration -> trust.
 *  All data below is generated in-memory so it renders with zero setup.
 * ================================================================== */

function mulberry32(a) {
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// each segment carries its own truth so the dashboard tells an honest story:
// most break even, props/ou show small edge (+CLV), longshot SGMs lose (-CLV)
const SEGMENTS = [
  { market: "1x2", sel: "Match result", pr: [1.6, 2.2], mp: [0.48, 0.64], bias: 0.94, clv: [0.97, 1.0] },
  { market: "ou25", sel: "Over/Under 2.5", pr: [1.8, 2.1], mp: [0.5, 0.6], bias: 0.98, clv: [0.95, 0.99] },
  { market: "btts", sel: "Both teams to score", pr: [1.85, 2.05], mp: [0.5, 0.56], bias: 0.9, clv: [0.99, 1.02] },
  { market: "player_gs", sel: "Anytime goalscorer", pr: [2.4, 3.6], mp: [0.3, 0.45], bias: 0.95, clv: [0.95, 0.99] },
  { market: "sgm", sel: "Same game multi", pr: [4.5, 8.0], mp: [0.12, 0.24], bias: 0.78, clv: [1.06, 1.16] },
];
const FIX = ["BRA v MAR", "ARG v CRO", "ENG v USA", "FRA v MEX", "ESP v JPN", "POR v CAN"];

function generateLedger(seed = 42, n = 110) {
  const rng = mulberry32(seed);
  const pick = (lo, hi) => lo + rng() * (hi - lo);
  const bets = [];
  const day = 86400000;
  for (let i = 0; i < n; i++) {
    const s = SEGMENTS[Math.floor(rng() * SEGMENTS.length)];
    const modelProb = +pick(s.mp[0], s.mp[1]).toFixed(3);
    const price = +pick(s.pr[0], s.pr[1]).toFixed(2);
    const trueP = modelProb * s.bias;
    const won = rng() < trueP;
    const closing = +(price * pick(s.clv[0], s.clv[1])).toFixed(2);
    const stake = +pick(1, 4).toFixed(1);
    bets.push({
      i,
      ts: Date.now() - (n - i) * day * 0.4,
      fixture: FIX[Math.floor(rng() * FIX.length)],
      market: s.market,
      selection: s.sel,
      modelProb,
      price,
      closing,
      stake,
      status: won ? "won" : "lost",
      pnl: +(won ? stake * (price - 1) : -stake).toFixed(2),
    });
  }
  // a few still-open bets at the end
  for (let k = 0; k < 5; k++) {
    const s = SEGMENTS[Math.floor(rng() * SEGMENTS.length)];
    bets.push({
      i: n + k,
      ts: Date.now() - k * 3600000,
      fixture: FIX[Math.floor(rng() * FIX.length)],
      market: s.market,
      selection: s.sel,
      modelProb: +((s.mp[0] + s.mp[1]) / 2).toFixed(3),
      price: +((s.pr[0] + s.pr[1]) / 2).toFixed(2),
      closing: null,
      stake: 2,
      status: "open",
      pnl: null,
    });
  }
  return bets;
}

const won01 = (b) => (b.status === "won" ? 1 : 0);

function perfMetrics(settled) {
  if (!settled.length) return { n: 0 };
  const staked = settled.reduce((a, b) => a + b.stake, 0);
  const pnl = settled.reduce((a, b) => a + b.pnl, 0);
  const wins = settled.reduce((a, b) => a + won01(b), 0);
  const clvs = settled.filter((b) => b.closing).map((b) => b.price / b.closing - 1);
  const avgClv = clvs.reduce((a, b) => a + b, 0) / (clvs.length || 1);
  const posClv = clvs.filter((c) => c > 0).length / (clvs.length || 1);
  const brier = settled.reduce((a, b) => a + (b.modelProb - won01(b)) ** 2, 0) / settled.length;
  return {
    n: settled.length,
    hitRate: wins / settled.length,
    staked,
    pnl,
    roi: pnl / staked,
    avgClv,
    posClv,
    brier,
  };
}

// pool-adjacent-violators isotonic fit (ported from the Python betlog)
function pav(values, weights) {
  const blocks = [];
  for (let k = 0; k < values.length; k++) {
    blocks.push([values[k] * weights[k], weights[k], 1]);
    while (
      blocks.length >= 2 &&
      blocks[blocks.length - 2][0] / blocks[blocks.length - 2][1] >
        blocks[blocks.length - 1][0] / blocks[blocks.length - 1][1]
    ) {
      const [s2, w2, c2] = blocks.pop();
      const [s1, w1, c1] = blocks.pop();
      blocks.push([s1 + s2, w1 + w2, c1 + c2]);
    }
  }
  const out = [];
  for (const [s, w, c] of blocks) for (let j = 0; j < c; j++) out.push(s / w);
  return out;
}

function fitCalibrator(settled, nbins = 10) {
  if (settled.length < 30) return { active: false, fn: (p) => p, points: [] };
  const bins = {};
  for (const b of settled) {
    const k = Math.min(nbins - 1, Math.floor(b.modelProb * nbins));
    (bins[k] = bins[k] || []).push(b);
  }
  const ks = Object.keys(bins).map(Number).sort((a, b) => a - b);
  const xs = ks.map((k) => bins[k].reduce((a, b) => a + b.modelProb, 0) / bins[k].length);
  const rates = ks.map((k) => bins[k].reduce((a, b) => a + won01(b), 0) / bins[k].length);
  const fitted = pav(rates, ks.map((k) => bins[k].length));
  const points = xs.map((x, idx) => ({ pred: x, realised: fitted[idx], n: bins[ks[idx]].length }));
  const fn = (p) => {
    if (p <= xs[0]) return fitted[0];
    if (p >= xs[xs.length - 1]) return fitted[fitted.length - 1];
    for (let idx = 1; idx < xs.length; idx++)
      if (p <= xs[idx]) {
        const t = (p - xs[idx - 1]) / (xs[idx] - xs[idx - 1] || 1);
        return fitted[idx - 1] + (fitted[idx] - fitted[idx - 1]) * t;
      }
    return fitted[fitted.length - 1];
  };
  return { active: true, fn, points };
}

function segmentTrust(settled) {
  const groups = {};
  for (const b of settled) (groups[b.market] = groups[b.market] || []).push(b);
  return Object.entries(groups).map(([market, bs]) => {
    const m = perfMetrics(bs);
    const signal = m.avgClv != null ? m.avgClv : m.roi;
    let raw = Math.max(0.4, Math.min(1.25, 1 + 3 * signal));
    const shrink = Math.min(1, m.n / 25);
    return { market, n: m.n, roi: m.roi, avgClv: m.avgClv, trust: 1 + (raw - 1) * shrink };
  }).sort((a, b) => b.trust - a.trust);
}

const fmtPct = (x) => (x == null || isNaN(x) ? "—" : `${(x * 100).toFixed(1)}%`);
const fmtSigned = (x) => (x == null || isNaN(x) ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%`);

/* ---- charts (hand-drawn SVG, on-brand) ---- */
function CalibrationChart({ points }) {
  const W = 340, H = 250, pad = 34;
  const X = (v) => pad + v * (W - 2 * pad);
  const Y = (v) => H - pad - v * (H - 2 * pad);
  const path = points.map((p, i) => `${i ? "L" : "M"}${X(p.pred)},${Y(p.realised)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="perf-svg" role="img" aria-label="Calibration curve">
      <rect x={pad} y={pad} width={W - 2 * pad} height={H - 2 * pad} className="perf-plot" />
      {[0, 0.5, 1].map((t) => (
        <g key={t}>
          <text x={X(t)} y={H - pad + 16} className="perf-axis" textAnchor="middle">{t}</text>
          <text x={pad - 8} y={Y(t) + 4} className="perf-axis" textAnchor="end">{t}</text>
        </g>
      ))}
      <line x1={X(0)} y1={Y(0)} x2={X(1)} y2={Y(1)} className="perf-perfect" />
      <path d={path} className="perf-curve" />
      {points.map((p, i) => (
        <circle key={i} cx={X(p.pred)} cy={Y(p.realised)} r={3.2} className="perf-dot" />
      ))}
      <text x={W / 2} y={H - 6} className="perf-axislbl" textAnchor="middle">Predicted probability</text>
      <text x={12} y={H / 2} className="perf-axislbl" textAnchor="middle" transform={`rotate(-90 12 ${H / 2})`}>Realised</text>
    </svg>
  );
}

function PnlChart({ series }) {
  const W = 340, H = 150, pad = 26;
  const min = Math.min(0, ...series), max = Math.max(0, ...series);
  const X = (i) => pad + (i / (series.length - 1)) * (W - 2 * pad);
  const Y = (v) => H - pad - ((v - min) / (max - min || 1)) * (H - 2 * pad);
  const path = series.map((v, i) => `${i ? "L" : "M"}${X(i)},${Y(v)}`).join(" ");
  const end = series[series.length - 1];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="perf-svg" role="img" aria-label="Cumulative profit and loss">
      <line x1={pad} y1={Y(0)} x2={W - pad} y2={Y(0)} className="perf-zero" />
      <path d={path} className={end >= 0 ? "perf-pnl pos" : "perf-pnl neg"} />
      <text x={W - pad} y={Y(end) - 6} className="perf-axislbl" textAnchor="end">
        {end >= 0 ? "+" : ""}{end.toFixed(1)}u
      </text>
    </svg>
  );
}

function PerformanceView({ ledger = [], onSettle = () => {}, remote = null, paperRemote = null, live = false }) {
  // headline-stats source: your real bets, or the auto-logged paper trader
  const hasPaper = !!(paperRemote && (paperRemote.n || paperRemote.open));
  const [book, setBook] = useState("real");
  const showPaper = book === "paper" && hasPaper;
  // live mode: only the real ledger counts; preview mode blends in the
  // generated sample history so the dashboard renders with zero setup
  const sample = useMemo(() => (live ? [] : generateLedger()), [live]);
  const liveSettled = ledger.filter((b) => b.status !== "open");
  const settled = useMemo(
    () => [...sample.filter((b) => b.status !== "open"), ...liveSettled],
    [sample, liveSettled]
  );
  const liveOpen = ledger.filter((b) => b.status === "open");
  const m = useMemo(() => perfMetrics(settled), [settled]);
  const cal = useMemo(() => fitCalibrator(settled), [settled]);
  const trust = useMemo(() => segmentTrust(settled), [settled]);
  const pnlSeries = useMemo(() => {
    let s = 0;
    return settled.map((b) => (s += b.pnl));
  }, [settled]);

  const sampleOpen = sample.filter((b) => b.status === "open");
  // open bets first, then every settled bet newest-first (settled already
  // contains liveSettled — don't list them twice)
  const recent = [...liveOpen, ...sampleOpen, ...settled.slice().reverse()].slice(0, 14);
  // headline metrics from the service when it answered (the ledger's truth,
  // including bets logged in earlier sessions); client-side maths otherwise
  const activeRemote = showPaper ? paperRemote : remote;
  const P = activeRemote?.profitability;
  const stats = activeRemote && activeRemote.n
    ? [
        { k: "Settled", v: String(activeRemote.n) },
        { k: "Hit rate", v: fmtPct(activeRemote.hit_rate) },
        { k: "ROI / yield", v: fmtSigned(P?.roi), c: (P?.roi ?? 0) >= 0 ? "var(--val)" : "var(--neg)" },
        { k: "Avg CLV", v: fmtSigned(P?.avg_clv), c: (P?.avg_clv ?? 0) >= 0 ? "var(--val)" : "var(--neg)" },
        { k: "Positive CLV", v: fmtPct(P?.pct_positive_clv) },
        { k: "Brier", v: activeRemote.brier == null ? "—" : activeRemote.brier.toFixed(3) },
      ]
    : [
        { k: "Settled", v: String(m.n) },
        { k: "Hit rate", v: fmtPct(m.hitRate) },
        { k: "ROI / yield", v: fmtSigned(m.roi), c: (m.roi ?? 0) >= 0 ? "var(--val)" : "var(--neg)" },
        { k: "Avg CLV", v: fmtSigned(m.avgClv), c: (m.avgClv ?? 0) >= 0 ? "var(--val)" : "var(--neg)" },
        { k: "Positive CLV", v: fmtPct(m.posClv) },
        { k: "Brier", v: m.brier == null ? "—" : m.brier.toFixed(3) },
      ];

  return (
    <div className="vt-root">
      <style>{CSS}</style>
      <header className="vt-head">
        <div className="vt-brand">
          <span className="vt-mark">▚</span>
          <div>
            <div className="vt-title">TRACK RECORD</div>
            <div className="vt-sub">recommendation history · calibration · CLV</div>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          {hasPaper && (
            <div className="vt-lineuptoggle">
              <button className={`vt-lbtn ${book === "real" ? "on" : ""}`} onClick={() => setBook("real")}>
                My bets
              </button>
              <button className={`vt-lbtn ${book === "paper" ? "on" : ""}`} onClick={() => setBook("paper")}>
                Paper trader
              </button>
            </div>
          )}
          <div className="vt-sub" style={{ maxWidth: 260, textAlign: "right" }}>
            {showPaper
              ? `Auto-logged paper picks · ${paperRemote.n} settled, ${paperRemote.open} open · 1u flat`
              : live
              ? `Live ledger · ${remote?.n ?? settled.length} settled, ${remote?.open ?? liveOpen.length} open`
              : `Sample history · ${settled.length} settled bets, generated in-memory`}
          </div>
        </div>
      </header>

      <main className="vt-main">
        <div className="perf-stats">
          {stats.map((s) => (
            <div className="perf-stat" key={s.k}>
              <span>{s.k}</span>
              <b style={{ color: s.c || "var(--bone)" }}>{s.v}</b>
            </div>
          ))}
        </div>

        {(liveOpen.length > 0 || liveSettled.length > 0) && (
          <section className="vt-sgm">
            <div className="vt-mkthead">
              <span>Your bets this session</span>
              <span className="vt-juice">logged from Markets · settle to feed the loop</span>
            </div>
            <div className="sess-list">
              {[...liveOpen, ...liveSettled].map((b) => (
                <div className="sess-row" key={b.id}>
                  <div className="sess-meta">
                    <span className="sess-fix">{b.fixture}</span>
                    <span className="sess-sel">{b.selection}</span>
                  </div>
                  <span className="vt-num">{pct(b.modelProb)}</span>
                  <span className="vt-num">{b.price.toFixed(2)}</span>
                  <span className="vt-num">{b.stake}u</span>
                  {b.status === "open" ? (
                    <div className="sess-actions">
                      <button className="sess-btn won" onClick={() => onSettle(b.id, "won")}>Won</button>
                      <button className="sess-btn lost" onClick={() => onSettle(b.id, "lost")}>Lost</button>
                    </div>
                  ) : (
                    <span className={`perf-status s-${b.status}`} style={{ textAlign: "right" }}>
                      {b.status} {b.pnl >= 0 ? "+" : ""}{b.pnl?.toFixed(2)}
                    </span>
                  )}
                </div>
              ))}
            </div>
            <p className="vt-sgmnote">
              {live
                ? "Persisted to the datalayer.service ledger; auto-settlement and CLV capture run server-side as fixtures finish."
                : "Held in memory for this session only — point API_BASE at a running datalayer.service to persist them."}
              {" "}Settling a bet updates the metrics and calibration above.
            </p>
          </section>
        )}

        {settled.length > 0 ? (
          <div className="perf-charts">
            <div className="perf-chartcard">
              <div className="perf-chead">
                <span>Calibration</span>
                <span className="vt-juice">curve below the line = overconfident</span>
              </div>
              <CalibrationChart points={cal.points} />
              <p className="perf-note">
                Curve below the diagonal = the model claims more than it delivers.
                The recommender replaces each raw probability with its calibrated
                value before computing edge. {cal.active ? "Calibration active." : "Need 30+ bets."}
              </p>
            </div>
            <div className="perf-chartcard">
              <div className="perf-chead">
                <span>Cumulative P&amp;L</span>
                <span className="vt-juice">units, settled order</span>
              </div>
              <PnlChart series={pnlSeries} />
              <p className="perf-note">
                Expect noise: long drawdowns happen with genuine edge. CLV, not
                this line, is the early read on whether the model beats the market.
              </p>
            </div>
          </div>
        ) : (
          <p className="perf-note">
            No settled bets yet. Log bets from the Markets tab; once fixtures
            finish, auto-settlement fills this dashboard from the real ledger.
          </p>
        )}

        <section className="vt-sgm">
          <div className="vt-mkthead">
            <span>Segment trust</span>
            <span className="vt-juice">edge multiplier learned from CLV · centred at 1.0</span>
          </div>
          <div className="perf-trust">
            {trust.map((t) => {
              const dev = t.trust - 1;
              const w = Math.min(50, Math.abs(dev) * 180);
              return (
                <div className="perf-trow" key={t.market}>
                  <span className="perf-tmkt">{t.market}</span>
                  <div className="perf-tbar">
                    <div className="perf-tcenter" />
                    <div
                      className="perf-tfill"
                      style={{
                        left: dev >= 0 ? "50%" : `${50 - w}%`,
                        width: `${w}%`,
                        background: dev >= 0 ? "var(--val)" : "var(--neg)",
                      }}
                    />
                  </div>
                  <span className="perf-tval" style={{ color: dev >= 0 ? "var(--val)" : "var(--neg)" }}>
                    {t.trust.toFixed(2)}×
                  </span>
                  <span className="perf-tn">n={t.n}</span>
                </div>
              );
            })}
          </div>
        </section>

        <section className="vt-sgm">
          <div className="vt-mkthead">
            <span>Recent recommendations</span>
            <span className="vt-juice">model vs calibrated · result · CLV</span>
          </div>
          <div className="perf-ledger">
            <div className="perf-lrow perf-lrow--head">
              <span>Fixture</span><span>Selection</span>
              <span className="vt-num">Model</span><span className="vt-num">Calib</span>
              <span className="vt-num">Price</span><span className="vt-num">Stake</span>
              <span>Status</span><span className="vt-num">P&amp;L</span><span className="vt-num">CLV</span>
            </div>
            {recent.map((b, idx) => {
              const calP = cal.fn(b.modelProb);
              const clv = b.closing ? b.price / b.closing - 1 : null;
              return (
                <div className="perf-lrow" key={idx}>
                  <span className="perf-lfix">{b.fixture}</span>
                  <span className="perf-lsel">{b.selection}</span>
                  <span className="vt-num">{fmtPct(b.modelProb)}</span>
                  <span className="vt-num" style={{ color: "var(--muted)" }}>{fmtPct(calP)}</span>
                  <span className="vt-num">{b.price.toFixed(2)}</span>
                  <span className="vt-num">{b.stake}u</span>
                  <span className={`perf-status s-${b.status}`}>{b.status}</span>
                  <span className="vt-num" style={{ color: b.pnl == null ? "var(--muted)" : b.pnl >= 0 ? "var(--val)" : "var(--neg)" }}>
                    {b.pnl == null ? "—" : `${b.pnl >= 0 ? "+" : ""}${b.pnl.toFixed(2)}`}
                  </span>
                  <span className="vt-num" style={{ color: clv == null ? "var(--muted)" : clv >= 0 ? "var(--val)" : "var(--neg)" }}>
                    {clv == null ? "—" : fmtSigned(clv)}
                  </span>
                </div>
              );
            })}
          </div>
        </section>

        <footer className="vt-foot">
          <span>
            {live
              ? "Reading the persistent ledger via /api/performance; calibration and segment trust update as bets resolve."
              : "History generated in-memory to preview the betlog loop. In production this reads your settled ledger."}
          </span>
          <span className="vt-disc">For analysis only · 18+ · gamble responsibly</span>
        </footer>
      </main>
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState("markets");
  const [ledger, setLedger] = useState([]);
  const [matches, setMatches] = useState(SAMPLE_MATCHES);
  const [liveFeed, setLiveFeed] = useState(false);
  const [perf, setPerf] = useState(null); // /api/performance payload when live
  const [paperPerf, setPaperPerf] = useState(null); // auto-logged paper ledger

  useEffect(() => {
    let dead = false;
    // a cold-started host may still be building its first feed (the instance
    // sleeps when idle and rebuilds on wake) — keep retrying for ~6 minutes
    // so the page flips from sample to live without a manual refresh
    let tries = 0;
    const loadFeed = () => {
      fetchFeed()
        .then((feed) => { if (!dead) { setMatches(feed); setLiveFeed(true); } })
        .catch(() => { if (!dead && tries++ < 12) setTimeout(loadFeed, 30000); });
    };
    loadFeed();
    fetch(`${API_BASE}/api/bets`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((bets) => { if (!dead) setLedger(bets.map(fromServerBet)); })
      .catch(() => {});
    fetch(`${API_BASE}/api/performance`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((p) => { if (!dead) setPerf(p); })
      .catch(() => {});
    fetch(`${API_BASE}/api/paper/performance`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((p) => { if (!dead) setPaperPerf(p); })
      .catch(() => {});
    return () => { dead = true; };
  }, []);

  const logBet = (b) => {
    const localId = Math.random().toString(36).slice(2, 8);
    setLedger((l) => [
      ...l,
      { ...b, id: localId, ts: Date.now(), status: "open", closing: null, pnl: null },
    ]);
    fetch(`${API_BASE}/api/bets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        match_id: b.fixture, market: b.market, selection: b.selection,
        model_prob: b.modelProb, price: b.price, stake: b.stake,
      }),
    })
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(({ id }) =>
        setLedger((l) => l.map((x) => (x.id === localId ? { ...x, serverId: id } : x)))
      )
      .catch(() => {});
  };
  const settleBet = (id, result) => {
    const bet = ledger.find((b) => b.id === id);
    setLedger((l) =>
      l.map((b) =>
        b.id === id
          ? {
              ...b,
              status: result,
              closing: b.closing ?? b.price,
              pnl: result === "won" ? +(b.stake * (b.price - 1)).toFixed(2) : -b.stake,
            }
          : b
      )
    );
    if (bet?.serverId) {
      fetch(`${API_BASE}/api/bets/${bet.serverId}/settle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ result }),
      }).catch(() => {});
    }
  };
  const openCount = ledger.filter((b) => b.status === "open").length;

  return (
    <div className="app-shell">
      <div className="app-tabs">
        <button className={`app-tab ${tab === "markets" ? "on" : ""}`} onClick={() => setTab("markets")}>
          Markets
        </button>
        <button className={`app-tab ${tab === "track" ? "on" : ""}`} onClick={() => setTab("track")}>
          Track record{ledger.length > 0 && <span className="app-badge">{ledger.length}</span>}
          {openCount > 0 && <span className="app-dot" title={`${openCount} open`} />}
        </button>
      </div>
      {tab === "markets" ? (
        <MarketsView
          key={liveFeed ? "live" : "sample"} // remount so the fixture rail resets on go-live
          matches={matches}
          feedNote={liveFeed ? `live feed · ${matches.length} matches` : "sample data"}
          onLog={logBet}
        />
      ) : (
        <PerformanceView ledger={ledger} onSettle={settleBet} remote={perf} paperRemote={paperPerf} live={liveFeed} />
      )}
    </div>
  );
}

/* ---------- styles ---------- */
const CSS = `
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
.app-shell{display:flex;flex-direction:column;gap:10px;font-family:'Space Grotesk',system-ui,sans-serif;}
.app-tabs{display:flex;gap:6px;}
.app-tab{background:#121E1B;border:1px solid rgba(236,231,218,.10);color:#7E8C86;border-radius:9px;padding:8px 18px;font:600 13px 'Space Grotesk',sans-serif;cursor:pointer;}
.app-tab:hover{background:#182824;color:#ECE7DA;}
.app-tab.on{background:#34D17F;color:#0C1413;border-color:#34D17F;}
.app-badge{display:inline-block;margin-left:7px;background:rgba(0,0,0,.18);border-radius:8px;padding:1px 6px;font-size:11px;font-family:'JetBrains Mono',monospace;}
.app-tab:not(.on) .app-badge{background:#34D17F;color:#0C1413;}
.app-dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#E2A33C;margin-left:6px;vertical-align:middle;}
.vt-logbtn{margin-top:9px;width:100%;background:transparent;border:1px solid var(--val);color:var(--val);border-radius:7px;padding:6px;font:600 12px 'Space Grotesk',sans-serif;cursor:pointer;transition:.15s;}
.vt-logbtn:hover{background:var(--val);color:var(--ink);}
.sess-list{display:flex;flex-direction:column;}
.sess-row{display:grid;grid-template-columns:1.6fr .7fr .7fr .6fr 1.1fr;align-items:center;gap:10px;padding:9px 4px;border-bottom:1px solid var(--line);}
.sess-row:last-child{border-bottom:none;}
.sess-meta{display:flex;flex-direction:column;gap:2px;}
.sess-fix{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;}
.sess-sel{font-size:13px;}
.sess-actions{display:flex;gap:6px;justify-content:flex-end;}
.sess-btn{border:1px solid var(--line);background:var(--panelHi);border-radius:6px;padding:4px 11px;font:600 11px 'Space Grotesk',sans-serif;cursor:pointer;}
.sess-btn.won{color:var(--val);}.sess-btn.won:hover{background:var(--val);color:var(--ink);}
.sess-btn.lost{color:var(--neg);}.sess-btn.lost:hover{background:var(--neg);color:var(--ink);}
.perf-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:11px;}
.perf-stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px;display:flex;flex-direction:column;gap:5px;}
.perf-stat span{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);}
.perf-stat b{font-size:21px;font-weight:600;font-family:'JetBrains Mono',monospace;}
.perf-charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:13px;}
.perf-chartcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;}
.perf-chead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;}
.perf-chead>span:first-child{font-weight:600;font-size:14px;}
.perf-svg{width:100%;height:auto;display:block;}
.perf-plot{fill:rgba(236,231,218,.03);stroke:var(--line);}
.perf-perfect{stroke:var(--muted);stroke-width:1;stroke-dasharray:4 4;opacity:.6;}
.perf-curve{fill:none;stroke:var(--val);stroke-width:2;}
.perf-dot{fill:var(--val);}
.perf-axis{fill:var(--muted);font-size:9px;font-family:'JetBrains Mono',monospace;}
.perf-axislbl{fill:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.06em;}
.perf-zero{stroke:var(--line);stroke-width:1;}
.perf-pnl{fill:none;stroke-width:2;}
.perf-pnl.pos{stroke:var(--val);}
.perf-pnl.neg{stroke:var(--neg);}
.perf-note{margin-top:10px;color:var(--muted);font-size:11.5px;line-height:1.5;}
.perf-trust{display:flex;flex-direction:column;gap:9px;}
.perf-trow{display:grid;grid-template-columns:90px 1fr 56px 52px;align-items:center;gap:12px;}
.perf-tmkt{font-size:13px;font-weight:500;font-family:'JetBrains Mono',monospace;}
.perf-tbar{position:relative;height:14px;background:rgba(236,231,218,.05);border-radius:4px;}
.perf-tcenter{position:absolute;left:50%;top:-2px;height:18px;width:1px;background:var(--muted);opacity:.5;}
.perf-tfill{position:absolute;top:0;height:100%;border-radius:3px;opacity:.85;}
.perf-tval{font-family:'JetBrains Mono',monospace;font-size:13px;text-align:right;}
.perf-tn{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--muted);text-align:right;}
.perf-ledger{display:flex;flex-direction:column;}
.perf-lrow{display:grid;grid-template-columns:1fr 1.4fr .7fr .7fr .6fr .6fr .8fr .8fr .8fr;align-items:center;gap:8px;padding:9px 4px;border-bottom:1px solid var(--line);}
.perf-lrow--head{font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);}
.perf-lrow--head .vt-num{font-family:'Space Grotesk',sans-serif;}
.perf-lfix{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;}
.perf-lsel{font-size:13px;}
.perf-status{font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:600;}
.perf-status.s-won{color:var(--val);}
.perf-status.s-lost{color:var(--neg);}
.perf-status.s-open{color:var(--amber);}
.cat-groups{display:flex;flex-direction:column;gap:8px;}
.cat-group{border:1px solid var(--line);border-radius:10px;overflow:hidden;}
.cat-ghead{width:100%;display:flex;justify-content:space-between;align-items:center;background:var(--panel);border:none;color:var(--bone);padding:12px 14px;font:600 14px 'Space Grotesk',sans-serif;cursor:pointer;}
.cat-ghead:hover{background:var(--panelHi);}
.cat-ghead.open{background:var(--panelHi);border-bottom:1px solid var(--line);}
.cat-gcount{color:var(--muted);font-size:11px;font-weight:400;font-family:'JetBrains Mono',monospace;}
.cat-gbody{padding:12px 14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;}
.cat-mname{font-size:12px;font-weight:600;color:var(--bone);margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em;}
.cat-note{color:var(--amber);font-style:normal;font-size:10px;font-weight:500;}
.cat-table{display:flex;flex-direction:column;}
.cat-row--head{font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);}
.cat-row--head .vt-num{font-family:'Space Grotesk',sans-serif;font-size:9px;}
.cat-row{display:grid;grid-template-columns:1.4fr .7fr .7fr .85fr .8fr;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--line);}
.cat-row:last-child{border-bottom:none;}
.cat-olabel{font-size:13px;}
.cat-hit{color:var(--bone);}
.cat-fair{color:var(--muted);}
.cat-input{background:var(--panelHi);border:1px solid var(--line);border-radius:6px;color:var(--bone);padding:3px 6px;width:60px;font-size:12px;outline:none;text-align:right;font-family:'JetBrains Mono',monospace;}
.cat-input:focus{border-color:var(--val);}
.vt-root{
  --ink:#0C1413;--panel:#121E1B;--panelHi:#182824;--line:rgba(236,231,218,.10);
  --bone:#ECE7DA;--muted:#7E8C86;--val:#34D17F;--valdim:rgba(52,209,127,.13);
  --amber:#E2A33C;--neg:#D45A41;
  background:var(--ink);color:var(--bone);font-family:'Space Grotesk',system-ui,sans-serif;
  border-radius:14px;overflow:hidden;line-height:1.45;border:1px solid var(--line);
}
.vt-root *{box-sizing:border-box;margin:0;}
.vt-num,.vt-cardodds,.vt-b365,.vt-fair,.vt-hit,input{font-family:'JetBrains Mono',ui-monospace,monospace;font-variant-numeric:tabular-nums;}
.vt-head{display:flex;justify-content:space-between;align-items:center;gap:24px;padding:18px 22px;border-bottom:1px solid var(--line);flex-wrap:wrap;}
.vt-brand{display:flex;align-items:center;gap:12px;}
.vt-mark{font-size:26px;color:var(--val);}
.vt-title{font-weight:700;letter-spacing:.16em;font-size:14px;}
.vt-sub{color:var(--muted);font-size:12px;letter-spacing:.04em;}
.vt-controls{display:flex;gap:20px;flex-wrap:wrap;}
.vt-ctrl{display:flex;flex-direction:column;gap:6px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;min-width:140px;}
.vt-inputwrap{display:flex;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:4px 10px;}
.vt-inputwrap em{color:var(--muted);font-style:normal;margin-right:4px;}
.vt-inputwrap input{background:none;border:none;color:var(--bone);width:70px;font-size:14px;outline:none;}
input[type=range]{accent-color:var(--val);cursor:pointer;}
.vt-grid{display:grid;grid-template-columns:230px 1fr;}
.vt-rail{border-right:1px solid var(--line);padding:14px;display:flex;flex-direction:column;gap:8px;}
.vt-railhead{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);padding:4px 6px;}
.vt-match{text-align:left;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 12px;cursor:pointer;color:var(--bone);transition:.15s;}
.vt-match:hover{background:var(--panelHi);}
.vt-match.is-active{border-color:var(--val);background:var(--panelHi);}
.vt-matchtop{display:flex;justify-content:space-between;align-items:center;gap:8px;}
.vt-teams{font-weight:600;font-size:13px;}
.vt-teams i{color:var(--muted);font-style:normal;font-weight:400;}
.vt-matchmeta{color:var(--muted);font-size:11px;margin-top:3px;}
.vt-live{font-size:9px;font-weight:700;letter-spacing:.1em;color:var(--ink);background:var(--val);border-radius:4px;padding:2px 5px;animation:pulse 2s infinite;}
.vt-live.sm{margin-left:10px;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.vt-main{padding:20px 24px;display:flex;flex-direction:column;gap:22px;min-width:0;}
.vt-matchhero{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;flex-wrap:wrap;}
.vt-matchhero h2{font-size:24px;font-weight:700;letter-spacing:-.01em;}
.vt-vs{color:var(--muted);font-weight:400;}
.vt-herometa{color:var(--muted);font-size:13px;margin-top:4px;}
.vt-model{display:flex;flex-direction:column;gap:10px;min-width:260px;}
.vt-xg{display:flex;gap:10px;}
.vt-xg label{display:flex;flex-direction:column;gap:4px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;}
.vt-xg input{background:var(--panel);border:1px solid var(--line);border-radius:7px;color:var(--bone);padding:5px 8px;width:88px;font-size:13px;outline:none;}
.vt-blend{display:flex;flex-direction:column;gap:7px;font-size:11px;color:var(--muted);letter-spacing:.02em;}
.vt-best{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;}
.vt-besthead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:13px;}
.vt-besthead>span:first-child{font-weight:600;font-size:14px;}
.vt-bestnote{color:var(--muted);font-size:11px;}
.vt-bestrow{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:11px;}
.vt-card{background:var(--panelHi);border:1px solid var(--val);border-radius:10px;padding:12px;position:relative;overflow:hidden;}
.vt-card::before{content:"";position:absolute;inset:0;background:var(--valdim);pointer-events:none;}
.vt-card>*{position:relative;}
.vt-cardmkt{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);}
.vt-cardlabel{font-weight:600;font-size:15px;margin:2px 0 6px;}
.vt-cardodds{font-size:26px;font-weight:600;color:var(--val);}
.vt-cardrow{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:5px;}
.vt-cardrow b{color:var(--bone);font-family:'JetBrains Mono',monospace;}
.vt-cardstake{margin-top:9px;padding-top:9px;border-top:1px solid var(--line);font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--val);}
.vt-empty{color:var(--muted);font-size:13px;padding:10px 2px;}
.vt-mkt,.vt-sgm{display:flex;flex-direction:column;}
.vt-mkthead{display:flex;justify-content:space-between;align-items:baseline;padding:0 2px 9px;border-bottom:1px solid var(--line);margin-bottom:8px;}
.vt-mkthead>span:first-child{font-weight:600;font-size:14px;}
.vt-juice{color:var(--muted);font-size:11px;font-family:'JetBrains Mono',monospace;}
.vt-table{display:flex;flex-direction:column;}
.vt-trow{display:grid;grid-template-columns:1.1fr .65fr .65fr .7fr 1.9fr .75fr .6fr;align-items:center;gap:10px;padding:11px 4px;border-bottom:1px solid var(--line);}
.vt-trow--head{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);}
.vt-trow--head .vt-num,.vt-trow--head .vt-bar{font-family:'Space Grotesk',sans-serif;}
.vt-trow.is-value{background:linear-gradient(90deg,var(--valdim),transparent);}
.vt-outcome{font-weight:500;font-size:14px;}
.vt-num{text-align:right;font-size:14px;}
.vt-b365{font-weight:600;font-size:15px;}
.vt-fair{color:var(--muted);}
.vt-hit{color:var(--bone);}
.vt-stake{color:var(--val);}
.vt-bar{padding-right:6px;}
.vbar{position:relative;height:18px;width:100%;}
.vbar-track{position:absolute;inset:0;background:rgba(236,231,218,.05);border-radius:4px;}
.vbar-fair{position:absolute;top:0;left:0;height:100%;background:rgba(236,231,218,.16);border-radius:4px;}
.vbar-slice{position:absolute;top:0;height:100%;opacity:.85;border-radius:2px;}
.vbar-tick{position:absolute;top:-2px;height:22px;width:2px;background:var(--bone);transform:translateX(-1px);}
.vt-legpool{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:14px;}
.vt-leggrouphd{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin-bottom:7px;}
.vt-legchips{display:flex;flex-wrap:wrap;gap:6px;}
.vt-chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;color:var(--bone);padding:5px 11px;font-size:12px;cursor:pointer;transition:.15s;}
.vt-chip:hover{background:var(--panelHi);}
.vt-chip.on{background:var(--val);color:var(--ink);border-color:var(--val);font-weight:600;}
.vt-sgmresult{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;}
.vt-sgmlegs{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:14px;}
.vt-sgmleg{background:var(--panelHi);border:1px solid var(--line);border-radius:6px;padding:4px 9px;font-size:12px;font-weight:500;}
.vt-sgmgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:14px;}
.vt-stat{display:flex;flex-direction:column;gap:4px;}
.vt-stat>span{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);}
.vt-stat b{font-size:19px;font-weight:600;font-family:'JetBrains Mono',monospace;}
.vt-stat b.dim{color:var(--muted);}
.vt-stat b.val{color:var(--val);}
.vt-bookinput input{background:var(--panelHi);border:1px solid var(--val);border-radius:7px;color:var(--bone);padding:4px 8px;width:90px;font-size:17px;outline:none;}
.vt-sgmnote{margin-top:14px;color:var(--muted);font-size:11.5px;line-height:1.5;}
.vt-targets{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;}
.vt-tbtn{background:var(--panel);border:1px solid var(--line);border-radius:8px;color:var(--bone);padding:6px 14px;font-size:13px;cursor:pointer;font-family:'JetBrains Mono',monospace;}
.vt-tbtn.on{background:var(--val);color:var(--ink);border-color:var(--val);font-weight:600;}
.vt-suggrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:11px;}
.vt-sugcard{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px;}
.vt-suglegs{display:flex;flex-direction:column;gap:4px;margin-bottom:11px;}
.vt-suglegs span{font-size:13px;font-weight:500;}
.vt-sugfoot{display:flex;justify-content:space-between;border-top:1px solid var(--line);padding-top:10px;}
.vt-sugfoot div{display:flex;flex-direction:column;gap:2px;}
.vt-sugfoot em{font-style:normal;font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);}
.vt-sugfoot b{font-size:14px;font-family:'JetBrains Mono',monospace;}
.vt-foot{display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:11px;padding-top:6px;border-top:1px solid var(--line);}
.vt-disc{color:var(--amber);letter-spacing:.04em;}
.vt-plrcontrols{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;flex-wrap:wrap;margin-bottom:16px;}
.vt-lineuptoggle{display:flex;gap:0;border:1px solid var(--line);border-radius:9px;overflow:hidden;}
.vt-lbtn{background:var(--panel);border:none;color:var(--muted);padding:8px 16px;font-size:13px;cursor:pointer;font-family:inherit;}
.vt-lbtn.on{background:var(--val);color:var(--ink);font-weight:600;}
.vt-plrgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:13px;}
.vt-plrcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;}
.vt-plrhead{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:11px;}
.vt-plrhead--toggle{width:100%;background:none;border:none;color:var(--bone);padding:0;cursor:pointer;font-family:inherit;}
.vt-plrhead--toggle:hover .vt-plrname{color:var(--val);}
.vt-fam{border-top:1px solid var(--line);}
.vt-fam:first-child{border-top:none;}
.vt-famhead{width:100%;display:flex;justify-content:space-between;align-items:center;background:none;border:none;color:var(--bone);padding:8px 2px;font:600 12px 'Space Grotesk',sans-serif;cursor:pointer;}
.vt-famhead:hover{color:var(--val);}
.vt-famr{display:flex;align-items:center;gap:10px;}
.vt-faml5{font-style:normal;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--muted);font-weight:400;}
.vt-plrname{font-weight:600;font-size:15px;}
.vt-plrmeta{color:var(--muted);font-size:11px;margin-top:2px;}
.vt-poschip{background:var(--panelHi);border:1px solid var(--line);border-radius:7px;padding:4px 9px;font-size:12px;font-weight:600;font-family:'JetBrains Mono',monospace;}
.vt-poschip.moved{border-color:var(--val);color:var(--val);}
.vt-plrtags{display:flex;align-items:center;gap:5px;}
.vt-sptag{font-size:9px;font-weight:700;letter-spacing:.05em;padding:3px 6px;border-radius:5px;font-family:'JetBrains Mono',monospace;}
.vt-sptag.pen{background:var(--val);color:var(--ink);}
.vt-sptag.fk{background:rgba(226,165,60,.18);color:var(--amber);border:1px solid var(--amber);}
.vt-poschip i{font-style:normal;color:var(--muted);font-weight:400;font-size:10px;}
.vt-proptable{display:flex;flex-direction:column;}
.vt-prow{display:grid;grid-template-columns:1.5fr .7fr .7fr .85fr .8fr;align-items:center;gap:8px;padding:7px 2px;border-bottom:1px solid var(--line);}
.vt-prow:last-child{border-bottom:none;}
.vt-prow--head{font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);}
.vt-prow--head .vt-num{font-family:'Space Grotesk',sans-serif;}
.vt-pmkt{font-size:13px;font-weight:500;}
.vt-pinput{background:var(--panelHi);border:1px solid var(--line);border-radius:6px;color:var(--bone);padding:3px 6px;width:62px;font-size:13px;outline:none;text-align:right;}
.vt-pinput:focus{border-color:var(--val);}
@media (max-width:720px){
  .vt-grid{grid-template-columns:1fr;}
  .vt-rail{flex-direction:row;overflow-x:auto;border-right:none;border-bottom:1px solid var(--line);}
  .vt-match{min-width:180px;}
  .vt-trow{grid-template-columns:1fr .7fr .7fr .7fr;}
  .vt-trow .vt-bar,.vt-trow--head .vt-bar,.vt-trow .vt-stake,.vt-trow--head .vt-num:last-child{display:none;}
  .vt-plrcontrols{flex-direction:column;align-items:stretch;}
}
@media (prefers-reduced-motion:reduce){.vt-live{animation:none;}}
`;
