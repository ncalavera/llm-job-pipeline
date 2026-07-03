import { getSupabase, validateConfig } from "./_supabase.js";
import { authNotConfigured } from "./_handler.js";

export default async function handler(req, res) {
  if (req.method !== "GET") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  // Fail closed like the PII readers: without the Basic Auth gate configured the
  // dashboard is open, so this probe (which reaches the DB) must refuse rather
  // than expose backend reachability to the public internet.
  if (authNotConfigured(res, "health")) return;

  const warnings = validateConfig();
  if (warnings.length) {
    console.warn("health: config warnings —", warnings.join("; "));
  }

  let connected = false;
  if (process.env.SUPABASE_URL && process.env.SUPABASE_SERVICE_ROLE_KEY) {
    try {
      const { error } = await getSupabase()
        .from("vacancy")
        .select("*", { count: "exact", head: true });
      if (error) throw error;
      connected = true;
    } catch (err) {
      console.error("health: backend probe failed —", err.message);
    }
  }

  const ok = connected && warnings.length === 0;
  // Minimal by design: a health check needs liveness + backend kind, nothing
  // more. The Supabase project prefix, env-presence flags, row count and error
  // text the old response carried leaked deployment shape — dropped here (and
  // logged server-side above), not exposed.
  return res
    .status(200)
    .json({ ok, ts: new Date().toISOString(), backend: "supabase" });
}
