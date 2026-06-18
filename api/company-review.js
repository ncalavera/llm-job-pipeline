import { getSupabase, validateConfig } from "./_supabase.js";

const VALID_ACTIONS = ["approve", "reject"];

export default async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "POST")
    return res.status(405).json({ error: "Method not allowed" });

  if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    console.error(
      "company-review: missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY",
    );
    return res.status(500).json({ error: "Server misconfigured" });
  }

  const warnings = validateConfig();
  if (warnings.length)
    console.warn("company-review: config warning —", warnings.join("; "));

  const { company_id, action } = req.body;
  if (!company_id || !action)
    return res.status(400).json({ error: "Missing company_id or action" });

  if (!VALID_ACTIONS.includes(action)) {
    return res
      .status(400)
      .json({ error: "Invalid action — must be 'approve' or 'reject'" });
  }

  const newStatus = action === "approve" ? "active" : "inactive";
  const reason =
    action === "approve" ? "approved via dashboard" : "rejected via dashboard";

  try {
    const { data, error } = await getSupabase()
      .from("company")
      .update({ status: newStatus, status_reason: reason })
      .eq("id", company_id)
      .select("id, canonical_name");

    if (error) throw error;

    if (!data || data.length === 0) {
      return res.status(404).json({ error: "Company not found", company_id });
    }

    console.log(
      `company-review: ${action} — ${data[0].canonical_name} (${company_id})`,
    );
    return res
      .status(200)
      .json({ ok: true, action, company_id, ts: new Date().toISOString() });
  } catch (err) {
    console.error(
      `company-review: error — ${company_id} ${action}`,
      err.message,
    );
    return res.status(500).json({ error: "Database error" });
  }
}
