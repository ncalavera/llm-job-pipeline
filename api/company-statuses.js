import { getSupabase } from "./_supabase.js";
import { withHandler } from "./_handler.js";

export default withHandler(
  { method: "GET", label: "company-statuses" },
  async (req, res) => {
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
  },
);
