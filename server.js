// server.js — self-hosted replacement for the Vercel deployment.
//
// One plain-Node HTTP server: serves public/ statically and reimplements the
// api/*.js endpoints against local Postgres (`pg` + DATABASE_URL) with
// contracts identical to the Supabase-backed handlers, so nothing under
// public/ changes. See MIGRATION.md for the full contract map, the systemd
// unit and the Caddy site block (Caddy owns TLS + Basic Auth — the
// middleware.js gate does NOT move here; bind stays on 127.0.0.1).
//
// Assumes a fully migrated database (every sql/migrations/*.postgres.sql
// applied). The Vercel handlers' unknown-column fallbacks for
// partially-migrated DBs are deliberately not carried over.
//
// Starts fine without a database: static files serve, API routes answer
// 500 "Server misconfigured" (no DATABASE_URL) or "Database error"
// (unreachable DB) — the same errors the Vercel handlers give.

import { createServer } from "node:http";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";

const PUBLIC_DIR = join(fileURLToPath(new URL(".", import.meta.url)), "public");

// ---------------------------------------------------------------------------
// Postgres
// ---------------------------------------------------------------------------

// DATE columns (vacancy.first_seen / last_seen / deadline) come back as plain
// "YYYY-MM-DD" strings — the format PostgREST served — instead of local-midnight
// Date objects. COUNT(*) (int8) comes back as a number.
pg.types.setTypeParser(1082, (v) => v);
pg.types.setTypeParser(20, (v) => parseInt(v, 10));

let _pool = null;
function getPool() {
  if (!_pool) {
    _pool = new pg.Pool({
      connectionString: process.env.DATABASE_URL,
      max: 5,
    });
    // A dropped idle connection must not crash the process.
    _pool.on("error", (err) => console.error("pg pool error:", err.message));
  }
  return _pool;
}

// ---------------------------------------------------------------------------
// Small response helpers (the subset of the Vercel res API the handlers used)
// ---------------------------------------------------------------------------

function sendJson(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(data),
  });
  res.end(data);
}

function sendEmpty(res, status) {
  res.writeHead(status);
  res.end();
}

/** Read and JSON-parse a request body; an unparseable/empty body yields {}
 * so the handlers' own "Missing …" 400 checks fire, like Vercel's parser. */
function readJsonBody(req) {
  return new Promise((resolvePromise) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > 1_000_000) {
        req.destroy();
        resolvePromise({});
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      try {
        resolvePromise(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch {
        resolvePromise({});
      }
    });
    req.on("error", () => resolvePromise({}));
  });
}

// ---------------------------------------------------------------------------
// ETag helpers — copied verbatim from api/vacancies.js (kept there for the
// Vercel deployment until cutover; duplicated so this server does not import
// the @supabase/supabase-js dependency chain).
// ---------------------------------------------------------------------------

/** Build the ETag for a snapshot version from its `updated_at` timestamp. */
export function computeETag(updatedAt) {
  return updatedAt ? `"${updatedAt}"` : null;
}

/** Strip an RFC 9110 weak-validator prefix: proxies may turn a strong ETag
 * into `W/"…"` on the way to the client. Comparing weakly keeps the 304 path
 * alive — losing it silently re-ships the full multi-MB payload every poll. */
function opaqueTag(tag) {
  const t = tag.trim();
  return t.startsWith("W/") ? t.slice(2) : t;
}

/** True when the client's cached copy (If-None-Match) is still current.
 * Weak comparison over a possibly comma-separated If-None-Match list. */
export function isNotModified(ifNoneMatch, etag) {
  if (!ifNoneMatch || !etag) return false;
  if (ifNoneMatch.trim() === "*") return true;
  const target = opaqueTag(etag);
  return ifNoneMatch.split(",").some((tag) => opaqueTag(tag) === target);
}

// ---------------------------------------------------------------------------
// Handler preambles (ports of api/vacancies.js's inline gate and
// api/_handler.js's withHandler — minus the Vercel-specific AUTH_USER /
// AUTH_PASS fail-closed check, which Caddy + the loopback bind replace).
// ---------------------------------------------------------------------------

