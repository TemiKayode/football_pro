// netlify/functions/matches.js
// Fetches live match data from The Odds API for top 10 leagues
// Called by the frontend every 60 seconds for real-time updates

const ODDS_API_KEY = process.env.ODDS_API_KEY;
const BASE = "https://api.the-odds-api.com/v4";

// Top 10 leagues worldwide — priority order
const TOP_LEAGUES = [
  { key: "soccer_epl",                    name: "Premier League",      country: "England",   flag: "🏴󠁧󠁢󠁥󠁮󠁧󠁿" },
  { key: "soccer_spain_la_liga",          name: "La Liga",             country: "Spain",     flag: "🇪🇸" },
  { key: "soccer_germany_bundesliga",     name: "Bundesliga",          country: "Germany",   flag: "🇩🇪" },
  { key: "soccer_italy_serie_a",          name: "Serie A",             country: "Italy",     flag: "🇮🇹" },
  { key: "soccer_france_ligue_one",       name: "Ligue 1",             country: "France",    flag: "🇫🇷" },
  { key: "soccer_champions_league",       name: "Champions League",    country: "Europe",    flag: "🏆" },
  { key: "soccer_netherlands_eredivisie", name: "Eredivisie",          country: "Netherlands",flag: "🇳🇱" },
  { key: "soccer_portugal_primeira_liga", name: "Primeira Liga",       country: "Portugal",  flag: "🇵🇹" },
  { key: "soccer_brazil_campeonato",      name: "Brasileirão",         country: "Brazil",    flag: "🇧🇷" },
  { key: "soccer_argentina_primera_division", name: "Liga Profesional",country: "Argentina", flag: "🇦🇷" },
];

// Remove vig from odds and return fair probabilities
function removVig(h, d, a) {
  if (!h || !d || !a || h <= 0 || d <= 0 || a <= 0) return null;
  const total = 1/h + 1/d + 1/a;
  return {
    H: Math.round((1/h) / total * 1000) / 10,
    D: Math.round((1/d) / total * 1000) / 10,
    A: Math.round((1/a) / total * 1000) / 10,
  };
}

// Simple Poisson xG estimation from implied probabilities
function estimateXG(pH, pA) {
  const pH_dec = pH / 100;
  const pA_dec = pA / 100;
  // Rough inverse: solve lambda from win probability
  // lambda_home ≈ -ln(1 - pH) * 1.35 (calibrated constant)
  const xgH = Math.max(0.5, Math.min(4.5, -Math.log(1 - Math.min(pH_dec, 0.99)) * 1.35));
  const xgA = Math.max(0.3, Math.min(3.5, -Math.log(1 - Math.min(pA_dec, 0.99)) * 1.1));
  return { xgH: Math.round(xgH * 100) / 100, xgA: Math.round(xgA * 100) / 100 };
}

// Poisson over 2.5 probability
function over25Prob(xgH, xgA) {
  let under = 0;
  for (let i = 0; i <= 2; i++) {
    for (let j = 0; j <= 2 - i; j++) {
      const pH = Math.exp(-xgH) * Math.pow(xgH, i) / factorial(i);
      const pA = Math.exp(-xgA) * Math.pow(xgA, j) / factorial(j);
      under += pH * pA;
    }
  }
  return Math.round((1 - under) * 1000) / 10;
}

function factorial(n) {
  return n <= 1 ? 1 : n * factorial(n - 1);
}

// Kelly criterion
function kelly(prob, odds, fraction = 0.25) {
  const b = odds - 1;
  const q = 1 - prob;
  const k = (b * prob - q) / b;
  return k > 0 ? Math.round(k * fraction * 100) / 100 : 0;
}

