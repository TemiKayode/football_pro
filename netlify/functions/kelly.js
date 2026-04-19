// Mirrors Flask /api/kelly.
const { jsonHeaders, rateLimit, requireAuth } = require("./_lib/auth");

exports.handler = async (event) => {
  const headers = jsonHeaders({ "Cache-Control": "no-store" });
  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers, body: "" };
  const rl = rateLimit(event, "kelly", 180, 60000);
  if (!rl.allowed) return { statusCode: 429, headers, body: JSON.stringify({ ok: false, error: "Rate limit exceeded." }) };
  const auth = await requireAuth(event);
  if (!auth.ok) return auth.response;

  const q = event.queryStringParameters || {};
  const prob = parseFloat(q.prob || "0.55");
  const odds = parseFloat(q.odds || "2");
  const bank = parseFloat(q.bankroll || "1000");
  const frac = parseFloat(q.fraction || "0.25");

  const b = odds - 1;
  const fk = b > 0 ? (b * prob - (1 - prob)) / b : 0;
  const stake = Math.round(bank * Math.max(0, fk) * frac * 100) / 100;
  const ev_pct = Math.round((prob * odds - 1) * 10000) / 100;
  const breakeven_pct = Math.round((1 / odds) * 10000) / 100;

  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({
      stake,
      ev_pct,
      full_kelly_pct: Math.round(fk * 10000) / 100,
      frac_kelly_pct: Math.round(fk * frac * 10000) / 100,
      breakeven_pct,
    }),
  };
};
