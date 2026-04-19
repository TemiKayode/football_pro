// netlify/functions/scores.js
// Fetches live and upcoming scores from football-data.org

const FD_KEY = process.env.FOOTBALL_DATA_KEY || "";
const BASE = "https://api.football-data.org/v4";
const { jsonHeaders, rateLimit, requireAuth } = require("./_lib/auth");

const COMPETITIONS = [
  { id: "PL", name: "Premier League", country: "England", flag: "EPL" },
  { id: "PD", name: "La Liga", country: "Spain", flag: "LALIGA" },
  { id: "BL1", name: "Bundesliga", country: "Germany", flag: "BL" },
  { id: "SA", name: "Serie A", country: "Italy", flag: "SA" },
  { id: "FL1", name: "Ligue 1", country: "France", flag: "L1" },
  { id: "CL", name: "Champions League", country: "Europe", flag: "UCL" },
  { id: "EL", name: "Europa League", country: "Europe", flag: "UEL" },
  { id: "UEL", name: "Europa League", country: "Europe", flag: "UEL" },
  { id: "UECL", name: "Conference League", country: "Europe", flag: "UECL" },
  { id: "ECL", name: "Conference League", country: "Europe", flag: "UECL" },
  { id: "DED", name: "Eredivisie", country: "Netherlands", flag: "ERE" },
  { id: "PPL", name: "Primeira Liga", country: "Portugal", flag: "POR" },
  { id: "BSA", name: "Brasileirao", country: "Brazil", flag: "BRA" },
  { id: "CLI", name: "Liga Profesional", country: "Argentina", flag: "ARG" },
];

async function fetchMatches(competitionId) {
  if (!FD_KEY) return [];
  const today = new Date().toISOString().split("T")[0];
  const days = Number.parseInt(process.env.FIXTURES_DAYS || "7", 10);
  const dateTo = new Date(Date.now() + Math.max(1, days) * 86400000).toISOString().split("T")[0];
  const url = `${BASE}/competitions/${competitionId}/matches?dateFrom=${today}&dateTo=${dateTo}`;
  try {
    const resp = await fetch(url, {
      headers: { "X-Auth-Token": FD_KEY },
      signal: AbortSignal.timeout(8000),
    });
    if (!resp.ok) return [];
    const data = await resp.json();
    const comp = COMPETITIONS.find((c) => c.id === competitionId);
    return (data.matches || []).map((m) => ({
      id: m.id,
      competition: competitionId,
      compName: comp?.name || competitionId,
      flag: comp?.flag || "MISC",
      home: m.homeTeam?.shortName || m.homeTeam?.name || "Home",
      away: m.awayTeam?.shortName || m.awayTeam?.name || "Away",
      status: m.status,
      minute: m.minute || null,
      homeScore: m.score?.fullTime?.home ?? m.score?.halfTime?.home ?? null,
      awayScore: m.score?.fullTime?.away ?? m.score?.halfTime?.away ?? null,
      kickoff: m.utcDate,
      matchday: m.matchday,
      stage: m.stage,
    }));
  } catch {
    return [];
  }
}

/** Sorted rows for scoreboard + live panel (shared with live.js). */
async function getAllScoreboardRows() {
  if (!FD_KEY) return [];
  const seen = new Set();
  const results = await Promise.allSettled(COMPETITIONS.map((c) => fetchMatches(c.id)));
  const all = results.flatMap((r) => (r.status === "fulfilled" ? r.value : []));
  const deduped = all.filter((m) => {
    const k = String(m.id);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  const order = { IN_PLAY: 0, PAUSED: 1, LIVE: 2, SCHEDULED: 3, TIMED: 4, FINISHED: 5 };
  deduped.sort(
    (a, b) =>
      (order[a.status] ?? 9) - (order[b.status] ?? 9) ||
      new Date(a.kickoff) - new Date(b.kickoff)
  );
  return deduped;
}

exports.getAllScoreboardRows = getAllScoreboardRows;

exports.handler = async (event) => {
  const headers = jsonHeaders({ "Cache-Control": "no-cache, max-age=0" });
  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers, body: "" };

  const publicMode = event?.queryStringParameters?.public === "1";
  const rl = rateLimit(event, "scores", publicMode ? 240 : 120, 60000);
  if (!rl.allowed) return { statusCode: 429, headers, body: JSON.stringify({ ok: false, error: "Rate limit exceeded." }) };
  if (!publicMode) {
    const auth = await requireAuth(event);
    if (!auth.ok) return auth.response;
  }

  if (!FD_KEY) {
    return {
      statusCode: 200,
      headers,
      body: JSON.stringify({
        matches: [],
        updated: new Date().toISOString(),
        message: "Set FOOTBALL_DATA_KEY for live scores and fixture fallbacks.",
      }),
    };
  }

  const all = await getAllScoreboardRows();
  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({ matches: all, updated: new Date().toISOString() }),
  };
};
