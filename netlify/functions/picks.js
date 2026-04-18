// netlify/functions/picks.js
// Computes value picks and accumulator combos from latest odds data
// Calls the matches function internally to avoid duplicate API calls

const { handler: matchesHandler } = require("./matches");

/* MIN_EDGE = probability edge as fraction (e.g. 0.025 = 2.5 pp vs implied). */
const MIN_EDGE = parseFloat(process.env.MIN_EDGE || "0.025");
const MIN_CONF = parseFloat(process.env.MIN_CONFIDENCE || "52");
const KELLY_FRACTION = parseFloat(process.env.KELLY_FRACTION || "0.25");
const BANKROLL = parseFloat(process.env.BANKROLL || "1000");

function fact(n) {
  if (n <= 1) return 1;
  let x = 1;
  for (let i = 2; i <= n; i++) x *= i;
  return x;
}

function poissonPmf(k, lam) {
  if (lam <= 0) return k === 0 ? 1 : 0;
  return Math.exp(-lam + k * Math.log(lam) - Math.log(fact(k)));
}

/** Independent Poisson 1X2 from xG lambdas (can disagree with market → real value signals). */
function poisson1x2(lamH, lamA, maxG = 6) {
  let pH = 0;
  let pD = 0;
  let pA = 0;
  for (let i = 0; i <= maxG; i++) {
    for (let j = 0; j <= maxG; j++) {
      const p = poissonPmf(i, lamH) * poissonPmf(j, lamA);
      if (i > j) pH += p;
      else if (i === j) pD += p;
      else pA += p;
    }
  }
  const s = pH + pD + pA;
  if (s <= 0) return { H: 33.3, D: 33.3, A: 33.3 };
  return {
    H: Math.round((pH / s) * 1000) / 10,
    D: Math.round((pD / s) * 1000) / 10,
    A: Math.round((pA / s) * 1000) / 10,
  };
}

function kelly(prob, odds, fraction = KELLY_FRACTION) {
  const b = odds - 1;
  const q = 1 - prob;
  const k = (b * prob - q) / b;
  return k > 0 ? Math.round(k * fraction * BANKROLL * 100) / 100 : 0;
}

function buildAccumulators(picks, foldSizes = [3, 5, 7]) {
  const accas = {};
  for (const n of foldSizes) {
    if (picks.length < n) { accas[n] = []; continue; }
    const combos = [];
    // Generate combinations without same fixture twice
    function combine(start, current) {
      if (current.length === n) {
        const fixtures = new Set(current.map(p => p.matchId));
        if (fixtures.size === n) combos.push([...current]);
        return;
      }
      for (let i = start; i < picks.length; i++) {
        // No two picks from same match
        const alreadyUsed = current.some(p => p.matchId === picks[i].matchId);
        if (!alreadyUsed) {
          current.push(picks[i]);
          combine(i + 1, current);
          current.pop();
        }
        if (combos.length >= 200) return; // cap combinations
      }
    }
    combine(0, []);

    const scored = combos.map(legs => {
      const combinedOdds = legs.reduce((p, l) => p * l.odds, 1);
      const combinedProb = legs.reduce((p, l) => p * (l.prob / 100), 1);
      const ev = (combinedProb * combinedOdds - 1) * 100;
      const score = ev * combinedProb;
      return { legs, combinedOdds: Math.round(combinedOdds * 100) / 100, combinedProb: Math.round(combinedProb * 10000) / 100, ev: Math.round(ev * 100) / 100, score };
    });

    scored.sort((a, b) => b.score - a.score);
    accas[n] = scored.slice(0, 3);
  }
  return accas;
}

exports.handler = async (event) => {
  const headers = {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/json",
    "Cache-Control": "no-cache, max-age=0",
  };

  // Get matches data
  const matchResp = await matchesHandler({ httpMethod: "GET" });
  const matchData = JSON.parse(matchResp.body);
  const matches = matchData.matches || [];

  // Value picks: Poisson(xG) 1X2 vs implied odds (devig-vs-implied on same line is ~always ≤ 0).
  const picks = [];
  for (const m of matches) {
    if (!m.odds) continue;
    const xgH = m.xg?.home ?? 1.2;
    const xgA = m.xg?.away ?? 1.0;
    const pm = poisson1x2(xgH, xgA);

    const push = (market, code, odds, bk, pPct, edgeFrac) => {
      if (!odds || odds <= 1.12) return;
      const edgePP = Math.round(edgeFrac * 1000) / 10;
      picks.push({
        matchId: m.id,
        home: m.home,
        away: m.away,
        league: m.league,
        leagueName: m.leagueName,
        flag: m.leagueFlag,
        market,
        marketCode: code,
        odds,
        bookmaker: bk || "Best",
        prob: pPct,
        edge: edgePP,
        confidence: Math.round(pPct),
        kelly: kelly(pPct / 100, odds),
        kickoff: m.kickoff,
        xg: m.xg,
        score: Math.round(edgeFrac * pPct * 1000) / 1000,
      });
    };

    const eh = pm.H / 100 - 1 / m.odds.home;
    if (pm.H >= MIN_CONF && eh >= MIN_EDGE) {
      push("Home Win", "H", m.odds.home, m.bks?.home, pm.H, eh);
    }
    const ed = pm.D / 100 - 1 / m.odds.draw;
    if (m.odds.draw > 0 && pm.D >= MIN_CONF && ed >= MIN_EDGE) {
      push("Draw", "D", m.odds.draw, m.bks?.draw, pm.D, ed);
    }
    const ea = pm.A / 100 - 1 / m.odds.away;
    if (pm.A >= MIN_CONF && ea >= MIN_EDGE) {
      push("Away Win", "A", m.odds.away, m.bks?.away, pm.A, ea);
    }

    if (m.probs && m.odds.over25 > 1.1) {
      const pO = m.probs.O25 / 100;
      const impO = 1 / m.odds.over25;
      const eO = pO - impO;
      if (m.probs.O25 >= MIN_CONF && eO >= MIN_EDGE) {
        push("Over 2.5", "O25", m.odds.over25, m.bks?.over, m.probs.O25, eO);
      }
    }
  }

  picks.sort((a, b) => b.score - a.score);
  const accas = buildAccumulators(picks.slice(0, 45), [3, 5, 7]);

  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({
      picks: picks.slice(0, 30),
      accumulators: accas,
      stats: {
        totalMatches: matches.length,
        valuePicks: picks.length,
        liveMatches: matches.filter(m => m.isLive).length,
        todayMatches: matches.filter(m => m.isToday).length,
        leagueCount: new Set(matches.map(m => m.league)).size,
      },
      updated: new Date().toISOString(),
      demo: matchData.demo,
    }),
  };
};
