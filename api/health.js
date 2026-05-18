import { getSupabase, getSupabaseUrlPrefix, validateConfig } from "./_supabase.js";

export default async function handler(req, res) {
  if (req.method !== "GET") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const warnings = validateConfig();
  if (warnings.length) {
    console.warn("health: config warnings —", warnings.join("; "));
  }

  const env = {};
  for (const key of ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "AUTH_USER", "AUTH_PASS"]) {
    env[key] = process.env[key] ? "set" : "MISSING";
  }

  const supabaseInfo = { url_prefix: getSupabaseUrlPrefix(), connected: false };

  if (process.env.SUPABASE_URL && process.env.SUPABASE_SERVICE_ROLE_KEY) {
    try {
      const { count, error } = await getSupabase()
        .from("vacancy")
        .select("*", { count: "exact", head: true });

      if (error) throw error;
      supabaseInfo.connected = true;
      supabaseInfo.vacancy_count = count;
    } catch (err) {
      supabaseInfo.error = err.message;
    }
  }

  const ok = supabaseInfo.connected && warnings.length === 0;
  return res.status(200).json({ ok, ts: new Date().toISOString(), supabase: supabaseInfo, env, warnings });
}
