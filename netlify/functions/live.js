// Dashboard /api/live — same shape as Flask (score_h, score_a, league, …).

const { getAllScoreboardRows } = require("./scores");

const LIVE_STATUSES = new Set(["IN_PLAY", "PAUSED", "LIVE"]);

exports.handler = async (event) => {
  const headers = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
  };

  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 204, headers, body: "" };
  }

  let all = [];
  try {
    all = await getAllScoreboardRows();
  } catch (e) {
    console.error("live:", e.message);
  }

  const live = all
    .filter(m => LIVE_STATUSES.has(m.status))
    .map(m => ({
      home: m.home,
      away: m.away,
      league: m.compName,
      score_h: m.homeScore != null ? m.homeScore : 0,
      score_a: m.awayScore != null ? m.awayScore : 0,
      minute: m.minute,
      status: m.status,
    }));

  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({ live, count: live.length }),
  };
};
