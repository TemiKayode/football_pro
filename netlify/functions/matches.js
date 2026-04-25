// netlify/functions/matches.js
// Fetches live match data from The Odds API for top 10 leagues
// Called by the frontend every 60 seconds for real-time updates

const ODDS_API_KEY = process.env.ODDS_API_KEY;
const API_FOOTBALL_KEY = process.env.API_FOOTBALL_KEY || "";
const BASE = "https://api.the-odds-api.com/v4";
const { jsonHeaders, rateLimit, requireAuth } = require("./_lib/auth");
const { getAllScoreboardRows } = require("./scores");

// Maps API-Football numeric league IDs to Odds API sport keys (used by dropdown filter)
const AF_LEAGUE_ID_TO_KEY = {
  39: "soccer_epl",
  140: "soccer_spain_la_liga",
  78: "soccer_germany_bundesliga",
  135: "soccer_italy_serie_a",
  61: "soccer_france_ligue_one",
  2: "soccer_champions_league",
  3: "soccer_europa_league",
  848: "soccer_uefa_europa_conference_league",
  88: "soccer_netherlands_eredivisie",
  94: "soccer_portugal_primeira_liga",
  71: "soccer_brazil_campeonato",
  128: "soccer_argentina_primera_division",
};

// Maps football-data.org competition IDs to Odds API sport keys
const FD_COMP_ID_TO_KEY = {
  PL:   "soccer_epl",
  PD:   "soccer_spain_la_liga",
  BL1:  "soccer_germany_bundesliga",
  SA:   "soccer_italy_serie_a",
  FL1:  "soccer_france_ligue_one",
  CL:   "soccer_champions_league",
  EL:   "soccer_europa_league",
  UEL:  "soccer_europa_league",
  UECL: "soccer_uefa_europa_conference_league",
  ECL:  "soccer_uefa_europa_conference_league",
  DED:  "soccer_netherlands_eredivisie",
  PPL:  "soccer_portugal_primeira_liga",
  BSA:  "soccer_brazil_campeonato",
  CLI:  "soccer_argentina_primera_division",
};
let cachedResponse = null;
let cachedAt = 0;

function buildEmptyMatches(note) {
  return {
    dataMode: "none",
    matches: [],
    updated: new Date().toISOString(),
    leagues: TOP_LEAGUES,
    totalLeagues: 0,
    stale: true,
    note: note || "No live data available. Configure ODDS_API_KEY for odds, and FOOTBALL_DATA_KEY or API_FOOTBALL_KEY for fixtures.",
  };
}

