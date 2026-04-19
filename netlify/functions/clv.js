// CLV / bet ledger is file-backed in the Flask app; public site has no persistent store.

const emptySummary = {
  total: 0,
  settled: 0,
  wins: 0,
  total_staked: 0,
  profit: 0,
  roi: 0,
  avg_clv: 0,
  win_rate: 0,
};
const { jsonHeaders, rateLimit, requireAuth } = require("./_lib/auth");

exports.handler = async (event) => {
  const headers = jsonHeaders({ "Cache-Control": "no-store" });
  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers, body: "" };
  const rl = rateLimit(event, "clv", 60, 60000);
  if (!rl.allowed) return { statusCode: 429, headers, body: JSON.stringify({ ok: false, error: "Rate limit exceeded." }) };
  const auth = await requireAuth(event);
  if (!auth.ok) return auth.response;
  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({
      bets: [],
      summary: emptySummary,
      note: "Bet tracking (CLV) is available when you run python app.py locally.",
    }),
  };
};
