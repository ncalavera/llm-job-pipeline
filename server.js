// server.js — the dashboard server.
//
// One plain-Node HTTP server: serves public/ statically and answers every
// /api/* endpoint against local Postgres (`pg` + DATABASE_URL). It is the only
// server: nothing under public/ needs a build step. See DASHBOARD.md for the full
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
import { stat, readFile } from "node:fs/promises";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";
import { brotliCompress, constants as zlibConstants } from "node:zlib";
import { promisify } from "node:util";
import { compactSnapshot, compactRecord, DETAIL_FIELDS } from "./public/modules/payload.js";
const compressBrotli = promisify(brotliCompress);

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
// ETag helpers — the only copy; public/ polls against them for 304s.
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
// Handler preambles: the same-origin gate and the shared withHandler wrapper.
// Authentication is Caddy's job; the loopback bind keeps this unreachable
// except through it.
// ---------------------------------------------------------------------------

/** Same-origin PII readers (/api/vacancies, /api/companies): no CORS header,
 * no-store. Returns true when the preamble already answered. */
function piiPreamble(req, res, label, method = "GET") {
  res.setHeader("Cache-Control", "no-store");
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
// GET /api/vacancies — contract in DASHBOARD.md
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

    const compact = new URL(req.url, "http://localhost").searchParams.get("view") === "inbox";
    const etag = compact ? "W/" + computeETag(meta.rows[0].updated_at + ":inbox-v1") : computeETag(meta.rows[0].updated_at);
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
    const payload = compact ? compactSnapshot(data.rows[0].payload) : data.rows[0].payload;
    res.setHeader('Vary', 'Accept-Encoding');
    const acceptsBr = (req.headers['accept-encoding'] || '').split(',').some(value => {
      const match = value.trim().match(/^br(?:\s*;\s*q=(0(?:\.\d+)?|1(?:\.0+)?))?$/i);
      return match && (match[1] === undefined || Number(match[1]) > 0);
    });
    if (compact && acceptsBr) {
      const body = await compressBrotli(JSON.stringify(payload), {params: {[zlibConstants.BROTLI_PARAM_QUALITY]: 4}});
      res.writeHead(200, {'Content-Type':'application/json; charset=utf-8', 'Content-Encoding':'br', 'Content-Length':body.length});
      return res.end(body);
    }
    return sendJson(res, 200, payload);
  } catch (err) {
    logError("vacancies", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// Load one record's long text only when its detail page opens.
async function handleSnapshotDetail(req, res) {
  if (piiPreamble(req, res, "snapshot-detail")) return;
  const params = new URL(req.url, "http://localhost").searchParams;
  const kind = params.get("kind"), id = params.get("id");
  const section = {vacancy: "groups", archive: "archived_groups", company: "companies"}[kind];
  if (!Object.hasOwn(DETAIL_FIELDS, kind) || !id || id.length > 100)
    return sendJson(res, 400, {error: "Invalid record"});
  try {
    const {rows} = await getPool().query(
      `SELECT item FROM dashboard_snapshot,
       LATERAL jsonb_array_elements(payload->$1) AS item
       WHERE dashboard_snapshot.id = 'current' AND item->>$2 = $3 LIMIT 1`,
      [section, kind === "company" ? "company_id" : "id", id]);
    if (!rows.length) return sendJson(res, 404, {error: "Record not found"});
    return sendJson(res, 200, Object.fromEntries(DETAIL_FIELDS[kind].map(k => [k, rows[0].item[k] ?? null])));
  } catch (error) {
    logError("snapshot-detail", error, reqMeta(req));
    return sendJson(res, 500, {error: "Details unavailable"});
  }
}

// ---------------------------------------------------------------------------
// GET /api/companies — contract in DASHBOARD.md
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
      `SELECT id, canonical_name, status, status_reason, tier, alignment_score, mission_fit,
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
        status_reason: c.status_reason || "",
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

    const compact = new URL(req.url, "http://localhost").searchParams.get("view") === "inbox";
    return sendJson(res, 200, { companies: compact ? companies.map(c => compactRecord(c, "company")) : companies });
  } catch (err) {
    logError("companies", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// POST /api/save — contract in DASHBOARD.md
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
  "unsure",
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

// Feedback is an append-only account of a decision, not a preference change.
const FEEDBACK_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// PostgreSQL's row version changes for every writer, including agent/CLI writes.
// Compare opaque tokens, never timestamps rounded by JavaScript.
const SCREENING_STATUSES = ["unseen", "liked", "passed", "skipped", "unsure", "expiring"];
async function handleScreeningDecision(req, res) {
  if (piiPreamble(req, res, "screening-decision", "POST")) return;
  if (!/^application\/json(?:;|$)/i.test(req.headers["content-type"] || ""))
    return sendJson(res, 415, { error: "JSON required" });
  const { changes, operation_id } = await readJsonBody(req);
  if (typeof operation_id !== "string" || !FEEDBACK_UUID.test(operation_id))
    return sendJson(res, 400, { error: "A valid operation_id UUID is required" });
  if (!Array.isArray(changes) || !changes.length || changes.length > 100 ||
      new Set(changes.map((c) => c?.id)).size !== changes.length ||
      changes.some((c) => !c || !FEEDBACK_UUID.test(c.id) ||
        !SCREENING_STATUSES.includes(c.status) || !SCREENING_STATUSES.includes(c.expected_status) ||
        typeof c.expected_revision !== "string" || !c.expected_revision))
    return sendJson(res, 400, { error: "Invalid changes or missing revision" });
  let client;
  try {
    client = await getPool().connect();
    await client.query("BEGIN");
    const request = JSON.stringify(changes.map(({ id, status, expected_status, expected_revision }) =>
      ({ id, status, expected_status, expected_revision })).sort((a, b) => a.id.localeCompare(b.id)));
    await client.query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", [operation_id.toLowerCase()]);
    const receipt = await client.query(
      "SELECT result, request = $2::jsonb AS same_request FROM screening_decision WHERE operation_id = $1::uuid",
      [operation_id, request]);
    if (receipt.rows.length) {
      await client.query("COMMIT");
      return receipt.rows[0].same_request
        ? sendJson(res, 200, receipt.rows[0].result)
        : sendJson(res, 409, { error: "operation_id already used for different changes" });
    }
    const { rows } = await client.query(
      `SELECT id, status, xmin::text AS revision FROM vacancy
       WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE`, [changes.map((c) => c.id)]);
    const current = new Map(rows.map((r) => [r.id, r]));
    const conflicts = changes.filter((c) => {
      const r = current.get(c.id);
      return !r || r.status !== c.expected_status || r.revision !== c.expected_revision;
    }).map((c) => c.id);
    if (conflicts.length) {
      await client.query("ROLLBACK");
      return sendJson(res, 409, { error: "Vacancies changed; refresh and try again", conflicts, rows });
    }
    const saved = [];
    for (const c of changes) {
      const result = await client.query(
        `UPDATE vacancy SET status = $2, status_updated_at = clock_timestamp(),
         applied_at = CASE WHEN $3 THEN COALESCE(applied_at, clock_timestamp()) ELSE applied_at END
         WHERE id = $1::uuid RETURNING id, status, xmin::text AS revision, status_updated_at`,
        [c.id, c.status, APPLICATION_STATUSES.includes(c.status)]);
      saved.push({ ...result.rows[0], previous: current.get(c.id).status });
    }
    const result = { ok: true, rows: saved };
    await client.query(
      "INSERT INTO screening_decision (operation_id, request, result) VALUES ($1::uuid, $2::jsonb, $3::jsonb)",
      [operation_id, request, JSON.stringify(result)]);
    await client.query("COMMIT");
    return sendJson(res, 200, result);
  } catch (err) {
    if (client) await client.query("ROLLBACK").catch(() => {});
    logError("screening-decision", err, reqMeta(req));
    return sendJson(res, 500, { error: "Could not save decisions" });
  } finally { client?.release(); }
}

async function handleScreeningFeedback(req, res) {
  if (
    piiPreamble(
      req,
      res,
      "screening-feedback",
      req.method === "GET" ? "GET" : "POST",
    )
  )
    return;
  // A foreign site must not submit a simple form request using cached login.
  if (
    req.method === "POST" &&
    String(req.headers["content-type"] || "")
      .split(";")[0]
      .trim()
      .toLowerCase() !== "application/json"
  ) {
    return sendJson(res, 415, { error: "Expected application/json" });
  }
  try {
    if (req.method === "GET") {
      const { rows } = await getPool().query(
        "SELECT * FROM screening_feedback ORDER BY created_at DESC, id DESC LIMIT 100",
      );
      return sendJson(res, 200, { items: rows });
    }
    const body = await readJsonBody(req);
    const { id, vacancy_ids, decision, reason, group_label } = body || {};
    if (
      typeof id !== "string" ||
      !FEEDBACK_UUID.test(id) ||
      !Array.isArray(vacancy_ids) ||
      vacancy_ids.length < 1 ||
      vacancy_ids.length > 100 ||
      vacancy_ids.some(
        (v) => typeof v !== "string" || !FEEDBACK_UUID.test(v),
      ) ||
      !["liked", "passed"].includes(decision) ||
      typeof reason !== "string" ||
      !reason.trim() ||
      reason.length > 4000 ||
      typeof group_label !== "string" ||
      group_label.length > 200
    ) {
      return sendJson(res, 400, { error: "Invalid screening feedback" });
    }
    const ids = vacancy_ids.map((v) => v.toLowerCase());
    if (new Set(ids).size !== ids.length) {
      return sendJson(res, 400, { error: "Duplicate vacancy IDs" });
    }
    // A retry cannot change the original payload. Check existing IDs at insert
    // time, but preserve historical feedback even if a vacancy is later removed.
    await getPool().query(
      `INSERT INTO screening_feedback (id, vacancy_ids, decision, reason, group_label)
       SELECT $1::uuid, $2::jsonb, $3, $4, $5
       WHERE (SELECT count(*) FROM vacancy WHERE id = ANY($6::uuid[])) = $7
       ON CONFLICT (id) DO NOTHING`,
      [id, JSON.stringify(ids), decision, reason, group_label, ids, ids.length],
    );
    const { rows } = await getPool().query(
      "SELECT * FROM screening_feedback WHERE id = $1::uuid",
      [id],
    );
    const item = rows[0];
    if (!item) return sendJson(res, 404, { error: "Vacancy not found" });
    if (
      JSON.stringify(item.vacancy_ids) !== JSON.stringify(ids) ||
      item.decision !== decision ||
      item.reason !== reason ||
      item.group_label !== group_label
    ) {
      return sendJson(res, 409, {
        error: "Feedback ID already used for a different decision",
      });
    }
    return sendJson(res, 200, { ok: true, item });
  } catch (err) {
    // Do not include reasons or database error details: those can contain private text.
    console.error("screening-feedback: database request failed", {
      code: err.code,
    });
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/statuses — contract in DASHBOARD.md
// ---------------------------------------------------------------------------

async function handleStatuses(req, res) {
  if (wrappedPreamble(req, res, "GET", "statuses")) return;
  try {
    const { rows } = await getPool().query(
      `SELECT id, status, status_updated_at, xmin::text AS revision FROM vacancy`,
    );
    const statuses = {};
    const timestamps = {};
    const revisions = {};
    for (const row of rows) {
      statuses[row.id] = row.status;
      revisions[row.id] = row.revision;
      if (row.status_updated_at) {
        timestamps[row.id] = row.status_updated_at;
      }
    }
    return sendJson(res, 200, { statuses, timestamps, revisions });
  } catch (err) {
    logError("statuses", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// POST /api/company-review — contract in DASHBOARD.md
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

  const newStatus = action === "approve" ? "candidate" : "inactive";
  const reason =
    action === "approve" ? "approved via dashboard" : "rejected via dashboard";

  try {
    const result = await getPool().query(
      `UPDATE company SET status = CASE
          WHEN $1 = 'candidate' AND status = 'active' THEN status ELSE $1 END,
          status_reason = $2
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
// GET /api/company-statuses — contract in DASHBOARD.md
// ---------------------------------------------------------------------------

async function handleCompanyStatuses(req, res) {
  if (wrappedPreamble(req, res, "GET", "company-statuses")) return;
  try {
    const { rows } = await getPool().query("SELECT id, status, status_reason FROM company");
    const statuses = {};
    const reasons = {};
    for (const row of rows) {
      reasons[row.id] = row.status_reason || "";
      statuses[row.id] = REVIEW_MAP[row.status] || "pending";
    }
    return sendJson(res, 200, { statuses, reasons });
  } catch (err) {
    logError("company-statuses", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

// ---------------------------------------------------------------------------
// GET /api/board-statuses — contract in DASHBOARD.md
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

async function handleSourceObservations(req, res) {
  if (piiPreamble(req, res, "source-observations")) return;
  const query = new URL(req.url, "http://localhost").searchParams;
  const source = query.get("source"), run = query.get("run");
  const offset = Number(query.get("offset") || 0);
  if (!source || !run || source.length > 200 || run.length > 200 || !Number.isSafeInteger(offset) || offset < 0) {
    return sendJson(res, 400, { error: "Invalid source run" });
  }
  try {
    const { rows } = await getPool().query(
      `SELECT external_id, title, organization, listing_url, outcome, reason, canonical_id
       FROM source_observation WHERE source_key = $1 AND run_id = $2
       ORDER BY external_id LIMIT 251 OFFSET $3`, [source, run, offset]);
    sendJson(res, 200, { items: rows.slice(0,250), next: rows.length > 250 ? offset + 250 : null });
  } catch (error) {
    logError("source-observations", error, reqMeta(req));
    sendJson(res, 500, { error: "Source accounting unavailable" });
  }
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
// POST /api/board-toggle — contract in DASHBOARD.md
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
// GET /api/health — contract in DASHBOARD.md
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
// GET /api/health-detail — contract in DASHBOARD.md
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
// Static files — public/ at the site root, revalidated on every request.
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
// /api/reports — contract in DASHBOARD.md
// ---------------------------------------------------------------------------

// What kind of reading a stored report is. Twin of statuses.REPORT_KINDS and
// the SQL CHECK on report.kind; an unrecognised kind would silently create a
// group of one in the list, which reads as a broken grouping, not a typo.
export const REPORT_KINDS = ["research", "grant", "company", "sector", "other"];

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
  return (
    (lastSpace > limit * 0.6 ? cut.slice(0, lastSpace) : cut).trim() + "\u2026"
  );
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

// ---------------------------------------------------------------------------
// /api/contacts — the Networking tab
//
// GET   lists every contact, newest activity first.
// POST  upserts one contact on (name, group) — the importer's identity rule.
// PATCH moves one contact to a new status and stamps when it moved.
//
// Three methods on one path rather than a second endpoint for the status
// change: the row IS the resource, and a PATCH that carries {id, status} says
// what it does more plainly than a /api/contact-status would.
// ---------------------------------------------------------------------------

// Twin of statuses.py CONTACT_STATUSES and the SQL CHECK on contact.status.
// A status missing here is one the dashboard can never set.
export const CONTACT_STATUSES = [
  "planned",
  "contacted",
  "replied",
  "met",
  "declined",
  "stale",
];

// Twin of statuses.py CONTACT_CHANNELS. Anything outside this set is dropped
// on write, so an unknown key can never reach the UI as a channel it has no
// way to render.
export const CONTACT_CHANNELS = [
  "ea_forum",
  "linkedin",
  "telegram",
  "x",
  "github",
  "site",
  "email",
  "calendly",
];

/** Keep only the channels the UI knows how to draw, dropping empty values. */
export function cleanChannels(raw) {
  const out = {};
  if (!raw || typeof raw !== "object") return out;
  for (const key of CONTACT_CHANNELS) {
    const value = raw[key];
    if (typeof value === "string" && value.trim()) out[key] = value.trim();
  }
  return out;
}

/** A group as stored: lowercase, spaces and underscores to hyphens. Twin of
 *  contacts.normalise_group, so the API and the importer agree on identity. */
export function normaliseGroup(value) {
  const text = String(value == null ? "" : value)
    .trim()
    .toLowerCase()
    .replace(/[_\s]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
  return text || "other";
}

function contactsPreamble(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

  if (req.method === "OPTIONS") {
    sendEmpty(res, 204);
    return true;
  }
  if (!["GET", "POST", "PATCH"].includes(req.method)) {
    sendJson(res, 405, { error: "Method not allowed" });
    return true;
  }
  if (!process.env.DATABASE_URL) {
    logError("contacts", new Error("missing DATABASE_URL"), reqMeta(req));
    sendJson(res, 500, { error: "Server misconfigured" });
    return true;
  }
  return false;
}

async function handleContacts(req, res) {
  if (contactsPreamble(req, res)) return;
  if (req.method === "POST") return handleContactUpsert(req, res);
  if (req.method === "PATCH") return handleContactStatus(req, res);
  return handleContactsList(req, res);
}

async function handleContactsList(req, res) {
  try {
    const { rows } = await getPool().query(
      `SELECT id, name, name_local, city, org, role, why_matters, channels,
              "group", status, status_at, last_active, opener, notes,
              source_path, created_at, updated_at
         FROM contact
        ORDER BY status_at DESC NULLS LAST, name ASC`,
    );
    const contacts = rows.map((r) => ({
      id: String(r.id),
      name: r.name,
      name_local: r.name_local || "",
      city: r.city || "",
      org: r.org || "",
      role: r.role || "",
      why_matters: r.why_matters || "",
      channels: cleanChannels(r.channels),
      group: r.group,
      status: r.status,
      status_at: r.status_at,
      last_active: r.last_active || "",
      opener: r.opener || "",
      notes: r.notes || "",
      source_path: r.source_path || "",
      updated_at: r.updated_at,
    }));
    return sendJson(res, 200, { contacts });
  } catch (err) {
    logError("contacts", err, reqMeta(req));
    return sendJson(res, 500, { error: "Database error" });
  }
}

async function handleContactUpsert(req, res) {
  const body = await readJsonBody(req);
  const name = String(body.name || "").trim();
  if (!name) return sendJson(res, 400, { error: "Missing name" });

  const group = normaliseGroup(body.group);
  const status = body.status || "planned";
  if (!CONTACT_STATUSES.includes(status)) {
    return sendJson(res, 400, { error: "Invalid status" });
  }

  try {
    const { rows } = await getPool().query(
      `INSERT INTO contact (name, name_local, city, org, role, why_matters,
                            channels, "group", status, last_active, opener,
                            notes, source_path)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
       ON CONFLICT (name, "group") DO UPDATE SET
         name_local = EXCLUDED.name_local,
         city = EXCLUDED.city,
         org = EXCLUDED.org,
         role = EXCLUDED.role,
         why_matters = EXCLUDED.why_matters,
         channels = EXCLUDED.channels,
         status = EXCLUDED.status,
         last_active = EXCLUDED.last_active,
         opener = EXCLUDED.opener,
         notes = EXCLUDED.notes,
         source_path = EXCLUDED.source_path,
         updated_at = now()
       RETURNING id`,
      [
        name,
        body.name_local || "",
        body.city || "",
        body.org || "",
        body.role || "",
        body.why_matters || "",
        JSON.stringify(cleanChannels(body.channels)),
        group,
        status,
        body.last_active || "",
        body.opener || "",
        body.notes || "",
        body.source_path || "",
      ],
    );
    return sendJson(res, 200, { ok: true, id: String(rows[0].id) });
  } catch (err) {
    logError("contacts", err, reqMeta(req, { name, group }));
    return sendJson(res, 500, { error: "Database error" });
  }
}

async function handleContactStatus(req, res) {
  const body = await readJsonBody(req);
  const id = String(body.id || "").trim();
  const status = String(body.status || "").trim();
  if (!id) return sendJson(res, 400, { error: "Missing id" });
  if (!CONTACT_STATUSES.includes(status)) {
    return sendJson(res, 400, { error: "Invalid status" });
  }

  try {
    const { rowCount } = await getPool().query(
      "UPDATE contact SET status = $1, status_at = now(), updated_at = now() WHERE id = $2",
      [status, id],
    );
    if (!rowCount)
      return sendJson(res, 404, { error: "Contact not found", id });
    return sendJson(res, 200, { ok: true, id, status });
  } catch (err) {
    logError("contacts", err, reqMeta(req, { id, status }));
    return sendJson(res, 500, { error: "Database error" });
  }
}

const API_ROUTES = {
  "/api/application-notes": handleApplicationNotes,
  "/api/materials": handleMaterials,
  "/api/vacancies": handleVacancies,
  "/api/snapshot-detail": handleSnapshotDetail,
  "/api/companies": handleCompanies,
  "/api/save": handleSave,
  "/api/screening-decision": handleScreeningDecision,
  "/api/screening-feedback": handleScreeningFeedback,
  "/api/statuses": handleStatuses,
  "/api/company-review": handleCompanyReview,
  "/api/company-statuses": handleCompanyStatuses,
  "/api/board-statuses": handleBoardStatuses,
  "/api/source-observations": handleSourceObservations,
  "/api/board-toggle": handleBoardToggle,
  "/api/health": handleHealth,
  "/api/health-detail": handleHealthDetail,
  "/api/reports": handleReports,
  "/api/contacts": handleContacts,
};

// Reuse the existing private application dossier. Vacancy status remains the
// dashboard's progress state; notes may describe any employer-specific steps.
export async function handleApplicationNotes(req, res) {
  if (piiPreamble(req, res, "application-notes", req.method === "GET" ? "GET" : "POST")) return;
  const body = req.method === "GET"
    ? Object.fromEntries(new URL(req.url, "http://localhost").searchParams)
    : await readJsonBody(req);
  if (!body || typeof body.id !== "string" || !FEEDBACK_UUID.test(body.id)) return sendJson(res, 400, { error: "Invalid vacancy ID" });
  if (req.method === "POST" && (!/^application\/json(?:;|$)/i.test(req.headers["content-type"] || "") ||
      typeof body.notes !== "string" || typeof body.expected_notes !== "string" ||
      body.notes.length > 50000 || body.expected_notes.length > 50000))
    return sendJson(res, 400, { error: "Notes and their previous value are required (maximum 50,000 characters)" });
  let client;
  try {
    client = await getPool().connect();
    await client.query("BEGIN");
    const vacancy = await client.query("SELECT company_id, status, applied_at FROM vacancy WHERE id = $1::uuid FOR UPDATE", [body.id]);
    if (!vacancy.rows.length) {
      await client.query("ROLLBACK");
      return sendJson(res, 404, { error: "Vacancy not found" });
    }
    const existing = await client.query("SELECT notes FROM application WHERE vacancy_id = $1::uuid FOR UPDATE", [body.id]);
    const notes = existing.rows[0]?.notes || "";
    if (req.method === "GET") {
      await client.query("COMMIT");
      const events = await client.query("SELECT previous_status, status, recorded_at FROM vacancy_status_event WHERE vacancy_id = $1::uuid ORDER BY id", [body.id]);
      return sendJson(res, 200, { notes, events: events.rows });
    }
    if (notes !== body.expected_notes) {
      await client.query("ROLLBACK");
      return sendJson(res, 409, { error: "Notes changed elsewhere. Copy your draft, then reload to compare before saving." });
    }
    if (body.notes === notes) {
      await client.query("COMMIT");
      return sendJson(res, 200, { notes });
    }
    if (existing.rows.length) {
      await client.query(`UPDATE application SET notes = $2, updated_at = now(),
        artifacts = jsonb_set(COALESCE(artifacts, '{}'::jsonb), '{note_history}',
          COALESCE(artifacts->'note_history', '[]'::jsonb) || jsonb_build_array(jsonb_build_object('recorded_at', now(), 'notes', notes)))
        WHERE vacancy_id = $1::uuid`, [body.id, body.notes]);
    } else {
      const v = vacancy.rows[0];
      const status = {applied: "applied", test_task: "interview", interview: "interview", accepted: "offer", declined: "rejected"}[v.status] || "draft";
      await client.query(`INSERT INTO application (vacancy_id, company_id, status, applied_at, notes)
        VALUES ($1::uuid, $2::uuid, $3, $4, $5)`, [body.id, v.company_id, status, v.applied_at, body.notes]);
    }
    await client.query("COMMIT");
    return sendJson(res, 200, { notes: body.notes });
  } catch (err) {
    if (client) await client.query("ROLLBACK").catch(() => {});
    console.error("application-notes: database request failed", {code: err.code});
    return sendJson(res, 500, { error: "Could not save or load application notes" });
  } finally { client?.release(); }
}

// Private files stay outside public/. Caddy supplies the dashboard's auth;
// this endpoint follows the existing no-CORS, no-store PII boundary.
export async function handleMaterials(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "GET") return sendJson(res, 405, { error: "Method not allowed" });
  const root = join(process.env.JOBSEARCH_PRIVATE_DIR || join(fileURLToPath(new URL(".", import.meta.url)), "private"), "materials");
  const params = new URL(req.url, "http://localhost").searchParams;
  const statements = params.get("view") === "statements";
  let rows;
  try { rows = JSON.parse(await readFile(join(root, statements ? "statements.json" : "index.json"), "utf8")); }
  catch (err) {
    if (err.code === "ENOENT") return sendJson(res, 200, []);
    throw err;
  }
  if (statements) return sendJson(res, 200, rows);
  const id = params.get("id");
  if (!id) return sendJson(res, 200, rows);
  const row = rows.find((r) => r.id === id);
  if (!row || !/^[a-f0-9]{64}$/.test(row.sha256)) return sendJson(res, 404, { error: "Not found" });
  const data = await readFile(join(root, "objects", row.sha256));
  res.writeHead(200, {
    "Content-Type": "application/octet-stream",
    "Content-Disposition": "attachment; filename*=UTF-8''" + encodeURIComponent(row.filename),
    "X-Content-Type-Options": "nosniff",
    "Content-Length": data.length,
  });
  res.end(data);
}

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