// Top 10 leagues worldwide — priority order
const TOP_LEAGUES = [
  { key: "soccer_epl",                    name: "Premier League",      country: "England",   flag: "🏴󠁧󠁢󠁥󠁮󠁧󠁿" },
  { key: "soccer_spain_la_liga",          name: "La Liga",             country: "Spain",     flag: "🇪🇸" },
  { key: "soccer_germany_bundesliga",     name: "Bundesliga",          country: "Germany",   flag: "🇩🇪" },
  { key: "soccer_italy_serie_a",          name: "Serie A",             country: "Italy",     flag: "🇮🇹" },
  { key: "soccer_france_ligue_one",       name: "Ligue 1",             country: "France",    flag: "🇫🇷" },
  { key: "soccer_champions_league",       name: "Champions League",    country: "Europe",    flag: "🏆" },
  { key: "soccer_europa_league",          name: "Europa League",       country: "Europe",    flag: "🏆" },
  { key: "soccer_uefa_europa_conference_league", name: "Conference League", country: "Europe", flag: "🏆" },
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
        modelReady: true,
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

function fixtureRowsToMatches(rows) {
  const now = Date.now();
  return (rows || []).map((m) => {
    const kickoff = m.kickoff || new Date().toISOString();
    const minsToKO = Math.round((new Date(kickoff).getTime() - now) / 60000);
    return {
      id: `fd_${m.id}`,
      home: m.home,
      away: m.away,
      league: FD_COMP_ID_TO_KEY[m.competition] || (m.competition || "football").toLowerCase(),
      leagueName: m.compName || m.competition || "Football",
      leagueFlag: m.flag || "⚽",
      country: "",
      kickoff,
      minsToKO,
      isLive: ["IN_PLAY", "PAUSED", "LIVE"].includes(m.status),
      isToday: new Date(kickoff).toDateString() === new Date().toDateString(),
      isTomorrow:
        new Date(kickoff).toDateString() ===
        new Date(Date.now() + 86400000).toDateString(),
      odds: { home: 0, draw: 0, away: 0, over25: 0, under25: 0 },
      bks: { home: "", draw: "", away: "", over: "", under: "" },
      probs: null,
      xg: null,
      modelReady: false,
      source: "football-data",
    };
  });
}

const AF_LIVE_SHORT = new Set(["1H", "HT", "2H", "ET", "P", "BT", "LIVE", "INT"]);

function mapApiFootballFixtureItem(item) {
  const f = item?.fixture || {};
  const teams = item?.teams || {};
  const league = item?.league || {};
  const kickoff = f?.date || new Date().toISOString();
  if (!f?.id) return null;
  const short = f?.status?.short || "";
  return {
    id: `af_${f.id}`,
    home: teams?.home?.name || "Home",
    away: teams?.away?.name || "Away",
    league: AF_LEAGUE_ID_TO_KEY[league?.id] || String(league?.id || "api-football"),
    leagueName: league?.name || "Football",
    leagueFlag: league?.country || "⚽",
    country: league?.country || "",
    kickoff,
    minsToKO: Math.round((new Date(kickoff).getTime() - Date.now()) / 60000),
    isLive: AF_LIVE_SHORT.has(short),
    isToday: new Date(kickoff).toDateString() === new Date().toDateString(),
    isTomorrow:
      new Date(kickoff).toDateString() === new Date(Date.now() + 86400000).toDateString(),
    odds: { home: 0, draw: 0, away: 0, over25: 0, under25: 0 },
    bks: { home: "", draw: "", away: "", over: "", under: "" },
    probs: null,
    xg: null,
    modelReady: false,
    source: "api-football",
  };
}

async function parseApiFootballResponse(resp) {
  if (!resp.ok) return [];
  const data = await resp.json();
  if (data?.errors && Object.keys(data.errors).length) return [];
  if (!Array.isArray(data?.response)) return [];
  return data.response.map(mapApiFootballFixtureItem).filter(Boolean);
}

/**
 * API-Football: free tier rejects `from`/`to` without extra params; `date=YYYY-MM-DD` works.
 * We fetch each day in the window plus `live=all`, then merge by fixture id.
 */
async function getApiFootballFixtures() {
  if (!API_FOOTBALL_KEY) return [];
  const days = Math.min(14, Math.max(1, Number.parseInt(process.env.FIXTURES_DAYS || "7", 10)));
  const maxRows = Number.parseInt(process.env.MAX_AF_FIXTURES || "800", 10);
  const headers = { "x-apisports-key": API_FOOTBALL_KEY };
  const opts = { headers, signal: AbortSignal.timeout(15000) };
  try {
    const dates = [];
    for (let i = 0; i < days; i++) {
      dates.push(new Date(Date.now() + i * 86400000).toISOString().slice(0, 10));
    }
    const dateUrls = dates.map((d) => `https://v3.football.api-sports.io/fixtures?date=${d}`);
    const liveUrl = "https://v3.football.api-sports.io/fixtures?live=all";
    const allResps = await Promise.all([...dateUrls.map((u) => fetch(u, opts)), fetch(liveUrl, opts)]);
    const byId = new Map();
    for (const resp of allResps) {
      const rows = await parseApiFootballResponse(resp);
      for (const r of rows) {
        if (!byId.has(r.id)) byId.set(r.id, r);
        else if (r.isLive) byId.set(r.id, r);
      }
    }
    const merged = [...byId.values()];
    return takeRelevantMatchesFirst(merged, Math.max(50, maxRows));
  } catch {
    return [];
  }
}

/** Prefer live + upcoming (soonest first), then recent past — not the oldest 800 globally. */
function takeRelevantMatchesFirst(matches, maxRows) {
  const now = Date.now();
  const liveCut = now - 3 * 3600000;
  const upcoming = [];
  const older = [];
  for (const m of matches) {
    const t = new Date(m.kickoff).getTime();
    if (t >= liveCut) upcoming.push(m);
    else older.push(m);
  }
  upcoming.sort((a, b) => new Date(a.kickoff) - new Date(b.kickoff));
  older.sort((a, b) => new Date(b.kickoff) - new Date(a.kickoff));
  const merged = [...upcoming, ...older];
  return merged.slice(0, maxRows);
}

exports.handler = async (event) => {
  const headers = jsonHeaders({ "Cache-Control": "no-cache, max-age=0" });

  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 200, headers, body: "" };
  }

  const publicMode = event?.queryStringParameters?.public === "1";
  const rl = rateLimit(event, "matches", publicMode ? 240 : 120, 60000);
  if (!rl.allowed) {
    return { statusCode: 429, headers, body: JSON.stringify({ ok: false, error: "Rate limit exceeded." }) };
  }

  if (!publicMode) {
    const auth = await requireAuth(event);
    if (!auth.ok) return auth.response;
  }

  const nowMs = Date.now();
  if (cachedResponse && nowMs - cachedAt < 30000) {
    console.log(JSON.stringify({ level: "info", event: "matches_cache_hit", ageMs: nowMs - cachedAt }));
    return { statusCode: 200, headers, body: JSON.stringify(cachedResponse) };
  }

  if (!ODDS_API_KEY) {
    // Prefer real fixtures if football-data key is available.
    const rows = await getAllScoreboardRows().catch(() => []);
    if (rows.length) {
      const fx = fixtureRowsToMatches(rows);
      const payload = {
        dataMode: "fixtures",
        matches: fx,
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: new Set(fx.map((m) => m.league)).size,
        note:
          "Schedules from football-data only (no % / xG shown). Set ODDS_API_KEY to enable Poisson, xG, edges, and value picks from real prices.",
        stale: false,
      };
      cachedResponse = payload;
      cachedAt = Date.now();
      return { statusCode: 200, headers, body: JSON.stringify(payload) };
    }
    const af = await getApiFootballFixtures();
    if (af.length) {
      const payload = {
        dataMode: "fixtures",
        matches: af,
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: new Set(af.map((m) => m.league)).size,
        note:
          "Schedules from API-Football only (no % / xG / picks — not computed without market odds). Set ODDS_API_KEY with quota so The Odds API can supply prices; then Poisson, xG, and edges are calculated from those prices.",
        stale: false,
      };
      cachedResponse = payload;
      cachedAt = Date.now();
      return { statusCode: 200, headers, body: JSON.stringify(payload) };
    }
    const empty = buildEmptyMatches(
      "No fixture data: set FOOTBALL_DATA_KEY (football-data.org) and/or a valid API_FOOTBALL_KEY, or ODDS_API_KEY for priced events."
    );
    cachedResponse = empty;
    cachedAt = Date.now();
    return { statusCode: 200, headers, body: JSON.stringify(empty) };
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
    console.warn(JSON.stringify({ level: "warn", event: "matches_empty_provider", fixtureFallback: true }));
    // If quota is exhausted/provider fails, fallback to real fixtures from football-data.
    const rows = await getAllScoreboardRows().catch(() => []);
    if (rows.length) {
      const fx = fixtureRowsToMatches(rows);
      const payload = {
        dataMode: "fixtures",
        matches: fx,
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: new Set(fx.map((m) => m.league)).size,
        note:
          "The Odds API returned no priced events. Showing schedules from football-data only. xG, probabilities, and value picks are hidden until the odds API returns 1X2 and totals. Check API key, quota, and that soccer markets are offered.",
        stale: true,
      };
      cachedResponse = payload;
      cachedAt = Date.now();
      return { statusCode: 200, headers, body: JSON.stringify(payload) };
    }
    const af = await getApiFootballFixtures();
    if (af.length) {
      const payload = {
        dataMode: "fixtures",
        matches: af,
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: new Set(af.map((m) => m.league)).size,
        note:
          "The Odds API returned no priced events. Fixture list from API-Football. Poisson, xG, and picks require real book prices — we do not show placeholder probabilities.",
        stale: true,
      };
      cachedResponse = payload;
      cachedAt = Date.now();
      return { statusCode: 200, headers, body: JSON.stringify(payload) };
    }
    const empty = buildEmptyMatches(
      "No matches from odds API and no fixture fallbacks (check ODDS_API_KEY quota, FOOTBALL_DATA_KEY, and API_FOOTBALL_KEY)."
    );
    cachedResponse = empty;
    cachedAt = Date.now();
    return { statusCode: 200, headers, body: JSON.stringify(empty) };
  }

  const payload = {
    dataMode: "odds",
    matches: allMatches,
    updated: new Date().toISOString(),
    leagues: TOP_LEAGUES,
    totalLeagues: new Set(allMatches.map(m => m.league)).size,
    stale: false,
  };
  cachedResponse = payload;
  cachedAt = Date.now();
  console.log(JSON.stringify({ level: "info", event: "matches_ready", total: payload.matches.length, leagues: payload.totalLeagues }));
  return {
    statusCode: 200,
    headers,
    body: JSON.stringify(payload),
  };
};
