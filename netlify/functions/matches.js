// netlify/functions/matches.js
// Fetches live match data from The Odds API for top 10 leagues
// Called by the frontend every 60 seconds for real-time updates

const ODDS_API_KEY = process.env.ODDS_API_KEY;
const API_FOOTBALL_KEY = process.env.API_FOOTBALL_KEY || "";
const SPORTMONKS_KEY = process.env.SPORTMONKS_KEY || "";
const ODDSPAPI_KEY  = process.env.ODDSPAPI_KEY  || "";
const BASE    = "https://api.the-odds-api.com/v4";
const SM_BASE = "https://api.sportmonks.com/v3/football";
const OP_BASE = "https://api.oddspapi.io/v4";
// AF_ODDS_ENABLED: opt-in only — adds 12 API-Football calls/refresh which burns free-tier (100/day) fast.
// Set AF_ODDS_ENABLED=true in Netlify env only if you have a paid AF plan.
const AF_ODDS_ENABLED = process.env.AF_ODDS_ENABLED === "true";
const { jsonHeaders, rateLimit, requireAuth } = require("./_lib/auth");
const { getAllScoreboardRows } = require("./scores");
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

function factorial(n) {
  return n <= 1 ? 1 : n * factorial(n - 1);
}

const RHO = -0.13; // Dixon-Coles low-score correlation

function poissonPmf(k, lam) {
  if (lam <= 0) return k === 0 ? 1 : 0;
  return Math.exp(-lam + k * Math.log(lam) - Math.log(factorial(k)));
}

function dcFactor(lh, la, i, j) {
  if (i === 0 && j === 0) return 1 - lh * la * RHO;
  if (i === 1 && j === 0) return 1 + la * RHO;
  if (i === 0 && j === 1) return 1 + lh * RHO;
  if (i === 1 && j === 1) return 1 - RHO;
  return 1;
}

function poissonProbs(lh, la, maxG = 8) {
  let pH = 0, pD = 0, pA = 0;
  for (let i = 0; i <= maxG; i++) {
    const piH = poissonPmf(i, lh);
    for (let j = 0; j <= maxG; j++) {
      const dc = i <= 1 && j <= 1 ? dcFactor(lh, la, i, j) : 1;
      const p = piH * poissonPmf(j, la) * dc;
      if (i > j) pH += p;
      else if (i === j) pD += p;
      else pA += p;
    }
  }
  return { pH, pD, pA };
}

// Invert 1X2 implied probs to (xgH, xgA) via DC-corrected Poisson + coordinate descent
function estimateXG(pH, pD, pA) {
  const tH = pH / 100, tD = pD / 100;
  const objective = (lh, la) => {
    const r = poissonProbs(lh, la);
    return (r.pH - tH) ** 2 + (r.pD - tD) ** 2;
  };
  let lh = 1.4, la = 1.1, step = 0.1;
  let best = objective(lh, la);
  for (let iter = 0; iter < 5; iter++) {
    let improved = true;
    while (improved) {
      improved = false;
      for (const [dlh, dla] of [[step,0],[-step,0],[0,step],[0,-step]]) {
        const nlh = Math.max(0.3, Math.min(5.5, lh + dlh));
        const nla = Math.max(0.2, Math.min(4.5, la + dla));
        const e = objective(nlh, nla);
        if (e < best) { best = e; lh = nlh; la = nla; improved = true; }
      }
    }
    step /= 2;
  }
  return { xgH: Math.round(lh * 100) / 100, xgA: Math.round(la * 100) / 100 };
}

// DC-corrected Over 2.5 probability
function over25Prob(xgH, xgA) {
  let under = 0;
  for (let i = 0; i <= 2; i++) {
    for (let j = 0; j <= 2 - i; j++) {
      const dc = i <= 1 && j <= 1 ? dcFactor(xgH, xgA, i, j) : 1;
      const ph = Math.exp(-xgH) * Math.pow(xgH, i) / factorial(i);
      const pa = Math.exp(-xgA) * Math.pow(xgA, j) / factorial(j);
      under += ph * pa * dc;
    }
  }
  return Math.round((1 - under) * 1000) / 10;
}

// Kelly criterion
function kelly(prob, odds, fraction = 0.25) {
  const b = odds - 1;
  const q = 1 - prob;
  const k = (b * prob - q) / b;
  return k > 0 ? Math.round(k * fraction * 100) / 100 : 0;
}

// API-Football league IDs matching our TOP_LEAGUES
const AF_TOP_LEAGUE_IDS = [2, 3, 39, 61, 71, 78, 88, 94, 128, 135, 140, 848];

