exports.handler = async (event) => {
  const headers = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
  };
  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers, body: "" };
  return {
    statusCode: 200,
    headers,
    body: JSON.stringify({
      ok: false,
      error: "CLV updates run only in the local app (file-backed bets.json).",
    }),
  };
};
