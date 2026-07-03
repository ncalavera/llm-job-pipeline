import { getSupabase } from "./_supabase.js";
import { withHandler } from "./_handler.js";

// Toggle a job board's persisted `enabled` flag from the dashboard.
//
// This writes ONLY board.enabled — the SAME field the CLI writes
// (scripts/sources.py enable-board / disable-board) and that run_daily.py
// unions into every run's board set. No side effects, no learning-log writes:
// the next pipeline run simply picks the new set up (STRATEGY guardrail 8).
//
// Sits behind the app's Basic Auth middleware like every other write endpoint;
// mirrors /api/save's contract (POST, withHandler preamble, 404 when the id
// isn't a known board, 500 on a DB error). Unlike save's `!status` guard we
// type-check `enabled`, because `false` is a legitimate value a truthiness
// check would wrongly reject.
export default withHandler(
  { method: "POST", label: "board-toggle" },
  async (req, res) => {
    const { board_id, enabled } = req.body;
    if (typeof board_id !== "string" || !board_id.trim())
      return res.status(400).json({ error: "Missing or invalid board_id" });
    if (typeof enabled !== "boolean")
      return res
        .status(400)
        .json({ error: "Missing or invalid enabled — must be a boolean" });

    try {
      const { data, error } = await getSupabase()
        .from("board")
        .update({ enabled, updated_at: new Date().toISOString() })
        .eq("id", board_id)
        .select("id");

      if (error) throw error;

      // Update-only + 404 (like /api/save): board_id is validated against the
      // known board set — the rows the pipeline actually keeps — so an unknown
      // or never-synced id fails closed instead of creating a bare row.
      if (!data || data.length === 0) {
        return res.status(404).json({ error: "Board not found", board_id });
      }

      console.log(`board-toggle: ${board_id} → enabled=${enabled}`);
      return res
        .status(200)
        .json({ ok: true, board_id, enabled, ts: new Date().toISOString() });
    } catch (err) {
      console.error(
        `board-toggle: error — ${board_id} enabled=${enabled}`,
        err.message,
      );
      return res.status(500).json({ error: "Database error" });
    }
  },
);
