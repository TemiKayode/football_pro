const { jsonHeaders, requireAuth, rateLimit } = require("./_lib/auth");

exports.handler = async (event) => {
  const headers = jsonHeaders({ "Cache-Control": "no-store" });
  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers, body: "" };
  const rl = rateLimit(event, "authMe", 120, 60000);
  if (!rl.allowed) return { statusCode: 429, headers, body: JSON.stringify({ ok: false, error: "Rate limit exceeded." }) };
  const auth = await requireAuth(event);
  if (!auth.ok) return auth.response;
  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({
      ok: true,
      user: {
        id: auth.user.id,
        email: auth.user.email,
      },
    }),
  };
};