async function fetchLeague(leagueKey) {
  const url = `${BASE}/sports/${leagueKey}/odds?apiKey=${ODDS_API_KEY}&regions=uk,eu&markets=h2h,totals&oddsFormat=decimal`;
  try {
    const resp = await fetch(url, { signal: AbortSignal.timeout(8000) });
    if (!resp.ok) return [];
    const events = await resp.json();
    const league = TOP_LEAGUES.find(l => l.key === leagueKey);

    return events.map(ev => {
      // Best odds across all bookmakers
      let bestH = 0, bestD = 0, bestA = 0, bestBkH = "", bestBkD = "", bestBkA = "";
      let bestO25 = 0, bestU25 = 0, bestBkO = "", bestBkU = "";

      for (const bk of (ev.bookmakers || [])) {
        for (const mkt of (bk.markets || [])) {
          if (mkt.key === "h2h") {
            const byName = {};
            for (const o of mkt.outcomes) byName[o.name] = o.price;
            const h = byName[ev.home_team] || 0;
            const d = byName["Draw"] || 0;
            const a = byName[ev.away_team] || 0;
            if (h > bestH) { bestH = h; bestBkH = bk.title; }
            if (d > bestD) { bestD = d; bestBkD = bk.title; }
            if (a > bestA) { bestA = a; bestBkA = bk.title; }
          }
          if (mkt.key === "totals") {
            for (const o of mkt.outcomes) {
              const pt = String(o.description || o.point || "");
              if (pt.includes("2.5")) {
                if (o.name === "Over"  && o.price > bestO25) { bestO25 = o.price; bestBkO = bk.title; }
                if (o.name === "Under" && o.price > bestU25) { bestU25 = o.price; bestBkU = bk.title; }
              }
            }
          }
        }
      }

      const probs = removVig(bestH, bestD, bestA);
      if (!probs) return null;

      const { xgH, xgA } = estimateXG(probs.H, probs.A);
      const o25 = over25Prob(xgH, xgA);
      const edge = {
        H:   probs.H - Math.round(100/bestH * 10) / 10,
        D:   probs.D - Math.round(100/bestD * 10) / 10,
        A:   probs.A - Math.round(100/bestA * 10) / 10,
      };

      const now = new Date();
      const kickoff = new Date(ev.commence_time);
      const minsToKO = Math.round((kickoff - now) / 60000);
      const isLive = minsToKO < 0 && minsToKO > -110;
      const isToday = kickoff.toDateString() === now.toDateString();
      const isTomorrow = kickoff.toDateString() === new Date(now.getTime() + 86400000).toDateString();

      return {
        id: ev.id,
        home: ev.home_team,
        away: ev.away_team,
        league: leagueKey,
        leagueName: league?.name || leagueKey,
        leagueFlag: league?.flag || "⚽",
        country: league?.country || "",
        kickoff: ev.commence_time,
        minsToKO,
        isLive,
        isToday,
        isTomorrow,
        odds: { home: bestH, draw: bestD, away: bestA, over25: bestO25, under25: bestU25 },
        bks: { home: bestBkH, draw: bestBkD, away: bestBkA, over: bestBkO, under: bestBkU },
        probs: { H: probs.H, D: probs.D, A: probs.A, O25: o25, U25: 100 - o25 },
        xg: { home: xgH, away: xgA },
        edge,
        valueH: edge.H > 3,
        valueD: edge.D > 3,
        valueA: edge.A > 3,
        valueO25: (o25 - (bestO25 > 0 ? Math.round(100/bestO25*10)/10 : 0)) > 3,
        kellyH: kelly(probs.H/100, bestH),
        kellyA: kelly(probs.A/100, bestA),
      };
    }).filter(Boolean);

  } catch (e) {
    console.error(`Error fetching ${leagueKey}:`, e.message);
    return [];
  }
}

