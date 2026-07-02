import { getSupabase, validateConfig } from "./_supabase.js";

// GET /api/vacancies — the live dashboard payload (full mode).
//
// Returns the single dashboard_snapshot row's payload, same shape as the baked
// public/data.js VACANCY_DATA. The pipeline upserts that row on every data
// change, so a browser refresh shows current data with no redeploy.
//
// Conditional GET: the dashboard polls this endpoint (see bootstrap.js). The
// row's `updated_at` is bumped on every write (see
// sql/migrations/0001_dashboard_snapshot.postgres.sql), so it is already a
// stable, unique version token — no need to hash the ~3.6 MB payload. A poll
// that sends a still-current `If-None-Match` gets a 304 with no body, and the
// full payload is never even pulled out of Postgres (see the two-query split
// below: the cheap metadata query runs first).
//
// Security (this payload carries PII — personal scoring text):
//   * Same-origin only — NO `Access-Control-Allow-Origin: *`. The matching
//     middleware.js Basic Auth gate is the access control.
//   * Fail closed — if AUTH_USER/AUTH_PASS are not configured, middleware lets
//     every request through (opt-in auth), so this endpoint refuses to serve
//     rather than leak PII on an unprotected deployment.
//   * `Cache-Control: no-store` — a refresh must never get a stale payload.
//     (This is an app-level conditional GET, not HTTP cache reuse: no-store
//     just means "always ask the server", which the ETag round-trip still does.)

/** Build the ETag for a snapshot version from its `updated_at` timestamp. */
export function computeETag(updatedAt) {
  return updatedAt ? `"${updatedAt}"` : null;
}

/** True when the client's cached copy (If-None-Match) is still current. */
export function isNotModified(ifNoneMatch, etag) {
  return Boolean(ifNoneMatch) && Boolean(etag) && ifNoneMatch === etag;
}

export default async function handler(req, res) {
  // No CORS allow-origin header on purpose: same-origin only.
  res.setHeader("Cache-Control", "no-store");

  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "GET")
    return res.status(405).json({ error: "Method not allowed" });

  // Fail closed: without the Basic Auth gate configured, the dashboard is open,
  // so refuse to serve PII. Checked BEFORE the Supabase config so an unprotected
  // deployment gets 503 (not 500) and never a payload.
  if (!process.env.AUTH_USER || !process.env.AUTH_PASS) {
    console.error("vacancies: refusing to serve — AUTH_USER/AUTH_PASS not set");
    return res.status(503).json({ error: "Auth not configured" });
  }

  if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    console.error(
      "vacancies: missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY",
    );
    return res.status(500).json({ error: "Server misconfigured" });
  }

  const warnings = validateConfig();
  if (warnings.length)
    console.warn("vacancies: config warning —", warnings.join("; "));

  try {
    // Cheap first: fetch only the version column. A poller sends
    // If-None-Match on every request, so an unchanged poll stops right here
    // — no JSONB payload leaves Postgres, let alone the response.
    const { data: meta, error: metaError } = await getSupabase()
      .from("dashboard_snapshot")
      .select("updated_at")
      .eq("id", "current")
      .maybeSingle();

    if (metaError) throw metaError;
    if (!meta) {
      // Endpoint is live but the snapshot has not been generated yet. NOT a 404
      // — 404 is the front-end's "endpoint absent → load static data.js" signal,
      // which would be wrong here (full mode ships no data.js).
      return res.status(503).json({ error: "Snapshot not generated yet" });
    }

    const etag = computeETag(meta.updated_at);
    if (etag) res.setHeader("ETag", etag);
    if (isNotModified(req.headers["if-none-match"], etag)) {
      return res.status(304).end();
    }

    const { data, error } = await getSupabase()
      .from("dashboard_snapshot")
      .select("payload")
      .eq("id", "current")
      .maybeSingle();

    if (error) throw error;
    if (!data) {
      return res.status(503).json({ error: "Snapshot not generated yet" });
    }
    return res.status(200).json(data.payload);
  } catch (err) {
    console.error("vacancies: error", err.message);
    return res.status(500).json({ error: "Database error" });
  }
}
