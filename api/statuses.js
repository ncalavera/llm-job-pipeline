import { getSupabase, validateConfig } from "./_supabase.js";

export default async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Authorization");

  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "GET")
    return res.status(405).json({ error: "Method not allowed" });

  if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    console.error(
      "statuses: missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY",
    );
    return res.status(500).json({ error: "Server misconfigured" });
  }

  const warnings = validateConfig();
  if (warnings.length)
    console.warn("statuses: config warning —", warnings.join("; "));

  try {
    const { data, error } = await getSupabase()
      .from("vacancy")
      .select("id, status, status_updated_at")
      .neq("status", "unseen")
      .neq("status", "archived");

    if (error) throw error;

    const statuses = {};
    const timestamps = {};
    for (const row of data) {
      statuses[row.id] = row.status;
      if (row.status_updated_at) {
        timestamps[row.id] = row.status_updated_at;
      }
    }
    return res.status(200).json({ statuses, timestamps });
  } catch (err) {
    console.error("statuses: error", err.message);
    return res.status(500).json({ error: "Database error" });
  }
}