exports.handler = async (event) => {
  const headers = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json",
    "Cache-Control": "no-cache, max-age=0",
  };

  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 200, headers, body: "" };
  }

  if (!ODDS_API_KEY) {
    // Return demo data so the UI works without a key
    return {
      statusCode: 200,
      headers,
      body: JSON.stringify({ demo: true, matches: getDemoMatches(), updated: new Date().toISOString(), leagues: TOP_LEAGUES }),
    };
  }

  // Fetch all leagues in parallel (faster than sequential)
  const results = await Promise.allSettled(TOP_LEAGUES.map(l => fetchLeague(l.key)));
  const allMatches = results.flatMap((r, i) => {
    if (r.status === "fulfilled") return r.value;
    console.error(`League ${TOP_LEAGUES[i].key} failed:`, r.reason);
    return [];
  });

  // Sort: live first, then by kickoff
  allMatches.sort((a, b) => {
    if (a.isLive && !b.isLive) return -1;
    if (!a.isLive && b.isLive) return 1;
    return new Date(a.kickoff) - new Date(b.kickoff);
  });

  if (allMatches.length === 0) {
    // If quota is exhausted or provider has no rows, keep the UI useful with demo fixtures.
    return {
      statusCode: 200,
      headers,
      body: JSON.stringify({
        demo: true,
        matches: getDemoMatches(),
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: TOP_LEAGUES.length,
        note: "Live odds temporarily unavailable (quota/provider). Showing demo fixtures.",
      }),
    };
  }

  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({
      demo: false,
      matches: allMatches,
      updated: new Date().toISOString(),
      leagues: TOP_LEAGUES,
      totalLeagues: new Set(allMatches.map(m => m.league)).size,
    }),
  };
};

function getDemoMatches() {
  const now = new Date();
  const demos = [
    { home: "Manchester City", away: "Arsenal",    hO: 2.05, dO: 3.40, aO: 3.80, lg: "soccer_epl",                    offset: 90 },
    { home: "Real Madrid",     away: "Barcelona",  hO: 2.30, dO: 3.20, aO: 3.10, lg: "soccer_spain_la_liga",          offset: 180 },
    { home: "Bayern Munich",   away: "Dortmund",   hO: 1.55, dO: 3.80, aO: 6.50, lg: "soccer_germany_bundesliga",     offset: 270 },
    { home: "PSG",             away: "Lyon",        hO: 1.55, dO: 3.80, aO: 5.50, lg: "soccer_france_ligue_one",       offset: -30 },
    { home: "Inter Milan",     away: "Juventus",   hO: 1.90, dO: 3.40, aO: 4.20, lg: "soccer_italy_serie_a",          offset: 360 },
    { home: "Ajax",            away: "PSV",         hO: 2.10, dO: 3.20, aO: 3.60, lg: "soccer_netherlands_eredivisie", offset: 420 },
    { home: "Porto",           away: "Benfica",     hO: 2.50, dO: 3.10, aO: 2.90, lg: "soccer_portugal_primeira_liga", offset: 510 },
    { home: "Flamengo",        away: "Palmeiras",  hO: 2.20, dO: 3.20, aO: 3.30, lg: "soccer_brazil_campeonato",      offset: 600 },
    { home: "River Plate",     away: "Boca Juniors",hO:2.10, dO: 3.10, aO: 3.50, lg: "soccer_argentina_primera_division", offset: 690 },
    { home: "Man United",      away: "Liverpool",  hO: 3.20, dO: 3.10, aO: 2.30, lg: "soccer_epl",                    offset: 780 },
  ];

  return demos.map((d, i) => {
    const ko = new Date(now.getTime() + d.offset * 60000);
    const minsToKO = d.offset;
    const lg = TOP_LEAGUES.find(l => l.key === d.lg);
    const probs = removVig(d.hO, d.dO, d.aO);
    const { xgH, xgA } = estimateXG(probs.H, probs.A);
    return {
      id: `demo_${i}`,
      home: d.home, away: d.away,
      league: d.lg, leagueName: lg?.name, leagueFlag: lg?.flag, country: lg?.country,
      kickoff: ko.toISOString(), minsToKO,
      isLive: minsToKO < 0 && minsToKO > -110,
      isToday: true, isTomorrow: false,
      odds: { home: d.hO, draw: d.dO, away: d.aO, over25: 1.85, under25: 1.95 },
      bks: { home: "Demo", draw: "Demo", away: "Demo", over: "Demo", under: "Demo" },
      probs: { H: probs.H, D: probs.D, A: probs.A, O25: over25Prob(xgH, xgA), U25: 100 - over25Prob(xgH, xgA) },
      xg: { home: xgH, away: xgA },
      edge: { H: 2.1, D: 1.5, A: 1.8 },
      valueH: false, valueD: false, valueA: false, valueO25: false,
      kellyH: kelly(probs.H/100, d.hO), kellyA: kelly(probs.A/100, d.aO),
    };
  });
}