/** Same-origin PII readers (/api/vacancies, /api/companies): no CORS header,
 * no-store. Returns true when the preamble already answered. */
function piiPreamble(req, res, label) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method === "OPTIONS") {
    sendEmpty(res, 204);
    return true;
  }
  if (req.method !== "GET") {
    sendJson(res, 405, { error: "Method not allowed" });
    return true;
  }
  if (!process.env.DATABASE_URL) {
    console.error(`${label}: missing DATABASE_URL`);
    sendJson(res, 500, { error: "Server misconfigured" });
    return true;
  }
  return false;
}

/** The withHandler preamble: permissive CORS, OPTIONS preflight, method
 * guard, DB config check. Returns true when it already answered. */
function wrappedPreamble(req, res, method, label) {
  const allowHeaders =
    method === "POST" ? "Content-Type, Authorization" : "Authorization";
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", `${method}, OPTIONS`);
  res.setHeader("Access-Control-Allow-Headers", allowHeaders);

  if (req.method === "OPTIONS") {
    sendEmpty(res, 204);
    return true;
  }
  if (req.method !== method) {
    sendJson(res, 405, { error: "Method not allowed" });
    return true;
  }
  if (!process.env.DATABASE_URL) {
    console.error(`${label}: missing DATABASE_URL`);
    sendJson(res, 500, { error: "Server misconfigured" });
    return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// GET /api/vacancies — see api/vacancies.js
// ---------------------------------------------------------------------------

async function handleVacancies(req, res) {
  if (piiPreamble(req, res, "vacancies")) return;
  try {
    // Cheap first: only the version column. An unchanged poll (If-None-Match
    // still current) stops here — the JSONB payload never leaves Postgres.
    // to_json → ISO 8601 ("2026-08-20T09:53:29.680292+04:00"): the same shape
    // PostgREST served, and — unlike `updated_at::text` — free of spaces,
    // which are not valid inside an HTTP entity-tag (RFC 9110 §8.8.3).
    const meta = await getPool().query(
      "SELECT to_json(updated_at) #>> '{}' AS updated_at FROM dashboard_snapshot WHERE id = 'current'",
    );
    if (meta.rowCount === 0) {
      // NOT 404 — that is bootstrap.js's "endpoint absent → static data.js"
      // signal, wrong here (full mode ships no data.js).
      return sendJson(res, 503, { error: "Snapshot not generated yet" });
    }

    const etag = computeETag(meta.rows[0].updated_at);
    if (etag) res.setHeader("ETag", etag);
    if (isNotModified(req.headers["if-none-match"], etag)) {
      return sendEmpty(res, 304);
    }

    const data = await getPool().query(
      "SELECT payload FROM dashboard_snapshot WHERE id = 'current'",
    );
    if (data.rowCount === 0) {
      return sendJson(res, 503, { error: "Snapshot not generated yet" });
    }
    return sendJson(res, 200, data.rows[0].payload);
  } catch (err) {
    console.error("vacancies: error", err.message);
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/companies — see api/companies.js
// ---------------------------------------------------------------------------

const REVIEW_MAP = {
  active: "approved",
  candidate: "pending",
  inactive: "rejected",
};

function slugify(name) {
  return (name || "").toLowerCase().replace(/ /g, "-").replace(/\./g, "");
}

async function handleCompanies(req, res) {
  if (piiPreamble(req, res, "companies")) return;
  try {
    const pool = getPool();
    // Plain SQL — no PostgREST 1000-row paging loops needed.
    const { rows } = await pool.query(
      `SELECT id, canonical_name, status, tier, alignment_score, mission_fit,
              about, notes, experience_match, personal_interest,
              website, careers_url,
              offices, category, fetch_strategy, fetch_status, last_fetched
         FROM company`,
    );

    // Live vacancy ids + counts per company (non-archived only).
    const { rows: vacs } = await pool.query(
      `SELECT id, company_id, status FROM vacancy WHERE status <> 'archived'`,
    );
    const vacByCompany = {};
    for (const v of vacs) {
      const bucket = (vacByCompany[v.company_id] ||= {
        ids: [],
        total: 0,
        liked: 0,
        unseen: 0,
      });
      bucket.ids.push(v.id);
      bucket.total += 1;
      // "Selected" = anything the user touched and kept: everything except
      // untouched (unseen) and rejected (passed).
      if (!["unseen", "passed"].includes(v.status)) bucket.liked += 1;
      if (v.status === "unseen") bucket.unseen += 1;
    }

    const companies = rows.map((c) => {
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
        // Emit undefined (not "") when absent so the client's snapshot merge
        // can still fill an older value; JSON.stringify drops undefined keys.
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

    return sendJson(res, 200, { companies });
  } catch (err) {
    console.error("companies: error", err.message);
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// POST /api/save — see api/save.js
// ---------------------------------------------------------------------------

const VALID_STATUSES = [
  "unseen",
  "liked",
  "passed",
  "to_apply",
  "to_research",
  "to_network",
  "skipped",
  "applied",
  "interview",
  "declined",
  "expiring",
  "archived",
];

async function handleSave(req, res) {
  if (wrappedPreamble(req, res, "POST", "save")) return;
  const { id, status } = await readJsonBody(req);
  if (!id || !status)
    return sendJson(res, 400, { error: "Missing id or status" });
  if (!VALID_STATUSES.includes(status))
    return sendJson(res, 400, { error: "Invalid status" });

  try {
    const result = await getPool().query(
      `UPDATE vacancy SET status = $1, status_updated_at = $2
        WHERE id = $3::uuid RETURNING id`,
      [status, new Date().toISOString(), id],
    );
    if (result.rowCount === 0) {
      console.warn(`save: vacancy not found — id=${id} status=${status}`);
      return sendJson(res, 404, { error: "Vacancy not found", id });
    }
    return sendJson(res, 200, { ok: true, ts: new Date().toISOString() });
  } catch (err) {
    console.error(`save: error — id=${id} status=${status}`, err.message);
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/statuses — see api/statuses.js
// ---------------------------------------------------------------------------

async function handleStatuses(req, res) {
  if (wrappedPreamble(req, res, "GET", "statuses")) return;
  try {
    const { rows } = await getPool().query(
      `SELECT id, status, status_updated_at FROM vacancy
        WHERE status <> 'unseen' AND status <> 'archived'`,
    );
    const statuses = {};
    const timestamps = {};
    for (const row of rows) {
      statuses[row.id] = row.status;
      if (row.status_updated_at) {
        timestamps[row.id] = row.status_updated_at;
      }
    }
    return sendJson(res, 200, { statuses, timestamps });
  } catch (err) {
    console.error("statuses: error", err.message);
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// POST /api/company-review — see api/company-review.js
// ---------------------------------------------------------------------------

const VALID_ACTIONS = ["approve", "reject"];

async function handleCompanyReview(req, res) {
  if (wrappedPreamble(req, res, "POST", "company-review")) return;
  const { company_id, action } = await readJsonBody(req);
  if (!company_id || !action)
    return sendJson(res, 400, { error: "Missing company_id or action" });
  if (!VALID_ACTIONS.includes(action))
    return sendJson(res, 400, {
      error: "Invalid action — must be 'approve' or 'reject'",
    });

  const newStatus = action === "approve" ? "active" : "inactive";
  const reason =
    action === "approve" ? "approved via dashboard" : "rejected via dashboard";

  try {
    const result = await getPool().query(
      `UPDATE company SET status = $1, status_reason = $2
        WHERE id = $3::uuid RETURNING id, canonical_name`,
      [newStatus, reason, company_id],
    );
    if (result.rowCount === 0) {
      return sendJson(res, 404, { error: "Company not found", company_id });
    }
    console.log(
      `company-review: ${action} — ${result.rows[0].canonical_name} (${company_id})`,
    );
    return sendJson(res, 200, {
      ok: true,
      action,
      company_id,
      ts: new Date().toISOString(),
    });
  } catch (err) {
    console.error(
      `company-review: error — ${company_id} ${action}`,
      err.message,
    );
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/company-statuses — see api/company-statuses.js
// ---------------------------------------------------------------------------

async function handleCompanyStatuses(req, res) {
  if (wrappedPreamble(req, res, "GET", "company-statuses")) return;
  try {
    const { rows } = await getPool().query("SELECT id, status FROM company");
    const statuses = {};
    for (const row of rows) {
      statuses[row.id] = REVIEW_MAP[row.status] || "pending";
    }
    return sendJson(res, 200, { statuses });
  } catch (err) {
    console.error("company-statuses: error", err.message);
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/board-statuses — see api/board-statuses.js
// ---------------------------------------------------------------------------

/** One grouped pass over vacancy.source_board replaces the per-board COUNT
 * round-trips the PostgREST version made. Returns Map(name → {total, recent}). */
async function boardVacancyCounts(pool, recentCutoffIso) {
  const { rows } = await pool.query(
    `SELECT source_board,
            COUNT(*)::int AS total,
            COUNT(*) FILTER (WHERE last_seen >= $1::date)::int AS recent
       FROM vacancy
      WHERE source_board IS NOT NULL
      GROUP BY source_board`,
    [recentCutoffIso],
  );
  return new Map(rows.map((r) => [r.source_board, r]));
}

async function handleBoardStatuses(req, res) {
  if (wrappedPreamble(req, res, "GET", "board-statuses")) return;
  try {
    const pool = getPool();
    const { rows: catalog } = await pool.query(
      `SELECT id, name, strategy, tier, ttl_days, url, last_fetched,
              enabled, hidden
         FROM board`,
    );
    const recentCutoff = new Date(
      Date.now() - 14 * 24 * 60 * 60 * 1000,
    ).toISOString();
    const counts = await boardVacancyCounts(pool, recentCutoff);

    const boards = catalog.map((b) => {
      let overdue = true;
      if (b.last_fetched && b.ttl_days != null) {
        const ageDays =
          (Date.now() - new Date(b.last_fetched).getTime()) / 86400000;
        overdue = ageDays >= b.ttl_days;
      }
      const c = counts.get(b.name);
      return {
        ...b,
        // Normalise the two flags so the client never sees undefined.
        enabled: b.enabled == null ? true : !!b.enabled,
        hidden: !!b.hidden,
        vac_total: (c && c.total) || 0,
        vac_recent: (c && c.recent) || 0,
        overdue,
      };
    });

    return sendJson(res, 200, { boards });
  } catch (err) {
    console.error("board-statuses: error", err.message);
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// POST /api/board-toggle — see api/board-toggle.js
// ---------------------------------------------------------------------------

async function handleBoardToggle(req, res) {
  if (wrappedPreamble(req, res, "POST", "board-toggle")) return;
  const { board_id, enabled } = await readJsonBody(req);
  if (typeof board_id !== "string" || !board_id.trim())
    return sendJson(res, 400, { error: "Missing or invalid board_id" });
  if (typeof enabled !== "boolean")
    return sendJson(res, 400, {
      error: "Missing or invalid enabled — must be a boolean",
    });

  try {
    const result = await getPool().query(
      `UPDATE board SET enabled = $1, updated_at = $2
        WHERE id = $3 RETURNING id`,
      [enabled, new Date().toISOString(), board_id],
    );
    // Update-only + 404: an unknown or never-synced id fails closed instead
    // of creating a bare row.
    if (result.rowCount === 0) {
      return sendJson(res, 404, { error: "Board not found", board_id });
    }
    console.log(`board-toggle: ${board_id} → enabled=${enabled}`);
    return sendJson(res, 200, {
      ok: true,
      board_id,
      enabled,
      ts: new Date().toISOString(),
    });
  } catch (err) {
    console.error(
      `board-toggle: error — ${board_id} enabled=${enabled}`,
      err.message,
    );
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/health — see api/health.js
// ---------------------------------------------------------------------------

async function handleHealth(req, res) {
  if (req.method !== "GET") {
    return sendJson(res, 405, { error: "Method not allowed" });
  }
  let connected = false;
  if (process.env.DATABASE_URL) {
    try {
      await getPool().query("SELECT COUNT(*) FROM vacancy");
      connected = true;
    } catch (err) {
      console.error("health: backend probe failed —", err.message);
    }
  }
  // Minimal by design: liveness + backend kind, nothing that leaks
  // deployment shape.
  return sendJson(res, 200, {
    ok: connected,
    ts: new Date().toISOString(),
    backend: "postgres",
  });
}

// ---------------------------------------------------------------------------
// GET /api/health-detail — see api/health-detail.js
// ---------------------------------------------------------------------------

// Triage decisions that count as a verdict (mirrors learning.DECISION_STATUSES).
const DECISION_STATUSES = [
  "liked",
  "to_apply",
  "to_research",
  "to_network",
  "applied",
  "passed",
];

const NON_DIRECT_COVERAGE = ["board_only", "manual"];

// fetch_status values that mean the direct fetch RAN but produced no usable
// roles. ('ok' is success; null/'' means never attempted, not broken.)
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

async function boardsBlock(pool) {
  const { rows } = await pool.query(
    `SELECT id, name, last_fetched, enabled, hidden,
            last_success, consecutive_failures
       FROM board`,
  );
  const counts = await boardVacancyCounts(pool, new Date(0).toISOString());
  const out = rows
    .filter((b) => !!b.enabled)
    .map((b) => {
      const c = counts.get(b.name);
      const vacancy_count = (c && c.total) || 0;
      const consecutive_failures = b.consecutive_failures || 0;
      const presumed_broken =
        consecutive_failures >= 3 || (!!b.last_fetched && vacancy_count === 0);
      return {
        id: b.id,
        name: b.name,
        last_fetched: b.last_fetched || null,
        last_success: b.last_success || null,
        consecutive_failures,
        vacancy_count,
        presumed_broken,
      };
    });
  out.sort((a, b) => Number(b.presumed_broken) - Number(a.presumed_broken));
  return out;
}

async function companiesBlock(pool) {
  const { rows } = await pool.query(
    `SELECT canonical_name, fetch_status, last_fetched, fetch_strategy,
            consecutive_failures, coverage
       FROM company
      WHERE status = 'active'`,
  );
  const failing = [];
  const manual_check = [];
  for (const c of rows) {
    const coverage = c.coverage || "direct";
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
    const cf = c.consecutive_failures || 0;
    if (cf >= 3 || NON_PRODUCING_STATUSES.includes(c.fetch_status)) {
      failing.push({
        name: c.canonical_name,
        fetch_status: c.fetch_status || "",
        consecutive_failures: cf,
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

async function waitingBlock(pool) {
  const candidates = await pool.query(
    `SELECT COUNT(*) AS n FROM company WHERE status = 'candidate'`,
  );
  const unseen = await pool.query(
    `SELECT COUNT(*) AS n FROM vacancy
      WHERE status = 'unseen' AND llm_score IS NOT NULL`,
  );
  const oldest = await pool.query(
    `SELECT first_seen FROM vacancy
      WHERE status = 'unseen' AND llm_score IS NOT NULL
      ORDER BY first_seen ASC LIMIT 1`,
  );
  return {
    candidates_pending: candidates.rows[0].n || 0,
    unseen_scored: unseen.rows[0].n || 0,
    oldest_unseen_age_days:
      oldest.rowCount > 0 ? ageDays(oldest.rows[0].first_seen) : null,
  };
}

async function learningBlock(pool) {
  // The learning_log table (migration 0008) may be absent on an old DB —
  // degrade the whole block to nulls rather than fail the endpoint.
  try {
    const review = await pool.query(
      `SELECT created_at FROM learning_log
        WHERE kind = 'reviewed'
        ORDER BY created_at DESC LIMIT 1`,
    );
    const cursor = review.rowCount > 0 ? review.rows[0].created_at : null;

    const applied = await pool.query(
      `SELECT COUNT(*) AS n FROM learning_log
        WHERE kind = 'applied' AND ($1::timestamptz IS NULL OR created_at > $1)`,
      [cursor],
    );
    const verdicts = await pool.query(
      `SELECT COUNT(*) AS n FROM vacancy
        WHERE status = ANY($1)
          AND ($2::timestamptz IS NULL OR status_updated_at > $2)`,
      [DECISION_STATUSES, cursor],
    );

    return {
      last_review: cursor,
      last_review_age_days: cursor ? ageDays(cursor) : null,
      applied_since: applied.rows[0].n || 0,
      verdicts_pending: verdicts.rows[0].n || 0,
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

async function handleHealthDetail(req, res) {
  if (wrappedPreamble(req, res, "GET", "health-detail")) return;
  try {
    const pool = getPool();
    const [boards, companies, waiting, learning] = await Promise.all([
      boardsBlock(pool),
      companiesBlock(pool),
      waitingBlock(pool),
      learningBlock(pool),
    ]);
    res.setHeader("Cache-Control", "no-store");
    return sendJson(res, 200, { boards, companies, waiting, learning });
  } catch (err) {
    console.error("health-detail: error", err.message);
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// Static files — public/ at the site root, matching the vercel.json headers.
// ---------------------------------------------------------------------------

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
  ".map": "application/json",
  ".woff2": "font/woff2",
  ".webmanifest": "application/manifest+json",
};

async function handleStatic(req, res, pathname) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    return sendJson(res, 405, { error: "Method not allowed" });
  }
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return sendEmpty(res, 404); // malformed percent-encoding
  }
  const rel = decoded === "/" ? "index.html" : decoded.slice(1);
  const filePath = resolve(join(PUBLIC_DIR, normalize(rel)));
  if (filePath !== PUBLIC_DIR && !filePath.startsWith(PUBLIC_DIR + sep)) {
    return sendEmpty(res, 404); // path traversal
  }
  let info;
  try {
    info = await stat(filePath);
  } catch {
    return sendEmpty(res, 404);
  }
  if (!info.isFile()) return sendEmpty(res, 404);

  res.writeHead(200, {
    "Content-Type": MIME[extname(filePath)] || "application/octet-stream",
    "Content-Length": info.size,
    // vercel.json set this for /, /index.html and *.js|css; uniform here.
    "Cache-Control": "public, max-age=0, must-revalidate",
  });
  if (req.method === "HEAD") return res.end();
  createReadStream(filePath).pipe(res);
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

const API_ROUTES = {
  "/api/vacancies": handleVacancies,
  "/api/companies": handleCompanies,
  "/api/save": handleSave,
  "/api/statuses": handleStatuses,
  "/api/company-review": handleCompanyReview,
  "/api/company-statuses": handleCompanyStatuses,
  "/api/board-statuses": handleBoardStatuses,
  "/api/board-toggle": handleBoardToggle,
  "/api/health": handleHealth,
  "/api/health-detail": handleHealthDetail,
};

export async function handleRequest(req, res) {
  const pathname = new URL(req.url, "http://localhost").pathname;
  const route = API_ROUTES[pathname];
  try {
    if (route) {
      await route(req, res);
    } else if (pathname.startsWith("/api/")) {
      // Vercel answers 404 for a function that does not exist; bootstrap.js
      // relies on that for /api/vacancies in simple mode.
      sendJson(res, 404, { error: "Not found" });
    } else {
      await handleStatic(req, res, pathname);
    }
  } catch (err) {
    console.error(`unhandled error on ${pathname}:`, err);
    if (!res.headersSent) sendJson(res, 500, { error: "Internal error" });
    else res.end();
  }
}

// Listen only when run directly (node server.js) — importing this module in
// tests must not open a socket.
if (
  process.argv[1] &&
  import.meta.url === new URL(`file://${process.argv[1]}`).href
) {
  const port = Number(process.env.PORT) || 3000;
  const host = process.env.HOST || "127.0.0.1";
  if (!process.env.DATABASE_URL) {
    console.warn(
      "DATABASE_URL is not set — static files will serve, API routes will answer 500",
    );
  }
  createServer(handleRequest).listen(port, host, () => {
    console.log(`dashboard server listening on http://${host}:${port}`);
  });
}
