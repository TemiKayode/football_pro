// Mirrors Flask /api/kelly for the public dashboard (no secrets).

exports.handler = async (event) => {
  const headers = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
  };
  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers, body: "" };

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
