import { getSupabase, validateConfig } from "./_supabase.js";

// GET /api/companies — live company rows for the dashboard's Companies tab.
//
// The dashboard snapshot (served by /api/vacancies) is only rebuilt by a
// fetch/score run, so its company list, statuses and tiers go stale between
// runs. This endpoint reads the company + vacancy tables live so the Companies
// tab's list, counts, tier, fit and monitoring are always current. Deep-profile
// fields (executive summary, full enrichment) still come from the snapshot —
// they are built from private local files the deployment does not carry.
//
// Same-origin only + no-store: the payload carries scoring text (PII), gated by
// the middleware.js Basic Auth like /api/vacancies.
const REVIEW_MAP = {
  active: "approved",
  candidate: "pending",
  inactive: "rejected",
};
const CUSTOM_BOOST_KEYS = ["career_narrative_boost", "mpa_narrative_boost"];

function slugify(name) {
  return (name || "").toLowerCase().replace(/ /g, "-").replace(/\./g, "");
}

function customBoost(mission) {
  if (!mission || typeof mission !== "object") return null;
  for (const k of CUSTOM_BOOST_KEYS) {
    const v = mission[k];
    if (typeof v === "number") return v;
  }
  return null;
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");

  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "GET")
    return res.status(405).json({ error: "Method not allowed" });

  if (!process.env.AUTH_USER || !process.env.AUTH_PASS) {
    console.error("companies: refusing to serve — AUTH_USER/AUTH_PASS not set");
    return res.status(503).json({ error: "Auth not configured" });
  }
  if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    console.error(
      "companies: missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY",
    );
    return res.status(500).json({ error: "Server misconfigured" });
  }

  const warnings = validateConfig();
  if (warnings.length)
    console.warn("companies: config warning —", warnings.join("; "));

  try {
    const supabase = getSupabase();

    // Supabase caps a single select at 1000 rows — page through so newly
    // enriched companies beyond the first 1000 still reach the dashboard.
    const COMPANY_PAGE = 1000;
    const rows = [];
    for (let from = 0; ; from += COMPANY_PAGE) {
      const { data: page, error: cErr } = await supabase
        .from("company")
        .select(
          "id, canonical_name, status, tier, alignment_score, mission_fit, " +
            "about, notes, experience_match, personal_interest, " +
            "website, careers_url, " +
            "offices, category, fetch_strategy, fetch_status, last_fetched, " +
            "vacancy_count, new_count",
        )
        .range(from, from + COMPANY_PAGE - 1);
      if (cErr) throw cErr;
      rows.push(...(page || []));
      if (!page || page.length < COMPANY_PAGE) break;
    }

    // Live vacancy ids + counts per company (non-archived only — the table's
    // "Vacancies"/"New"/"Liked" columns are about live roles).
    const vacByCompany = {};
    const PAGE = 1000;
    for (let from = 0; ; from += PAGE) {
      const { data: vacs, error: vErr } = await supabase
        .from("vacancy")
        .select("id, company_id, status")
        .neq("status", "archived")
        .range(from, from + PAGE - 1);
      if (vErr) throw vErr;
      for (const v of vacs || []) {
        const bucket = (vacByCompany[v.company_id] ||= {
          ids: [],
          total: 0,
          liked: 0,
          unseen: 0,
        });
        bucket.ids.push(v.id);
        bucket.total += 1;
        // "Selected" = anything the user has touched and kept: everything
        // except untouched (unseen) and rejected (passed). Includes applied,
        // and stays correct for any future positive status (e.g. interviewing).
        if (!["unseen", "passed"].includes(v.status)) bucket.liked += 1;
        if (v.status === "unseen") bucket.unseen += 1;
      }
      if (!vacs || vacs.length < PAGE) break;
    }

    const companies = (rows || []).map((c) => {
      const vc = vacByCompany[c.id] || {
        ids: [],
        total: 0,
        liked: 0,
        unseen: 0,
      };
      const strategy = c.fetch_strategy || "";
      const about = c.about && typeof c.about === "object" ? c.about : {};
      const mission =
        c.mission_fit && typeof c.mission_fit === "object" ? c.mission_fit : {};
      const alignmentScore =
        c.alignment_score != null
          ? Number(c.alignment_score)
          : mission.alignment_score != null
            ? Number(mission.alignment_score)
            : null;
      const isEnriched = !!(
        about.description || mission.alignment_score != null
      );
      return {
        company_id: String(c.id),
        name: c.canonical_name,
        slug: slugify(c.canonical_name),
        status: (c.status || "").toLowerCase(),
        review_status: REVIEW_MAP[(c.status || "").toLowerCase()] || "pending",
        calculated_tier: c.tier || null,
        alignment_score: alignmentScore,
        mpa_prestige: customBoost(c.mission_fit),
        // website / careers_url are plain DB columns, not private-file fields —
        // serve them live so a company's profile links stay current even when
        // the snapshot is stale or lacks the row (e.g. pending/candidate orgs).
        // Emit undefined (not "") when absent so the snapshot merge can still
        // fill an older value; res.json drops undefined keys.
        website: c.website || undefined,
        careers_url: c.careers_url || undefined,
        offices: c.offices || "",
        category: c.category || "",
        strategy,
        fetch_status: c.fetch_status || "",
        last_fetched: c.last_fetched || "",
        is_manual_check: strategy === "manual_check",
        needs_source: !strategy && vc.total === 0,
        is_archived: (c.status || "").toLowerCase() === "inactive",
        vacancy_count: vc.total,
        liked_count: vc.liked,
        new_count: vc.unseen,
        vacancy_ids: vc.ids,
        // --- Enrichment fields (live from the about / mission_fit JSONB) ---
        is_enriched: isEnriched,
        experience_match: c.experience_match,
        personal_interest: c.personal_interest,
        notes: c.notes || "",
        description: about.description || "",
        sector: about.sector || "",
        founded_year: about.founded_year || "",
        employee_count: about.employee_count || "",
        funding_status: about.funding_status || "",
        hq_location: about.hq_location || "",
        alignment_label: mission.alignment_label || "",
        // Per-aspect WANT scores (drives the profile "WANT breakdown" chart and
        // the sortable aspect columns). Only emit when present so the snapshot
        // merge in api.js still fills older rows; undefined keys are dropped by
        // res.json, leaving live[f] === undefined for the merge to overwrite.
        fit_dimensions:
          mission.dimensions &&
          typeof mission.dimensions === "object" &&
          Object.keys(mission.dimensions).length
            ? mission.dimensions
            : undefined,
        fit_strengths: mission.strengths || [],
        fit_risks: mission.risks || [],
        fit_approach: mission.approach || "",
        experience_reasoning: mission.experience_match_reasoning || "",
        mission_verdict: mission.mission_verdict || "",
      };
    });

    return res.status(200).json({ companies });
  } catch (err) {
    console.error("companies: error", err.message);
    return res.status(500).json({ error: "Database error" });
  }
}
