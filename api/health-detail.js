import { getSupabase } from "./_supabase.js";
import { withHandler } from "./_handler.js";

// GET /api/health-detail — the Health tab's aggregate: is the pipeline working,
// what waits on you, did the learning loop move.
//
// Four read-only blocks (KTD6), each built from data the pipeline already
// stores — no new writes, no LLM spend:
//   boards    — per enabled board: freshness, failure streak, yield, a
//               PRESUMED-BROKEN flag (3+ consecutive failures, or fetched yet
//               zero vacancies).
//   companies — active companies whose direct fetch is failing, plus the ones
//               deliberately covered by boards/hand (not broken, just not
//               directly crawled).
//   waiting   — candidate companies pending review + unseen scored roles (with
//               the oldest one's age).
//   learning  — last review, changes applied since, verdicts waiting to be
//               reviewed (mirrors scripts/learning.py's cursor math).
//
// Same auth/method/config gate as the other status endpoints (withHandler).
// Every column added by a later migration (board/company last_success,
// consecutive_failures; company coverage; board hidden) is read defensively:
// an unknown-column error retries without it and the field degrades to its
// safe default, so a partially-migrated DB still answers.

// Triage decisions that count as a verdict (mirrors learning.DECISION_STATUSES).
const DECISION_STATUSES = [
  "liked",
  "to_apply",
  "to_research",
  "to_network",
  "applied",
  "passed",
];

// A company whose roles are surfaced by the boards or tracked by hand is NOT
// broken when it has no direct fetch — it was never meant to have one.
const NON_DIRECT_COVERAGE = ["board_only", "manual"];

// fetch_status values that mean the direct fetch RAN but produced no usable
// roles — the "never parses" signal the health tab exists to surface. ('ok' is
// success; null/'' means never attempted, not broken.) `last_success` is NOT
// used as a health signal: live it is populated for only a handful of rows
// (added by a recent migration, not backfilled), so its absence says nothing.
const NON_PRODUCING_STATUSES = [
  "error",
  "render_ok_zero",
  "no_data",
  "js_required",
];

function ageDays(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86400000);
}

// Try a select with the optional (later-migration) columns; on an
// unknown-column error retry with just the base columns. Returns { rows, cols }
// where cols reports which optional columns are actually present, so the caller
// normalises the missing ones to their defaults.
async function selectDefensive(supabase, table, baseCols, optionalCols, build) {
  const full = build(
    supabase.from(table).select([...baseCols, ...optionalCols].join(", ")),
  );
  const res = await full;
  if (!res.error)
    return { rows: res.data || [], present: new Set(optionalCols) };
  const base = build(supabase.from(table).select(baseCols.join(", ")));
  const res2 = await base;
  if (res2.error) throw res2.error;
  return { rows: res2.data || [], present: new Set() };
}

async function countBoardVacancies(supabase, boardName) {
  const { count, error } = await supabase
    .from("vacancy")
    .select("id", { count: "exact", head: true })
    .eq("source_board", boardName);
  if (error) throw error;
  return count || 0;
}

async function boardsBlock(supabase) {
  const { rows, present } = await selectDefensive(
    supabase,
    "board",
    ["id", "name", "last_fetched"],
    ["enabled", "hidden", "last_success", "consecutive_failures"],
    (q) => q,
  );
  // Only enabled boards are the operator's concern here; when the enabled
  // column is absent, every board counts as enabled (its historical default).
  const enabled = rows.filter((b) =>
    present.has("enabled") ? !!b.enabled : true,
  );
  const out = [];
  for (const b of enabled) {
    const vacancy_count = await countBoardVacancies(supabase, b.name);
    const consecutive_failures = present.has("consecutive_failures")
      ? b.consecutive_failures || 0
      : null;
    const presumed_broken =
      (consecutive_failures != null && consecutive_failures >= 3) ||
      (!!b.last_fetched && vacancy_count === 0);
    out.push({
      id: b.id,
      name: b.name,
      last_fetched: b.last_fetched || null,
      last_success: present.has("last_success") ? b.last_success || null : null,
      consecutive_failures,
      vacancy_count,
      presumed_broken,
    });
  }
  out.sort((a, b) => Number(b.presumed_broken) - Number(a.presumed_broken));
  return out;
}

