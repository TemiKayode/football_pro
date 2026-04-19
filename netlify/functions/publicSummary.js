const { handler: matchesHandler } = require("./matches");
const { handler: picksHandler } = require("./picks");

exports.handler = async () => {
  const m = await matchesHandler({ httpMethod: "GET", headers: {}, queryStringParameters: { public: "1" } });
  const p = await picksHandler({ httpMethod: "GET", headers: {}, queryStringParameters: { public: "1" } });
  const mBody = JSON.parse(m.body || "{}");
  const pBody = JSON.parse(p.body || "{}");
  const body = {
    ok: true,
    updated: mBody.updated || pBody.updated || new Date().toISOString(),
    dataMode: mBody.dataMode || "unknown",
    totals: {
      matches: Array.isArray(mBody.matches) ? mBody.matches.length : 0,
      picks: Array.isArray(pBody.picks) ? pBody.picks.length : 0,
      leagues: mBody.totalLeagues || 0,
    },
    note: mBody.note || "",
  };
  return {
    statusCode: 200,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": "public, max-age=60",
    },
    body: JSON.stringify(body),
  };
};
