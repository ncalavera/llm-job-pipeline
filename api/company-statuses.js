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
      "company-statuses: missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY",
    );
    return res.status(500).json({ error: "Server misconfigured" });
  }

  const warnings = validateConfig();
  if (warnings.length)
    console.warn("company-statuses: config warning —", warnings.join("; "));

  try {
    // Page through — Supabase caps a single select at 1000 rows, which would
    // drop status overrides for companies beyond the first 1000.
    const PAGE = 1000;
    const data = [];
    for (let from = 0; ; from += PAGE) {
      const { data: page, error } = await getSupabase()
        .from("company")
        .select("id, status")
        .range(from, from + PAGE - 1);
      if (error) throw error;
      data.push(...(page || []));
      if (!page || page.length < PAGE) break;
    }

    const statusMap = {
      active: "approved",
      candidate: "pending",
      inactive: "rejected",
    };
    const statuses = {};
    for (const row of data) {
      statuses[row.id] = statusMap[row.status] || "pending";
    }
    return res.status(200).json({ statuses });
  } catch (err) {
    console.error("company-statuses: error", err.message);
    return res.status(500).json({ error: "Database error" });
  }
}
