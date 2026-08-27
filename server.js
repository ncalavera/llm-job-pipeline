// server.js — the dashboard server.
//
// One plain-Node HTTP server: serves public/ statically and answers every
// /api/* endpoint against local Postgres (`pg` + DATABASE_URL). It replaced a
// Vercel deployment whose nine serverless handlers (api/*.js), vercel.json and
// middleware.js are now deleted — the contracts were ported unchanged, so
// nothing under public/ moved with them. See MIGRATION.md for the full
// contract map, the systemd unit and the Caddy site block (Caddy owns TLS +
// Basic Auth, which is why the old middleware gate has no successor here; the
// bind stays on 127.0.0.1).
//
// Assumes a fully migrated database (every sql/migrations/*.postgres.sql
// applied). The retired handlers' unknown-column fallbacks for
// partially-migrated DBs are deliberately not carried over.
//
// Starts fine without a database: static files serve, API routes answer
// 500 "Server misconfigured" (no DATABASE_URL) or "Database error"
// (unreachable DB) — the same errors the retired handlers gave.

import { createServer } from "node:http";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";

const PUBLIC_DIR = join(fileURLToPath(new URL(".", import.meta.url)), "public");

// ---------------------------------------------------------------------------
// Error logging
// ---------------------------------------------------------------------------
//
// One formatter for every failure this process can see. A bare `err.message`
// ("column x does not exist") names the symptom and hides everything needed to
// act on it, so each line carries: the stack, the pg error fields the driver
// attaches, the route + request id, and whatever parameters the handler was
// working with.

// The fields node-postgres copies off a Postgres ErrorResponse. `code` is the
// SQLSTATE, which is what turns "Database error" into a diagnosis.
const ERROR_FIELDS = [
  "code",
  "severity",
  "detail",
  "hint",
  "position",
  "where",
  "schema",
  "table",
  "column",
  "dataType",
  "constraint",
  "routine",
  "errno",
  "syscall",
  "path",
];

function formatError(err, extra) {
  const e = err || {};
  const meta = {};
  for (const field of ERROR_FIELDS) {
    if (e[field] != null) meta[field] = e[field];
  }
  for (const [k, v] of Object.entries(extra || {})) {
    if (v !== undefined) meta[k] = v;
  }
  const message = e.message || String(err);
  const context = Object.keys(meta).length ? ` ${JSON.stringify(meta)}` : "";
  const stack = e.stack ? `\n${e.stack}` : "";
  return `${message}${context}${stack}`;
}

/** Log a failure with full diagnostics. `extra` carries route/request/params. */
export function logError(label, err, extra) {
  console.error(`${label}: ${formatError(err, extra)}`);
}

/** Same diagnostics at warning level — a degraded path, not a failed request. */
export function logWarn(label, err, extra) {
  console.warn(`${label}: ${formatError(err, extra)}`);
}

/** Request identity for a log line. `req.id` is set by handleRequest. */
function reqMeta(req, extra) {
  return {
    rid: req && req.id,
    route: req && req.url,
    method: req && req.method,
    ...(extra || {}),
  };
}

// ---------------------------------------------------------------------------
// Postgres
// ---------------------------------------------------------------------------

// DATE columns (vacancy.first_seen / last_seen / deadline) come back as plain
// "YYYY-MM-DD" strings — the format PostgREST served — instead of local-midnight
// Date objects. COUNT(*) (int8) comes back as a number.
pg.types.setTypeParser(1082, (v) => v);
pg.types.setTypeParser(20, (v) => parseInt(v, 10));

let _pool = null;
let _injectedPool = null;

/** Test seam: run the handlers against a stub `{ query }` instead of a real
 * pool. Pass null to restore the real one. Never called by the server itself. */
export function setPool(pool) {
  _injectedPool = pool;
}

function getPool() {
  if (_injectedPool) return _injectedPool;
  if (!_pool) {
    _pool = new pg.Pool({
      connectionString: process.env.DATABASE_URL,
      max: 5,
    });
    // A dropped idle connection must not crash the process.
    _pool.on("error", (err) => logError("pg pool", err));
  }
  return _pool;
}