async function companiesBlock(supabase) {
  // Page active companies (Supabase caps a select at 1000).
  const PAGE = 1000;
  const rows = [];
  let present = new Set();
  for (let from = 0; ; from += PAGE) {
    const { rows: page, present: p } = await selectDefensive(
      supabase,
      "company",
      ["canonical_name", "fetch_status", "last_fetched", "fetch_strategy"],
      ["consecutive_failures", "coverage"],
      (q) => q.eq("status", "active").range(from, from + PAGE - 1),
    );
    present = p;
    rows.push(...page);
    if (page.length < PAGE) break;
  }

  const coverageOf = (c) =>
    present.has("coverage") ? c.coverage || "direct" : "direct";

  const failing = [];
  const manual_check = [];
  for (const c of rows) {
    const coverage = coverageOf(c);
    if (
      NON_DIRECT_COVERAGE.includes(coverage) ||
      c.fetch_strategy === "manual_check"
    ) {
      manual_check.push({
        name: c.canonical_name,
        strategy: coverage !== "direct" ? coverage : c.fetch_strategy || "",
      });
      continue;
    }
    const cf = present.has("consecutive_failures")
      ? c.consecutive_failures || 0
      : 0;
    if (cf >= 3 || NON_PRODUCING_STATUSES.includes(c.fetch_status)) {
      failing.push({
        name: c.canonical_name,
        fetch_status: c.fetch_status || "",
        consecutive_failures: present.has("consecutive_failures") ? cf : null,
        last_fetched: c.last_fetched || null,
      });
    }
  }
  failing.sort(
    (a, b) => (b.consecutive_failures || 0) - (a.consecutive_failures || 0),
  );
  manual_check.sort((a, b) => a.name.localeCompare(b.name));
  return { failing, manual_check };
}

async function waitingBlock(supabase) {
  const { count: candidates, error: cErr } = await supabase
    .from("company")
    .select("id", { count: "exact", head: true })
    .eq("status", "candidate");
  if (cErr) throw cErr;

  const { count: unseen, error: uErr } = await supabase
    .from("vacancy")
    .select("id", { count: "exact", head: true })
    .eq("status", "unseen")
    .not("llm_score", "is", null);
  if (uErr) throw uErr;

  // Oldest unseen scored role — one row, ascending by first_seen.
  const { data: oldestRows, error: oErr } = await supabase
    .from("vacancy")
    .select("first_seen")
    .eq("status", "unseen")
    .not("llm_score", "is", null)
    .order("first_seen", { ascending: true })
    .limit(1);
  if (oErr) throw oErr;
  const oldest = oldestRows && oldestRows[0];

  return {
    candidates_pending: candidates || 0,
    unseen_scored: unseen || 0,
    oldest_unseen_age_days: oldest ? ageDays(oldest.first_seen) : null,
  };
}

async function learningBlock(supabase) {
  // The learning_log table (migration 0008) may be absent on an old DB —
  // degrade the whole block to nulls rather than fail the endpoint.
  try {
    const { data: reviewRows, error: rErr } = await supabase
      .from("learning_log")
      .select("created_at")
      .eq("kind", "reviewed")
      .order("created_at", { ascending: false })
      .limit(1);
    if (rErr) throw rErr;
    const cursor =
      reviewRows && reviewRows[0] ? reviewRows[0].created_at : null;

    let appliedQ = supabase
      .from("learning_log")
      .select("id", { count: "exact", head: true })
      .eq("kind", "applied");
    if (cursor) appliedQ = appliedQ.gt("created_at", cursor);
    const { count: applied, error: aErr } = await appliedQ;
    if (aErr) throw aErr;

    let verdictQ = supabase
      .from("vacancy")
      .select("id", { count: "exact", head: true })
      .in("status", DECISION_STATUSES);
    if (cursor) verdictQ = verdictQ.gt("status_updated_at", cursor);
    const { count: verdicts, error: vErr } = await verdictQ;
    if (vErr) throw vErr;

    return {
      last_review: cursor,
      last_review_age_days: cursor ? ageDays(cursor) : null,
      applied_since: applied || 0,
      verdicts_pending: verdicts || 0,
    };
  } catch (err) {
    console.warn("health-detail: learning block unavailable —", err.message);
    return {
      last_review: null,
      last_review_age_days: null,
      applied_since: null,
      verdicts_pending: null,
      unavailable: true,
    };
  }
}

export default withHandler(
  { method: "GET", label: "health-detail" },
  async (req, res) => {
    try {
      const supabase = getSupabase();
      const [boards, companies, waiting, learning] = await Promise.all([
        boardsBlock(supabase),
        companiesBlock(supabase),
        waitingBlock(supabase),
        learningBlock(supabase),
      ]);
      res.setHeader("Cache-Control", "no-store");
      return res.status(200).json({ boards, companies, waiting, learning });
    } catch (err) {
      console.error("health-detail: error", err.message);
      return res.status(500).json({ error: "Database error" });
    }
  },
);
