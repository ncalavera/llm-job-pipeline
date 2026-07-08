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

      // `enabled` (migration 0011) and `hidden` (later) may be absent on a
      // partially-migrated DB. Try the full select; on an unknown-column error
      // fall back to the base columns and treat the flags as their defaults
      // (enabled true / hidden false) so the boards tab still renders.
      const BASE_COLS = "id, name, strategy, tier, ttl_days, url, last_fetched";
      let catalog;
      const full = await supabase
        .from("board")
        .select(`${BASE_COLS}, enabled, hidden`);
      if (full.error) {
        const base = await supabase.from("board").select(BASE_COLS);
        if (base.error) throw base.error;
        catalog = base.data;
      } else {
        catalog = full.data;
      }

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
            // Normalise the two flags so the client never sees undefined:
            // a missing column (older schema) reads as enabled / not hidden.
            enabled: b.enabled == null ? true : !!b.enabled,
            hidden: !!b.hidden,
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
