import { getSupabase } from "./_supabase.js";
import { withHandler } from "./_handler.js";

export default withHandler(
  { method: "GET", label: "statuses" },
  async (req, res) => {
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
  },
);
