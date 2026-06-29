import { getSupabase, validateConfig } from "./_supabase.js";

const VALID_STATUSES = [
  "unseen",
  "liked",
  "passed",
  "to_apply",
  "to_research",
  "to_network",
  "skipped",
  "applied",
  "expiring",
  "archived",
];

export default async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "POST")
    return res.status(405).json({ error: "Method not allowed" });

  if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    console.error("save: missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY");
    return res.status(500).json({ error: "Server misconfigured" });
  }

  const warnings = validateConfig();
  if (warnings.length)
    console.warn("save: config warning —", warnings.join("; "));

  const { id, status } = req.body;
  if (!id || !status)
    return res.status(400).json({ error: "Missing id or status" });

  if (!VALID_STATUSES.includes(status)) {
    return res.status(400).json({ error: "Invalid status" });
  }

  try {
    const { data, error } = await getSupabase()
      .from("vacancy")
      .update({ status, status_updated_at: new Date().toISOString() })
      .eq("id", id)
      .select("id");

    if (error) throw error;

    if (!data || data.length === 0) {
      console.warn(`save: vacancy not found — id=${id} status=${status}`);
      return res.status(404).json({ error: "Vacancy not found", id });
    }

    return res.status(200).json({ ok: true, ts: new Date().toISOString() });
  } catch (err) {
    console.error(`save: error — id=${id} status=${status}`, err.message);
    return res.status(500).json({ error: "Database error" });
  }
}
