// Public site: ML training is intentionally local-only.

const MSG =
  "Model training runs only on your machine (Python + scikit-learn + data.csv). " +
  "Use the same repo: python app.py → Config → Train, or: python core/model.py data.csv";
const { jsonHeaders, requireAuth, rateLimit } = require("./_lib/auth");

exports.handler = async (event) => {
  const headers = jsonHeaders();
  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers, body: "" };
  const rl = rateLimit(event, "train", 20, 60000);
  if (!rl.allowed) return { statusCode: 429, headers, body: JSON.stringify({ ok: false, error: "Rate limit exceeded." }) };
  const auth = await requireAuth(event);
  if (!auth.ok) return auth.response;
  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({ ok: false, stdout: MSG, stderr: "" }),
  };
};
