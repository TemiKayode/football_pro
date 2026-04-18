// Public site: ML training is intentionally local-only.

const MSG =
  "Model training runs only on your machine (Python + scikit-learn + data.csv). " +
  "Use the same repo: python app.py → Config → Train, or: python core/model.py data.csv";

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
    body: JSON.stringify({ ok: false, stdout: MSG, stderr: "" }),
  };
};
