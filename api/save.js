import { getSupabase } from "./_supabase.js";
import { withHandler } from "./_handler.js";

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

export default withHandler(
  { method: "POST", label: "save" },
  async (req, res) => {
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
  },
);