let afOddsCache = null;
let afOddsCachedAt = 0;

// Fetch pre-match odds from API-Football for today's top-league fixtures.
// Uses a 60-minute cache to stay within free-tier quota (100 req/day).
async function fetchAfOddsMap(date) {
  if (!API_FOOTBALL_KEY) return {};
  const nowMs = Date.now();
  if (afOddsCache && nowMs - afOddsCachedAt < 3600000) return afOddsCache;
  const yr = new Date().getMonth() < 7 ? new Date().getFullYear() - 1 : new Date().getFullYear();
  const opts = { headers: { "x-apisports-key": API_FOOTBALL_KEY }, signal: AbortSignal.timeout(12000) };
  const urls = AF_TOP_LEAGUE_IDS.map(
    lid => `https://v3.football.api-sports.io/odds?league=${lid}&season=${yr}&date=${date}&bookmaker=8`
  );
  const results = await Promise.allSettled(urls.map(u => fetch(u, opts).then(r => r.ok ? r.json() : null).catch(() => null)));
  const oddsMap = {};
  for (const r of results) {
    if (r.status !== "fulfilled" || !r.value?.response) continue;
    for (const item of r.value.response) {
      const fid = item.fixture?.id;
      if (!fid) continue;
      for (const bk of (item.bookmakers || [])) {
        const mw = bk.bets?.find(b => b.id === 1);
        if (!mw) continue;
        const h = parseFloat(mw.values?.find(v => v.value === "Home")?.odd);
        const d = parseFloat(mw.values?.find(v => v.value === "Draw")?.odd);
        const a = parseFloat(mw.values?.find(v => v.value === "Away")?.odd);
        if (h > 1 && d > 1 && a > 1) { oddsMap[`af_${fid}`] = { home: h, draw: d, away: a }; break; }
      }
    }
  }
  afOddsCache = oddsMap;
  afOddsCachedAt = nowMs;
  return oddsMap;
}

// Merge AF bookmaker odds into fixture-only match objects, computing xG/probs
function applyAfOdds(fixtures, oddsMap) {
  return fixtures.map(m => {
    const o = oddsMap[m.id];
    if (!o) return m;
    const probs = removVig(o.home, o.draw, o.away);
    if (!probs) return m;
    const { xgH, xgA } = estimateXG(probs.H, probs.D, probs.A);
    const o25 = over25Prob(xgH, xgA);
    return {
      ...m,
      odds: { home: o.home, draw: o.draw, away: o.away, over25: 0, under25: 0 },
      bks: { home: "Bet365", draw: "Bet365", away: "Bet365", over: "", under: "" },
      probs: { H: probs.H, D: probs.D, A: probs.A, O25: o25, U25: 100 - o25 },
      xg: { home: xgH, away: xgA },
      modelReady: true,
    };
  });
}

// ── OddsPapi integration ──────────────────────────────────────────────────────
// 2 calls per refresh: /fixtures (today+tomorrow) → /odds-by-tournaments (bulk)
// Market 101 = Full Time Result (1X2), outcomes: 101=Home, 102=Draw, 103=Away

function extractOpOdds(bookmakerOdds) {
  let bestH = 0, bestD = 0, bestA = 0;
  for (const bk of Object.values(bookmakerOdds || {})) {
    const mkt = (bk.markets || {})["101"] || Object.values(bk.markets || {})[0];
    if (!mkt?.outcomes) continue;
    const h = parseFloat(mkt.outcomes["101"]?.players?.["0"]?.price || 0);
    const d = parseFloat(mkt.outcomes["102"]?.players?.["0"]?.price || 0);
    const a = parseFloat(mkt.outcomes["103"]?.players?.["0"]?.price || 0);
    if (h > 1) bestH = Math.max(bestH, h);
    if (d > 1) bestD = Math.max(bestD, d);
    if (a > 1) bestA = Math.max(bestA, a);
  }
  return (bestH > 1 && bestD > 1 && bestA > 1) ? { home: bestH, draw: bestD, away: bestA } : null;
}

