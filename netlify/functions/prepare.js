// Public site: data download is intentionally local-only (Python + disk + long runtime).

const MSG =
  "Prepare / download runs only in the local Football Pro app (Python backend). " +
  "Clone the repo, run: python -m pip install -r requirements.txt, then python app.py " +
  "and use ⬇ Download on http://localhost:5000 — or: python core/prepare_data.py --league E0 --seasons 2122 2223 2324 2425";
const { jsonHeaders, requireAuth, rateLimit } = require("./_lib/auth");

exports.handler = async (event) => {
  const headers = jsonHeaders();
  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers, body: "" };
  const rl = rateLimit(event, "prepare", 20, 60000);
  if (!rl.allowed) return { statusCode: 429, headers, body: JSON.stringify({ ok: false, error: "Rate limit exceeded." }) };
  const auth = await requireAuth(event);
  if (!auth.ok) return auth.response;
  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({ ok: false, stdout: MSG, stderr: "" }),
  };
};
