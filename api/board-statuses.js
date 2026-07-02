import { getSupabase } from "./_supabase.js";
import { withHandler } from "./_handler.js";

// Live status of monitored job boards. Catalog + last_fetched come from the
// `board` table (kept in sync from config by sync_boards); vacancy counts come
// from vacancy.source_board (the board's display name).
export default withHandler(
  { method: "GET", label: "board-statuses" },
  async (req, res) => {
    try {
      const supabase = getSupabase();

      const { data: catalog, error: catalogErr } = await supabase
        .from("board")
        .select("id, name, strategy, tier, ttl_days, url, last_fetched");
      if (catalogErr) throw catalogErr;

      const recentCutoff = new Date(
        Date.now() - 14 * 24 * 60 * 60 * 1000,
      ).toISOString();

      const boards = await Promise.all(
        (catalog || []).map(async (b) => {
          const totalQ = supabase
            .from("vacancy")
            .select("id", { count: "exact", head: true })
            .eq("source_board", b.name);
          const recentQ = supabase
            .from("vacancy")
            .select("id", { count: "exact", head: true })
            .eq("source_board", b.name)
            .gte("last_seen", recentCutoff);
          const [totalRes, recentRes] = await Promise.all([totalQ, recentQ]);
          if (totalRes.error) throw totalRes.error;
          if (recentRes.error) throw recentRes.error;

          let overdue = true;
          if (b.last_fetched && b.ttl_days != null) {
            const ageDays =
              (Date.now() - new Date(b.last_fetched).getTime()) / 86400000;
            overdue = ageDays >= b.ttl_days;
          }

          return {
            ...b,
            vac_total: totalRes.count || 0,
            vac_recent: recentRes.count || 0,
            overdue,
          };
        }),
      );

      return res.status(200).json({ boards });
    } catch (err) {
      console.error("board-statuses: error", err.message);
      return res.status(500).json({ error: "Database error" });
    }
  },
);