async function getOddsPapiMatches() {
  if (!ODDSPAPI_KEY) return [];
  const today    = new Date().toISOString().slice(0, 10);
  const tomorrow = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  const opts = { signal: AbortSignal.timeout(12000) };
  try {
    const fxResp = await fetch(
      `${OP_BASE}/fixtures?sportId=10&from=${today}&to=${tomorrow}&hasOdds=true&apiKey=${ODDSPAPI_KEY}`,
      opts
    );
    if (!fxResp.ok) return [];
    const fxRaw = await fxResp.json();
    const fixtures = Array.isArray(fxRaw) ? fxRaw : (fxRaw.data || fxRaw.fixtures || []);
    if (!fixtures.length) return [];

    // Bulk-fetch odds for all tournaments returned
    const tids = [...new Set(fixtures.map(f => f.tournamentId).filter(Boolean))].join(",");
    const oddsResp = await fetch(
      `${OP_BASE}/odds-by-tournaments?tournamentIds=${tids}&bookmakers=bet365,pinnacle,1xbet,betway&apiKey=${ODDSPAPI_KEY}`,
      opts
    );
    const oddsRaw = oddsResp.ok ? await oddsResp.json() : {};
    const oddsFixtures = Array.isArray(oddsRaw) ? oddsRaw : (oddsRaw.fixtures || oddsRaw.data || []);

    const oddsMap = {};
    for (const item of oddsFixtures) {
      if (!item.fixtureId) continue;
      const o = extractOpOdds(item.bookmakerOdds);
      if (o) oddsMap[item.fixtureId] = o;
    }

    const now = Date.now();
    const matches = fixtures.map(f => {
      const kickoff = f.startTime || f.start_time || new Date().toISOString();
      const minsToKO = Math.round((new Date(kickoff).getTime() - now) / 60000);
      const homeName = f.participant1Name || f.participant1ShortName || "Home";
      const awayName = f.participant2Name || f.participant2ShortName || "Away";
      const o = oddsMap[f.fixtureId];
      const probs = o ? removVig(o.home, o.draw, o.away) : null;
      let xgData = null, probsData = null;
      if (probs) {
        const { xgH, xgA } = estimateXG(probs.H, probs.D, probs.A);
        const o25 = over25Prob(xgH, xgA);
        xgData = { home: xgH, away: xgA };
        probsData = { H: probs.H, D: probs.D, A: probs.A, O25: o25, U25: 100 - o25 };
      }
      return {
        id: `op_${f.fixtureId}`,
        home: homeName,
        away: awayName,
        league: String(f.tournamentId || "op"),
        leagueName: f.tournamentName || "Football",
        leagueFlag: "⚽",
        country: f.categoryName || "",
        kickoff,
        minsToKO,
        isLive: f.statusId === 1,
        isToday:    new Date(kickoff).toDateString() === new Date().toDateString(),
        isTomorrow: new Date(kickoff).toDateString() === new Date(Date.now() + 86400000).toDateString(),
        odds: o
          ? { home: o.home, draw: o.draw, away: o.away, over25: 0, under25: 0 }
          : { home: 0, draw: 0, away: 0, over25: 0, under25: 0 },
        bks: { home: "OddsPapi", draw: "OddsPapi", away: "OddsPapi", over: "", under: "" },
        probs: probsData,
        xg: xgData,
        modelReady: probs != null,
        source: "oddspapi",
      };
    }).filter(m => m.home !== "Home" || m.away !== "Away");

    matches.sort((a, b) => {
      if (a.isLive && !b.isLive) return -1;
      if (!a.isLive && b.isLive) return 1;
      return new Date(a.kickoff) - new Date(b.kickoff);
    });
    console.log(JSON.stringify({ level: "info", event: "oddspapi_ready", total: matches.length, priced: matches.filter(m => m.modelReady).length }));
    return matches;
  } catch (e) {
    console.error("OddsPapi failed:", e.message);
    return [];
  }
}

// ── Sportmonks integration ────────────────────────────────────────────────────

