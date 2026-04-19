// Dashboard /api/live — same shape as Flask (score_h, score_a, league, …).
// Uses football-data.org when FOOTBALL_DATA_KEY is set; otherwise API-Football (API_FOOTBALL_KEY) live=all.

const FD_KEY = process.env.FOOTBALL_DATA_KEY || "";
const API_FOOTBALL_KEY = process.env.API_FOOTBALL_KEY || "";
const { getAllScoreboardRows } = require("./scores");
const { jsonHeaders, rateLimit, requireAuth } = require("./_lib/auth");

const LIVE_STATUSES = new Set(["IN_PLAY", "PAUSED", "LIVE"]);

async function fetchApiFootballLive() {
  if (!API_FOOTBALL_KEY) return [];
  try {
    const resp = await fetch("https://v3.football.api-sports.io/fixtures?live=all", {
      headers: { "x-apisports-key": API_FOOTBALL_KEY },
      signal: AbortSignal.timeout(12000),
    });
    if (!resp.ok) return [];
    const data = await resp.json();
    if (data?.errors && Object.keys(data.errors).length) return [];
    if (!Array.isArray(data?.response)) return [];
    return data.response.map((item) => {
      const f = item?.fixture || {};
      const teams = item?.teams || {};
      const league = item?.league || {};
      const goals = item?.goals || {};
      return {
        home: teams?.home?.name || "Home",
        away: teams?.away?.name || "Away",
        league: league?.name || "",
        score_h: goals?.home != null ? goals.home : 0,
        score_a: goals?.away != null ? goals.away : 0,
        minute: f?.status?.elapsed ?? null,
        status: f?.status?.short || "",
      };
    });
  } catch (e) {
    console.error("live api-football:", e.message);
    return [];
  }
}

exports.handler = async (event) => {
  const headers = jsonHeaders({ "Cache-Control": "no-store" });

  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 204, headers, body: "" };
  }
  const publicMode = event?.queryStringParameters?.public === "1";
  const rl = rateLimit(event, "live", publicMode ? 240 : 120, 60000);
  if (!rl.allowed) return { statusCode: 429, headers, body: JSON.stringify({ ok: false, error: "Rate limit exceeded." }) };
  if (!publicMode) {
    const auth = await requireAuth(event);
    if (!auth.ok) return auth.response;
  }

  let live = [];
  let source = "none";

  if (FD_KEY) {
    let all = [];
    try {
      all = await getAllScoreboardRows();
    } catch (e) {
      console.error("live:", e.message);
    }
    live = all
      .filter((m) => LIVE_STATUSES.has(m.status))
      .map((m) => ({
        home: m.home,
        away: m.away,
        league: m.compName,
        score_h: m.homeScore != null ? m.homeScore : 0,
        score_a: m.awayScore != null ? m.awayScore : 0,
        minute: m.minute,
        status: m.status,
      }));
    if (live.length) source = "football-data";
  }

  if (live.length === 0 && API_FOOTBALL_KEY) {
    live = await fetchApiFootballLive();
    if (live.length) source = "api-football";
  }

  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({ live, count: live.length, source }),
  };
};
