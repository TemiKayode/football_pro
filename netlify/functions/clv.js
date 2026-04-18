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

exports.handler = async (event) => {
  const headers = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
  };
  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers, body: "" };
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