function mapSmToMatch(item) {
  const home = (item.participants || []).find(p => p.meta?.location === "home");
  const away = (item.participants || []).find(p => p.meta?.location === "away");
  if (!home || !away) return null;

  const kickoff = item.starting_at_timestamp
    ? new Date(item.starting_at_timestamp * 1000).toISOString()
    : (item.starting_at || new Date().toISOString());
  const minsToKO = Math.round((new Date(kickoff).getTime() - Date.now()) / 60000);

  // SM state IDs: 1=NS, 2=1H, 3=HT, 4=2H, 5=ET, 6=PEN, 7=FT, 10=CANC
  const stateId = item.state_id;
  const isLive = [2, 3, 4, 5, 6].includes(stateId);

  const league = item.league || {};

  // Extract best 1X2 odds from the flat odds array Sportmonks returns
  let bestH = 0, bestD = 0, bestA = 0;
  for (const o of (item.odds || [])) {
    const desc = String(o.market_description || o.name || "").toLowerCase();
    if (!desc.includes("3way") && !desc.includes("match winner") && !desc.includes("result") && !desc.includes("1x2") && !desc.includes("full time")) continue;
    const label = String(o.label || o.name || "").toLowerCase();
    const val = parseFloat(o.value || o.odd || "0");
    if (val < 1.02) continue;
    if (label === "home" || label === "1")        bestH = Math.max(bestH, val);
    else if (label === "draw" || label === "x")   bestD = Math.max(bestD, val);
    else if (label === "away" || label === "2")   bestA = Math.max(bestA, val);
  }

  const probs = (bestH > 1 && bestD > 1 && bestA > 1) ? removVig(bestH, bestD, bestA) : null;
  let xgData = null, probsData = null;
  if (probs) {
    const { xgH, xgA } = estimateXG(probs.H, probs.D, probs.A);
    const o25 = over25Prob(xgH, xgA);
    xgData = { home: xgH, away: xgA };
    probsData = { H: probs.H, D: probs.D, A: probs.A, O25: o25, U25: 100 - o25 };
  }

  return {
    id: `sm_${item.id}`,
    home: home.name,
    away: away.name,
    league: String(league.id || "sm"),
    leagueName: league.name || "Football",
    leagueFlag: "⚽",
    country: "",
    kickoff,
    minsToKO,
    isLive,
    isToday: new Date(kickoff).toDateString() === new Date().toDateString(),
    isTomorrow: new Date(kickoff).toDateString() === new Date(Date.now() + 86400000).toDateString(),
    odds: probs
      ? { home: bestH, draw: bestD, away: bestA, over25: 0, under25: 0 }
      : { home: 0, draw: 0, away: 0, over25: 0, under25: 0 },
    bks: { home: "SM", draw: "SM", away: "SM", over: "", under: "" },
    probs: probsData,
    xg: xgData,
    modelReady: probs != null,
    source: "sportmonks",
  };
}

