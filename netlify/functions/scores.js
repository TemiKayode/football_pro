// netlify/functions/scores.js
// Fetches live and today's scores from football-data.org (free tier: 10 req/min)

const FD_KEY = process.env.FOOTBALL_DATA_KEY || "";
const BASE = "https://api.football-data.org/v4";

const COMPETITIONS = [
  { id: "PL",  name: "Premier League",   country: "England",    flag: "🏴󠁧󠁢󠁥󠁮󠁧󠁿" },
  { id: "PD",  name: "La Liga",          country: "Spain",      flag: "🇪🇸" },
  { id: "BL1", name: "Bundesliga",       country: "Germany",    flag: "🇩🇪" },
  { id: "SA",  name: "Serie A",          country: "Italy",      flag: "🇮🇹" },
  { id: "FL1", name: "Ligue 1",          country: "France",     flag: "🇫🇷" },
  { id: "CL",  name: "Champions League", country: "Europe",     flag: "🏆" },
  { id: "DED", name: "Eredivisie",       country: "Netherlands",flag: "🇳🇱" },
  { id: "PPL", name: "Primeira Liga",    country: "Portugal",   flag: "🇵🇹" },
  { id: "BSA", name: "Brasileirão",      country: "Brazil",     flag: "🇧🇷" },
  { id: "CLI", name: "Liga Profesional", country: "Argentina",  flag: "🇦🇷" },
];

async function fetchMatches(competitionId) {
  if (!FD_KEY) return [];
  const today = new Date().toISOString().split("T")[0];
  const tomorrow = new Date(Date.now() + 86400000).toISOString().split("T")[0];
  const url = `${BASE}/competitions/${competitionId}/matches?dateFrom=${today}&dateTo=${tomorrow}`;
  try {
    const resp = await fetch(url, {
      headers: { "X-Auth-Token": FD_KEY },
      signal: AbortSignal.timeout(6000),
    });
    if (!resp.ok) return [];
    const data = await resp.json();
    const comp = COMPETITIONS.find(c => c.id === competitionId);
    return (data.matches || []).map(m => ({
      id: m.id,
      competition: competitionId,
      compName: comp?.name || competitionId,
      flag: comp?.flag || "⚽",
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
  } catch (e) {
    return [];
  }
}

/** Sorted rows for scoreboard + live panel (shared with live.js). */
async function getAllScoreboardRows() {
  if (!FD_KEY) return [];
  const results = await Promise.allSettled(COMPETITIONS.map(c => fetchMatches(c.id)));
  const all = results.flatMap(r => r.status === "fulfilled" ? r.value : []);
  const order = { IN_PLAY: 0, PAUSED: 1, LIVE: 2, SCHEDULED: 3, TIMED: 4, FINISHED: 5 };
  all.sort(
    (a, b) =>
      (order[a.status] ?? 9) - (order[b.status] ?? 9) ||
      new Date(a.kickoff) - new Date(b.kickoff)
  );
  return all;
}

exports.getAllScoreboardRows = getAllScoreboardRows;

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

  if (!FD_KEY) {
    return {
      statusCode: 200,
      headers,
      body: JSON.stringify({ demo: true, matches: [], message: "Set FOOTBALL_DATA_KEY for live scores" }),
    };
  }

  const all = await getAllScoreboardRows();

  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({ demo: false, matches: all, updated: new Date().toISOString() }),
  };
};
