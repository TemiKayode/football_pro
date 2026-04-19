// Lightweight status for the dashboard (credits + key flags). No secrets returned.

const ODDS_API_KEY = process.env.ODDS_API_KEY;
const { jsonHeaders, requireAuth, rateLimit } = require("./_lib/auth");

exports.handler = async (event) => {
  const headers = jsonHeaders({ "Cache-Control": "no-store" });
  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers, body: "" };
  const rl = rateLimit(event, "status", 60, 60000);
  if (!rl.allowed) return { statusCode: 429, headers, body: JSON.stringify({ ok: false, error: "Rate limit exceeded." }) };
  const auth = await requireAuth(event);
  if (!auth.ok) return auth.response;

  let credits = null;
  if (ODDS_API_KEY) {
    try {
      const r = await fetch(
        `https://api.the-odds-api.com/v4/sports?apiKey=${ODDS_API_KEY}`,
        { signal: AbortSignal.timeout(10000) }
      );
      const rem = r.headers.get("x-requests-remaining");
      if (rem != null && rem !== "") credits = Number(rem);
    } catch (e) {
      console.warn("status credits:", e.message);
    }
  }

  const football =
    !!(process.env.FOOTBALL_DATA_KEY || process.env.API_FOOTBALL_KEY);

  const body = {
    credits: credits != null && !Number.isNaN(credits) ? credits : null,
    api_keys: { odds: !!ODDS_API_KEY, football },
    config: {
      dry_run: true,
      bankroll: parseFloat(process.env.BANKROLL || "1000"),
      min_edge_pct: parseFloat(process.env.MIN_EDGE || "0.03") * 100,
      min_conf: parseFloat(process.env.MIN_CONFIDENCE || "52"),
      kelly: parseFloat(process.env.KELLY_FRACTION || "0.25"),
      folds: (process.env.ACCA_FOLDS || "3,5,7").split(",").map((s) => s.trim()),
    },
  };

  return { statusCode: 200, headers, body: JSON.stringify(body) };
};