// ---------------------------------------------------------------------------
// Small response helpers (the subset of the old res API the handlers used)
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
 * so the handlers' own "Missing …" 400 checks fire, like the old parser. */
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
// ETag helpers — ported from the retired api/vacancies.js. This is now the only
// copy; the duplicate went with the Vercel path.
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
// Handler preambles (ports of the retired same-origin gate and the shared
// withHandler wrapper — minus the Vercel-specific AUTH_USER / AUTH_PASS
// fail-closed check, which Caddy + the loopback bind replace).
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
    logError(label, new Error("missing DATABASE_URL"), reqMeta(req));
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
    logError(label, new Error("missing DATABASE_URL"), reqMeta(req));
    sendJson(res, 500, { error: "Server misconfigured" });
    return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// GET /api/vacancies — contract in MIGRATION.md
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
    logError("vacancies", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/companies — contract in MIGRATION.md
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
    logError("companies", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// POST /api/save — contract in MIGRATION.md
// ---------------------------------------------------------------------------

// The status vocabulary this door accepts. Hand-maintained twin of
// scripts/database_supabase.py VALID_STATUSES and dashboard_local.py — a status
// missing here is a board column whose save the server refuses.
export const VALID_STATUSES = [
  "unseen",
  "liked",
  "passed",
  "to_apply",
  "to_research",
  "to_network",
  "skipped",
  "applied",
  "test_task",
  "interview",
  "declined",
  "accepted",
  "expiring",
  "archived",
];

// The statuses that mean an application was actually sent. Twin of
// scripts/statuses.py APPLICATION_STATUSES; a status missing here is a row the
// Applications table shows with no send date.
export const APPLICATION_STATUSES = [
  "applied",
  "test_task",
  "interview",
  "declined",
  "accepted",
];

async function handleSave(req, res) {
  if (wrappedPreamble(req, res, "POST", "save")) return;
  const { id, status } = await readJsonBody(req);
  if (!id || !status)
    return sendJson(res, 400, { error: "Missing id or status" });
  if (!VALID_STATUSES.includes(status))
    return sendJson(res, 400, { error: "Invalid status" });

  // status_updated_at moves with every stage, so it can never answer "when did
  // I send this" — on a declined row it holds the date of the rejection.
  // applied_at answers that, and only the FIRST write into the funnel may set
  // it: COALESCE keeps the original send date through every later stage.
  // Mirrors _write_status in scripts/database_supabase.py.
  const stampApplied = APPLICATION_STATUSES.includes(status);
  const sql = stampApplied
    ? `UPDATE vacancy SET status = $1, status_updated_at = $2,
              applied_at = COALESCE(applied_at, $2::timestamptz)
        WHERE id = $3::uuid RETURNING id`
    : `UPDATE vacancy SET status = $1, status_updated_at = $2
        WHERE id = $3::uuid RETURNING id`;

  try {
    const result = await getPool().query(sql, [
      status,
      new Date().toISOString(),
      id,
    ]);
    if (result.rowCount === 0) {
      console.warn(`save: vacancy not found — id=${id} status=${status}`);
      return sendJson(res, 404, { error: "Vacancy not found", id });
    }
    return sendJson(res, 200, { ok: true, ts: new Date().toISOString() });
  } catch (err) {
    logError("save", err, reqMeta(req, { id, status }));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/statuses — contract in MIGRATION.md
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
    logError("statuses", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// POST /api/company-review — contract in MIGRATION.md
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
    logError("company-review", err, reqMeta(req, { company_id, action }));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/company-statuses — contract in MIGRATION.md
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
    logError("company-statuses", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/board-statuses — contract in MIGRATION.md
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
    logError("board-statuses", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// POST /api/board-toggle — contract in MIGRATION.md
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
    logError("board-toggle", err, reqMeta(req, { board_id, enabled }));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/health — contract in MIGRATION.md
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
      logError("health: backend probe failed", err, reqMeta(req));
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
// GET /api/health-detail — contract in MIGRATION.md
// ---------------------------------------------------------------------------

// Triage decisions that count as a verdict — the liked basket plus an explicit
// pass. Mirrors scripts/learning.py DECISION_STATUSES (= LIKED_BASKET +
// ("passed",)); server.test.js reads that file and fails on drift, because a
// status missing here silently undercounts verdicts_pending on the Health tab.
export const DECISION_STATUSES = [
  "liked",
  "to_apply",
  "to_research",
  "to_network",
  "applied",
  "test_task",
  "interview",
  "accepted",
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
    logWarn("health-detail: learning block unavailable", err);
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
    logError("health-detail", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// Static files — public/ at the site root, with the cache headers the
// retired vercel.json set.
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

  const headers = {
    "Content-Type": MIME[extname(filePath)] || "application/octet-stream",
    "Content-Length": info.size,
    // The retired vercel.json set this for /, /index.html and *.js|css.
    "Cache-Control": "public, max-age=0, must-revalidate",
  };
  if (req.method === "HEAD") {
    res.writeHead(200, headers);
    return res.end();
  }

  // stat() and open() are separate syscalls, so everything can change between
  // them: a deploy rsync replaces the file, a mode change makes it unreadable.
  // An unhandled 'error' on the stream is an unhandled 'error' on an
  // EventEmitter, which takes the whole process down — one bad file would end
  // every in-flight request. Headers therefore wait for 'open': until the fd
  // exists the response is still free to become a 404/500.
  await new Promise((done) => {
    const stream = createReadStream(filePath);
    let opened = false;
    stream.on("error", (err) => {
      logError("static", err, reqMeta(req, { file: filePath, opened }));
      if (!opened && !res.headersSent) {
        sendEmpty(res, err.code === "ENOENT" ? 404 : 500);
      } else {
        // Content-Length was already promised and cannot be met — cutting the
        // socket is the only way the client learns the body is incomplete.
        res.destroy(err);
      }
      done();
    });
    stream.on("open", () => {
      opened = true;
      res.writeHead(200, headers);
      stream.pipe(res);
    });
    stream.on("close", done);
  });
}

// ---------------------------------------------------------------------------
// /api/reports — contract in MIGRATION.md
// ---------------------------------------------------------------------------

// What kind of reading a stored report is. Twin of statuses.REPORT_KINDS and
// the SQL CHECK on report.kind; an unrecognised kind would silently create a
// group of one in the list, which reads as a broken grouping, not a typo.
export const REPORT_KINDS = [
  "research",
  "grant",
  "company",
  "sector",
  "other",
];

// How much of a report the list view carries. Enough to tell two reports apart
// at a glance, small enough that a hundred of them are still one cheap
// response — the full body is one click away at /api/reports/<slug>.
export const REPORT_EXCERPT_CHARS = 200;

/**
 * A plain-text preview of a report's opening prose.
 *
 * Not a raw slice of the file. These documents open with their own H1, and the
 * first thing under it is often a fenced ASCII diagram or a table — a raw slice
 * of one of those is a row of box-drawing characters, which tells the reader
 * nothing and looks like a rendering bug. So the scan skips everything that is
 * not prose (headings at any depth, fenced code, horizontal rules, table rows,
 * front matter), strips the inline markers that would otherwise show as literal
 * asterisks and backticks, collapses whitespace, and cuts on a word boundary.
 */
export function reportExcerpt(bodyMd, limit = REPORT_EXCERPT_CHARS) {
  if (!bodyMd) return "";
  const kept = [];
  let inCode = false;
  let seenProse = false;

  for (const raw of String(bodyMd).split("\n")) {
    const line = raw.trim();

    // A fence toggles; everything between them is a diagram or a snippet, and
    // neither is a summary of the report.
    if (line.startsWith("```")) {
      inCode = !inCode;
      continue;
    }
    if (inCode) continue;

    if (!line) continue;
    if (line.startsWith("#")) continue; // any heading, not just the title
    if (/^([-*_])\1{2,}$/.test(line)) continue; // horizontal rule / front matter fence
    if (line === "---") continue;
    if (line.startsWith("|")) continue; // a table is not prose either

    kept.push(stripInlineMarkdown(line));
    seenProse = true;
    if (kept.join(" ").length > limit + 40) break;
  }
  if (!seenProse) return "";

  const flat = kept.join(" ").replace(/\s+/g, " ").trim();
  if (flat.length <= limit) return flat;
  const cut = flat.slice(0, limit);
  const lastSpace = cut.lastIndexOf(" ");
  return (lastSpace > limit * 0.6 ? cut.slice(0, lastSpace) : cut).trim() + "\u2026";
}

/** Drop the markers that only mean something once rendered. The excerpt lands
 *  in a text node, so a literal "**Date:**" there is noise, not emphasis. */
function stripInlineMarkdown(line) {
  return line
    .replace(/^\s*[-*+]\s+/, "") // bullet marker
    .replace(/^\s*\d+[.)]\s+/, "") // number marker
    .replace(/^\s*>\s?/, "") // blockquote marker
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1") // link/image -> its label
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/(?<!\w)[*_]([^*_]+)[*_](?!\w)/g, "$1")
    .trim();
}

// /api/reports answers GET (list) and POST (upsert) on ONE path, which is the
// only route here that does. wrappedPreamble hard-codes a single allowed
// method — bending it to take a list would touch every other endpoint's
// preamble for one case — so this route carries its own, with the same CORS
// shape, the same 405, and the same missing-DATABASE_URL 500.
function reportsPreamble(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

  if (req.method === "OPTIONS") {
    sendEmpty(res, 204);
    return true;
  }
  if (req.method !== "GET" && req.method !== "POST") {
    sendJson(res, 405, { error: "Method not allowed" });
    return true;
  }
  if (!process.env.DATABASE_URL) {
    logError("reports", new Error("missing DATABASE_URL"), reqMeta(req));
    sendJson(res, 500, { error: "Server misconfigured" });
    return true;
  }
  return false;
}

async function handleReports(req, res) {
  if (reportsPreamble(req, res)) return;
  return req.method === "POST"
    ? handleReportUpsert(req, res)
    : handleReportsList(req, res);
}

async function handleReportsList(req, res) {
  try {
    const { rows } = await getPool().query(
      `SELECT slug, title, kind, body_md, source_path, created_at, updated_at
         FROM report ORDER BY updated_at DESC`,
    );
    const reports = rows.map((r) => ({
      slug: r.slug,
      title: r.title,
      kind: r.kind,
      source_path: r.source_path || "",
      created_at: r.created_at,
      updated_at: r.updated_at,
      excerpt: reportExcerpt(r.body_md),
    }));
    return sendJson(res, 200, { reports });
  } catch (err) {
    logError("reports", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

async function handleReportDetail(req, res, slug) {
  if (wrappedPreamble(req, res, "GET", "report")) return;
  try {
    const { rows } = await getPool().query(
      `SELECT slug, title, kind, body_md, source_path, created_at, updated_at
         FROM report WHERE slug = $1`,
      [slug],
    );
    if (!rows.length) {
      return sendJson(res, 404, { error: "Report not found", slug });
    }
    return sendJson(res, 200, { report: rows[0] });
  } catch (err) {
    logError("report", err, reqMeta(req, { slug }));
    return sendJson(res, 500, { error: "Database error" });
  }
}

async function handleReportUpsert(req, res) {
  const { slug, title, kind, body_md, source_path } = await readJsonBody(req);
  if (!slug || !title || !body_md) {
    return sendJson(res, 400, { error: "Missing slug, title or body_md" });
  }
  const reportKind = kind || "other";
  if (!REPORT_KINDS.includes(reportKind)) {
    return sendJson(res, 400, { error: "Invalid kind" });
  }

  try {
    // Upsert on the slug: re-importing an edited file must land on the same
    // row, not fork a second copy of the report. created_at is left alone —
    // the report was first written when it was first written — while
    // updated_at moves, because it is what the list sorts by.
    const { rows } = await getPool().query(
      `INSERT INTO report (slug, title, kind, body_md, source_path)
            VALUES ($1, $2, $3, $4, $5)
       ON CONFLICT (slug) DO UPDATE
              SET title = EXCLUDED.title,
                  kind = EXCLUDED.kind,
                  body_md = EXCLUDED.body_md,
                  source_path = EXCLUDED.source_path,
                  updated_at = NOW()
        RETURNING slug, (xmax = 0) AS inserted`,
      [slug, title, reportKind, body_md, source_path || null],
    );
    return sendJson(res, 200, {
      ok: true,
      slug: rows[0].slug,
      created: rows[0].inserted === true,
    });
  } catch (err) {
    logError("report-upsert", err, reqMeta(req, { slug, kind: reportKind }));
    return sendJson(res, 500, { error: "Database error" });
  }
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
  "/api/reports": handleReports,
};

// One report by slug: /api/reports/<slug>. The only path-parameter route on
// this server, so it is matched explicitly rather than by adding a pattern
// router for a single case.
const REPORT_DETAIL_PREFIX = "/api/reports/";

/** The slug in /api/reports/<slug>, or "" when the path is not that shape.
 *  A slug with a slash in it is rejected rather than joined back together —
 *  the CLI only ever produces flat slugs, so a nested path is malformed. */
export function reportSlugFromPath(pathname) {
  if (!pathname.startsWith(REPORT_DETAIL_PREFIX)) return "";
  const rest = decodeURIComponent(pathname.slice(REPORT_DETAIL_PREFIX.length));
  return rest && !rest.includes("/") ? rest : "";
}

let _reqSeq = 0;

export async function handleRequest(req, res) {
  // Short per-process request id so a log line ties back to one request.
  req.id = (++_reqSeq).toString(36);
  const pathname = new URL(req.url, "http://localhost").pathname;
  const route = API_ROUTES[pathname];
  const reportSlug = route ? "" : reportSlugFromPath(pathname);
  try {
    if (route) {
      await route(req, res);
    } else if (reportSlug) {
      await handleReportDetail(req, res, reportSlug);
    } else if (pathname.startsWith("/api/")) {
      // 404 for an endpoint that does not exist — bootstrap.js relies on it
      // for /api/vacancies in simple mode (it means "fall back to data.js").
      sendJson(res, 404, { error: "Not found" });
    } else {
      await handleStatic(req, res, pathname);
    }
  } catch (err) {
    logError("unhandled", err, reqMeta(req, { pathname }));
    if (!res.headersSent) sendJson(res, 500, { error: "Internal error" });
    else res.end();
  }
}

// ---------------------------------------------------------------------------
// Process-level safety net
// ---------------------------------------------------------------------------

// I/O the process cannot control: a client that hung up, a file that vanished
// or turned unreadable, a socket the kernel reset. These reach 'uncaughtException'
// only because some emitter had no local listener; the process state itself is
// fine, so it logs and keeps serving. Anything else (a real bug — a TypeError,
// an assertion) leaves memory in an unknown state, and Node's own advice is to
// exit and let systemd restart.
const RECOVERABLE_CODES = new Set([
  "EPIPE",
  "ECONNRESET",
  "ECONNABORTED",
  "ETIMEDOUT",
  "EACCES",
  "EPERM",
  "ENOENT",
  "EISDIR",
  "EBADF",
  "EMFILE",
  "ENFILE",
  "ERR_STREAM_PREMATURE_CLOSE",
  "ERR_STREAM_DESTROYED",
  "ERR_STREAM_WRITE_AFTER_END",
  "ERR_HTTP_HEADERS_SENT",
]);

/** True when the failure is transport/filesystem noise, not corrupted state. */
export function isRecoverableError(err) {
  return !!(err && err.code && RECOVERABLE_CODES.has(err.code));
}

/** Install the last-resort handlers. Called only when the server actually runs;
 * importing this module in tests must not swallow their failures. */
export function installProcessGuards() {
  process.on("uncaughtException", (err, origin) => {
    logError("uncaughtException", err, { origin, pid: process.pid });
    if (!isRecoverableError(err)) {
      logError("fatal — exiting", err, { origin, pid: process.pid });
      process.exit(1);
    }
  });
  process.on("unhandledRejection", (reason) => {
    const err = reason instanceof Error ? reason : new Error(String(reason));
    logError("unhandledRejection", err, { pid: process.pid });
    if (!isRecoverableError(err)) {
      logError("fatal — exiting", err, { pid: process.pid });
      process.exit(1);
    }
  });
}

// Listen only when run directly (node server.js) — importing this module in
// tests must not open a socket.
if (
  process.argv[1] &&
  import.meta.url === new URL(`file://${process.argv[1]}`).href
) {
  installProcessGuards();
  const port = Number(process.env.PORT) || 3000;
  const host = process.env.HOST || "127.0.0.1";
  if (!process.env.DATABASE_URL) {
    console.warn(
      "DATABASE_URL is not set — static files will serve, API routes will answer 500",
    );
  }
  const server = createServer(handleRequest);
  // A socket that dies mid-response emits here, not on the response object.
  server.on("clientError", (err, socket) => {
    logError("clientError", err);
    if (socket.writable) socket.end("HTTP/1.1 400 Bad Request\r\n\r\n");
  });
  server.listen(port, host, () => {
    console.log(`dashboard server listening on http://${host}:${port}`);
  });
}
