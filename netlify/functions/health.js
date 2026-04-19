const { jsonHeaders } = require("./_lib/auth");

exports.handler = async () => {
  const body = {
    ok: true,
    service: "football-pro-api",
    ts: new Date().toISOString(),
    env: process.env.CONTEXT || "unknown",
  };
  return { statusCode: 200, headers: jsonHeaders({ "Cache-Control": "no-store" }), body: JSON.stringify(body) };
};