async function getSportmonksFixtures() {
  if (!SPORTMONKS_KEY) return [];
  const today = new Date().toISOString().slice(0, 10);
  const tomorrow = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  const inc = "participants;league;state;odds";
  const opts = { signal: AbortSignal.timeout(15000) };
  try {
    const [r1, r2, rLive] = await Promise.all([
      fetch(`${SM_BASE}/fixtures/date/${today}?api_token=${SPORTMONKS_KEY}&include=${inc}`, opts),
      fetch(`${SM_BASE}/fixtures/date/${tomorrow}?api_token=${SPORTMONKS_KEY}&include=${inc}`, opts),
      fetch(`${SM_BASE}/livescores/latest?api_token=${SPORTMONKS_KEY}&include=${inc}`, opts),
    ]);
    const [d1, d2, dLive] = await Promise.all([
      r1.ok ? r1.json() : { data: [] },
      r2.ok ? r2.json() : { data: [] },
      rLive.ok ? rLive.json() : { data: [] },
    ]);
    const byId = new Map();
    for (const item of [...(d1.data || []), ...(d2.data || []), ...(dLive.data || [])]) {
      if (!item?.id) continue;
      const mapped = mapSmToMatch(item);
      if (!mapped) continue;
      // Live takes priority
      if (!byId.has(item.id) || mapped.isLive) byId.set(item.id, mapped);
    }
    const all = [...byId.values()];
    all.sort((a, b) => {
      if (a.isLive && !b.isLive) return -1;
      if (!a.isLive && b.isLive) return 1;
      return new Date(a.kickoff) - new Date(b.kickoff);
    });
    console.log(JSON.stringify({ level: "info", event: "sm_fixtures", total: all.length, priced: all.filter(m => m.modelReady).length }));
    return all;
  } catch (e) {
    console.error("Sportmonks fetch failed:", e.message);
    return [];
  }
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

      const { xgH, xgA } = estimateXG(probs.H, probs.D, probs.A);
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
      league: (m.competition || "football").toLowerCase(),
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
    league: String(league?.id || "api-football"),
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
  const days = Math.min(14, Math.max(1, Number.parseInt(process.env.FIXTURES_DAYS || "2", 10)));
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
  if (cachedResponse && nowMs - cachedAt < 300000) {
    console.log(JSON.stringify({ level: "info", event: "matches_cache_hit", ageMs: nowMs - cachedAt }));
    return { statusCode: 200, headers, body: JSON.stringify(cachedResponse) };
  }

  if (!ODDS_API_KEY) {
    // OddsPapi: 2 calls (fixtures + bulk odds), global coverage, 200+ bookmakers
    const op = await getOddsPapiMatches();
    if (op.length) {
      const priced = op.filter(m => m.modelReady).length;
      const payload = {
        dataMode: priced > 0 ? "odds" : "fixtures",
        matches: op,
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: new Set(op.map(m => m.league)).size,
        note: priced > 0
          ? `${priced} of ${op.length} matches priced via OddsPapi (${priced > 0 ? "bet365/Pinnacle/1xBet" : "no odds"}).`
          : "Fixtures from OddsPapi — no 1X2 odds returned for current fixtures.",
        stale: false,
      };
      cachedResponse = payload;
      cachedAt = Date.now();
      return { statusCode: 200, headers, body: JSON.stringify(payload) };
    }
    // Sportmonks: fixtures + pre-match odds in one request, ~200 req/hour free
    const sm = await getSportmonksFixtures();
    if (sm.length) {
      const priced = sm.filter(m => m.modelReady).length;
      const payload = {
        dataMode: priced > 0 ? "odds" : "fixtures",
        matches: sm,
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: new Set(sm.map(m => m.league)).size,
        note: priced > 0
          ? `${priced} of ${sm.length} matches priced via Sportmonks. Set ODDS_API_KEY for full cross-bookmaker coverage.`
          : "Fixtures from Sportmonks (no odds returned on current plan). Set ODDS_API_KEY for predictions.",
        stale: false,
      };
      cachedResponse = payload;
      cachedAt = Date.now();
      return { statusCode: 200, headers, body: JSON.stringify(payload) };
    }
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
      const today = new Date().toISOString().slice(0, 10);
      const oddsMap = AF_ODDS_ENABLED ? await fetchAfOddsMap(today).catch(() => ({})) : {};
      const enriched = AF_ODDS_ENABLED ? applyAfOdds(af, oddsMap) : af;
      const priced = enriched.filter(m => m.modelReady).length;
      const payload = {
        dataMode: priced > 0 ? "odds" : "fixtures",
        matches: enriched,
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: new Set(enriched.map((m) => m.league)).size,
        note: priced > 0
          ? `${priced} matches priced via API-Football odds. Set ODDS_API_KEY for full coverage across all leagues.`
          : "Schedules from API-Football only. Set ODDS_API_KEY for odds, xG, and value picks.",
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
    // OddsPapi as first fallback when Odds API quota is exhausted
    const op = await getOddsPapiMatches();
    if (op.length) {
      const priced = op.filter(m => m.modelReady).length;
      const payload = {
        dataMode: priced > 0 ? "odds" : "fixtures",
        matches: op,
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: new Set(op.map(m => m.league)).size,
        note: priced > 0
          ? `Odds API quota exhausted. ${priced} matches priced via OddsPapi. Renew ODDS_API_KEY for broader coverage.`
          : "Odds API quota exhausted. OddsPapi fixtures only — odds not available for today's matches.",
        stale: true,
      };
      cachedResponse = payload;
      cachedAt = Date.now();
      return { statusCode: 200, headers, body: JSON.stringify(payload) };
    }
    // Sportmonks as second fallback when Odds API quota is exhausted
    const sm = await getSportmonksFixtures();
    if (sm.length) {
      const priced = sm.filter(m => m.modelReady).length;
      const payload = {
        dataMode: priced > 0 ? "odds" : "fixtures",
        matches: sm,
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: new Set(sm.map(m => m.league)).size,
        note: priced > 0
          ? `Odds API quota exhausted. ${priced} matches priced via Sportmonks odds. Renew ODDS_API_KEY for full coverage.`
          : "Odds API quota exhausted. Fixtures from Sportmonks — odds not on current plan.",
        stale: true,
      };
      cachedResponse = payload;
      cachedAt = Date.now();
      return { statusCode: 200, headers, body: JSON.stringify(payload) };
    }
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
      const today = new Date().toISOString().slice(0, 10);
      const oddsMap = AF_ODDS_ENABLED ? await fetchAfOddsMap(today).catch(() => ({})) : {};
      const enriched = AF_ODDS_ENABLED ? applyAfOdds(af, oddsMap) : af;
      const priced = enriched.filter(m => m.modelReady).length;
      const payload = {
        dataMode: priced > 0 ? "odds" : "fixtures",
        matches: enriched,
        updated: new Date().toISOString(),
        leagues: TOP_LEAGUES,
        totalLeagues: new Set(enriched.map((m) => m.league)).size,
        note: priced > 0
          ? `The Odds API quota is exhausted. ${priced} matches priced via API-Football odds (Bet365). Renew ODDS_API_KEY for full league coverage.`
          : "The Odds API quota is exhausted. Showing fixtures only — set ODDS_API_KEY or AF_ODDS_ENABLED=true for predictions.",
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
