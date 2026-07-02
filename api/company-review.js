import { getSupabase } from "./_supabase.js";
import { withHandler } from "./_handler.js";

const VALID_ACTIONS = ["approve", "reject"];

export default withHandler(
  { method: "POST", label: "company-review" },
  async (req, res) => {
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
      action === "approve"
        ? "approved via dashboard"
        : "rejected via dashboard";

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
  },
);
