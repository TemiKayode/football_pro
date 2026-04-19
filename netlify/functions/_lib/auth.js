const buckets = new Map();

function jsonHeaders(extra = {}) {
  return {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    ...extra,
  };
}

function getClientIp(event) {
  const h = event?.headers || {};
  return (
    h["x-nf-client-connection-ip"] ||
    h["x-forwarded-for"] ||
    h["client-ip"] ||
    "unknown"
  );
}

function rateLimit(event, keyPrefix = "api", limit = 120, windowMs = 60000) {
  const ip = getClientIp(event);
  const key = `${keyPrefix}:${ip}`;
  const now = Date.now();
  const slot = buckets.get(key) || { count: 0, resetAt: now + windowMs };
  if (now > slot.resetAt) {
    slot.count = 0;
    slot.resetAt = now + windowMs;
  }
  slot.count += 1;
  buckets.set(key, slot);
  return {
    allowed: slot.count <= limit,
    remaining: Math.max(0, limit - slot.count),
    resetAt: slot.resetAt,
  };
}

async function verifyNetlifyToken(token) {
  const base =
    process.env.URL ||
    process.env.DEPLOY_URL ||
    (process.env.DEPLOY_PRIME_URL
      ? `https://${process.env.DEPLOY_PRIME_URL}`
      : "");
  if (!base) return { ok: false, reason: "missing_base_url" };
  try {
    const r = await fetch(`${base}/.netlify/identity/user`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(8000),
    });
    if (!r.ok) return { ok: false, reason: `identity_${r.status}` };
    const user = await r.json();
    return { ok: true, user };
  } catch (e) {
    return { ok: false, reason: e.message || "identity_request_failed" };
  }
}

async function requireAuth(event) {
  // Auth is opt-in for now: set REQUIRE_AUTH=true in Netlify env to enforce login.
  if ((process.env.REQUIRE_AUTH || "false").toLowerCase() !== "true") {
    return { ok: true, user: null };
  }
  const auth = event?.headers?.authorization || event?.headers?.Authorization || "";
  if (!auth.startsWith("Bearer ")) {
    return {
      ok: false,
      response: {
        statusCode: 401,
        headers: jsonHeaders(),
        body: JSON.stringify({ ok: false, error: "Authentication required." }),
      },
    };
  }
  const token = auth.slice("Bearer ".length).trim();
  const res = await verifyNetlifyToken(token);
  if (!res.ok) {
    return {
      ok: false,
      response: {
        statusCode: 401,
        headers: jsonHeaders(),
        body: JSON.stringify({ ok: false, error: "Invalid or expired session." }),
      },
    };
  }
  return { ok: true, user: res.user };
}

module.exports = { jsonHeaders, rateLimit, requireAuth };
