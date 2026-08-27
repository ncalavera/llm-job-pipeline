// Tests for server.js — the self-hosted dashboard server.
//
// The ETag helpers are duplicated from the retired Vercel handler, so they get
// the same test cases here: a drifted copy must fail loudly, not silently break
// the 304 poll path. Routing is exercised through handleRequest with mock
// req/res — no socket. The DB is a stub pool injected through setPool(), so the
// handlers run their real SQL-to-JSON shaping instead of stopping at the
// "Server misconfigured" gate (which is all they used to do here).

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, writeFileSync, chmodSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  computeETag,
  isNotModified,
  handleRequest,
  setPool,
  logError,
  isRecoverableError,
  VALID_STATUSES,
  DECISION_STATUSES,
  APPLICATION_STATUSES,
  REPORT_KINDS,
  reportExcerpt,
  reportSlugFromPath,
} from "./server.js";

const ROOT = fileURLToPath(new URL(".", import.meta.url));

test("computeETag wraps updated_at in quotes", () => {
  assert.equal(
    computeETag("2026-08-20 10:11:12+00"),
    '"2026-08-20 10:11:12+00"',
  );
  assert.equal(computeETag(null), null);
  assert.equal(computeETag(""), null);
});

test("isNotModified matches exact, weak, and list forms", () => {
  const etag = '"v1"';
  assert.equal(isNotModified('"v1"', etag), true);
  assert.equal(isNotModified('W/"v1"', etag), true); // proxy-weakened client tag
  assert.equal(isNotModified('"v0", "v1"', etag), true);
  assert.equal(isNotModified("*", etag), true);
  assert.equal(isNotModified('"v0"', etag), false);
  assert.equal(isNotModified(null, etag), false);
  assert.equal(isNotModified('"v1"', null), false);
});

// --- Mock req/res --------------------------------------------------------

function mockReq({ method = "GET", url = "/", body, headers = {} } = {}) {
  const listeners = {};
  return {
    method,
    url,
    headers,
    on(event, fn) {
      listeners[event] = fn;
      // Emit the whole body as soon as 'end' is registered.
      if (event === "end") {
        if (body !== undefined && listeners.data)
          listeners.data(Buffer.from(JSON.stringify(body)));
        fn();
      }
      return this;
    },
    destroy() {},
  };
}

function mockRes() {
  let resolveFinished;
  const res = {
    statusCode: null,
    headers: {},
    body: "",
    headersSent: false,
    destroyed: false,
  };
  res.finished = new Promise((resolve) => (resolveFinished = resolve));
  res.setHeader = (k, v) => {
    res.headers[k] = v;
  };
  res.writeHead = (status, headers) => {
    res.statusCode = status;
    Object.assign(res.headers, headers || {});
    res.headersSent = true;
    return res;
  };
  res.write = (data) => {
    if (data) res.body += data;
    return true;
  };
  res.end = (data) => {
    if (data) res.body += data;
    resolveFinished();
    return res;
  };
  res.on = () => res;
  res.once = () => res;
  res.emit = () => false;
  res.destroy = () => {
    res.destroyed = true;
    resolveFinished();
    return res;
  };
  return res;
}

async function call(opts) {
  const req = mockReq(opts);
  const res = mockRes();
  await handleRequest(req, res);
  return res;
}

// --- Stub pool ------------------------------------------------------------

// Match a handler's SQL by substring against a table of replies, so each test
// states only the rows it cares about. Whitespace is collapsed first — the SQL
// in server.js is multi-line.
function stubPool(replies) {
  const seen = [];
  setPool({
    async query(sql, params) {
      const flat = String(sql).replace(/\s+/g, " ").trim();
      seen.push({ sql: flat, params });
      for (const [needle, rows] of replies) {
        if (flat.includes(needle)) {
          if (rows instanceof Error) throw rows;
          return { rows, rowCount: rows.length };
        }
      }
      throw new Error(`stub pool: no reply for SQL: ${flat}`);
    },
  });
  return seen;
}

/** Run `fn` with DATABASE_URL set and a stub pool installed, then clean up. */
async function withStubDb(replies, fn) {
  const previous = process.env.DATABASE_URL;
  process.env.DATABASE_URL = "postgres://stub/stub";
  const seen = stubPool(replies);
  try {
    return await fn(seen);
  } finally {
    setPool(null);
    if (previous === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = previous;
  }
}

// --- Routing contracts (no DB configured) ---------------------------------

test("unknown /api/ path answers 404 JSON", async () => {
  delete process.env.DATABASE_URL;
  const res = await call({ url: "/api/nope" });
  assert.equal(res.statusCode, 404);
  assert.deepEqual(JSON.parse(res.body), { error: "Not found" });
});

test("wrapped endpoint: OPTIONS preflight answers 204 with CORS headers", async () => {
  delete process.env.DATABASE_URL;
  const res = await call({ method: "OPTIONS", url: "/api/save" });
  assert.equal(res.statusCode, 204);
  assert.equal(res.headers["Access-Control-Allow-Origin"], "*");
  assert.equal(res.headers["Access-Control-Allow-Methods"], "POST, OPTIONS");
});

test("wrapped endpoint: wrong method answers 405", async () => {
  delete process.env.DATABASE_URL;
  const res = await call({ method: "GET", url: "/api/save" });
  assert.equal(res.statusCode, 405);
  assert.deepEqual(JSON.parse(res.body), { error: "Method not allowed" });
});

test("without DATABASE_URL an API route fails gracefully as 500 misconfigured", async () => {
  delete process.env.DATABASE_URL;
  const res = await call({ url: "/api/statuses" });
  assert.equal(res.statusCode, 500);
  assert.deepEqual(JSON.parse(res.body), { error: "Server misconfigured" });
});

test("/api/vacancies is same-origin (no CORS header) and no-store", async () => {
  delete process.env.DATABASE_URL;
  const res = await call({ url: "/api/vacancies" });
  assert.equal(res.statusCode, 500); // misconfigured, but headers already set
  assert.equal(res.headers["Cache-Control"], "no-store");
  assert.equal(res.headers["Access-Control-Allow-Origin"], undefined);
});

test("/api/health answers 200 ok:false when the DB is not configured", async () => {
  delete process.env.DATABASE_URL;
  const res = await call({ url: "/api/health" });
  assert.equal(res.statusCode, 200);
  const body = JSON.parse(res.body);
  assert.equal(body.ok, false);
  assert.equal(body.backend, "postgres");
});

// --- Endpoints against a stub database ------------------------------------

test("/api/vacancies serves the snapshot payload with an ETag", async () => {
  await withStubDb(
    [
      ["to_json(updated_at)", [{ updated_at: "2026-08-20T09:53:29.68+04:00" }]],
      ["SELECT payload", [{ payload: { groups: [{ id: "v1" }] } }]],
    ],
    async () => {
      const res = await call({ url: "/api/vacancies" });
      assert.equal(res.statusCode, 200);
      assert.equal(res.headers.ETag, '"2026-08-20T09:53:29.68+04:00"');
      assert.deepEqual(JSON.parse(res.body), { groups: [{ id: "v1" }] });
    },
  );
});

test("/api/vacancies answers 304 when the client's ETag is current", async () => {
  await withStubDb(
    [["to_json(updated_at)", [{ updated_at: "v-current" }]]],
    async () => {
      const res = await call({
        url: "/api/vacancies",
        headers: { "if-none-match": 'W/"v-current"' },
      });
      assert.equal(res.statusCode, 304);
      assert.equal(res.body, "");
    },
  );
});

test("/api/vacancies answers 503 (not 404) before the first snapshot", async () => {
  // 404 is bootstrap.js's "endpoint absent → fall back to static data.js"
  // signal; answering it here would silently blank a full-mode dashboard.
  await withStubDb([["to_json(updated_at)", []]], async () => {
    const res = await call({ url: "/api/vacancies" });
    assert.equal(res.statusCode, 503);
    assert.deepEqual(JSON.parse(res.body), {
      error: "Snapshot not generated yet",
    });
  });
});

test("/api/companies shapes a company row and rolls up its live roles", async () => {
  await withStubDb(
    [
      [
        "FROM company",
        [
          {
            id: "c-1",
            canonical_name: "Example Org Inc.",
            status: "ACTIVE",
            tier: "A",
            alignment_score: "82",
            mission_fit: { alignment_label: "strong", dimensions: {} },
            about: { description: "Does things.", sector: "health" },
            website: "https://example.test",
            careers_url: null,
            offices: null,
            category: null,
            fetch_strategy: "greenhouse",
            fetch_status: "ok",
            last_fetched: null,
            notes: null,
            experience_match: 4,
            personal_interest: 3,
          },
        ],
      ],
      [
        "FROM vacancy WHERE status <> 'archived'",
        [
          { id: "v1", company_id: "c-1", status: "unseen" },
          { id: "v2", company_id: "c-1", status: "test_task" },
          { id: "v3", company_id: "c-1", status: "passed" },
        ],
      ],
    ],
    async () => {
      const res = await call({ url: "/api/companies" });
      assert.equal(res.statusCode, 200);
      const [c] = JSON.parse(res.body).companies;
      assert.equal(c.company_id, "c-1");
      assert.equal(c.slug, "example-org-inc");
      assert.equal(c.status, "active");
      assert.equal(c.review_status, "approved");
      assert.equal(c.alignment_score, 82);
      assert.equal(c.vacancy_count, 3);
      // "Selected" = touched and kept, so a take-home counts and a pass does not.
      assert.equal(c.liked_count, 1);
      assert.equal(c.new_count, 1);
      assert.equal(c.website, "https://example.test");
      assert.equal(c.careers_url, undefined); // absent, not ""
      assert.equal(c.fit_dimensions, undefined); // empty object collapses
    },
  );
});

test("/api/statuses returns the status and timestamp maps", async () => {
  await withStubDb(
    [
      [
        "SELECT id, status, status_updated_at FROM vacancy",
        [
          { id: "v1", status: "test_task", status_updated_at: "2026-08-20" },
          { id: "v2", status: "liked", status_updated_at: null },
        ],
      ],
    ],
    async () => {
      const res = await call({ url: "/api/statuses" });
      assert.equal(res.statusCode, 200);
      const body = JSON.parse(res.body);
      assert.deepEqual(body.statuses, { v1: "test_task", v2: "liked" });
      assert.deepEqual(body.timestamps, { v1: "2026-08-20" });
    },
  );
});

test("/api/company-statuses maps company status to a review verdict", async () => {
  await withStubDb(
    [
      [
        "SELECT id, status FROM company",
        [
          { id: "c1", status: "active" },
          { id: "c2", status: "candidate" },
          { id: "c3", status: "inactive" },
          { id: "c4", status: "something-else" },
        ],
      ],
    ],
    async () => {
      const res = await call({ url: "/api/company-statuses" });
      assert.equal(res.statusCode, 200);
      assert.deepEqual(JSON.parse(res.body).statuses, {
        c1: "approved",
        c2: "pending",
        c3: "rejected",
        c4: "pending",
      });
    },
  );
});

test("/api/board-statuses joins the catalog with its vacancy counts", async () => {
  const stale = new Date(Date.now() - 30 * 86400000).toISOString();
  await withStubDb(
    [
      [
        "SELECT id, name, strategy, tier, ttl_days",
        [
          {
            id: "b1",
            name: "Board One",
            strategy: "rss",
            tier: 1,
            ttl_days: 7,
            url: "https://board.test",
            last_fetched: stale,
            enabled: null,
            hidden: null,
          },
        ],
      ],
      [
        "GROUP BY source_board",
        [{ source_board: "Board One", total: 12, recent: 3 }],
      ],
    ],
    async () => {
      const res = await call({ url: "/api/board-statuses" });
      assert.equal(res.statusCode, 200);
      const [b] = JSON.parse(res.body).boards;
      assert.equal(b.enabled, true); // null normalises to enabled
      assert.equal(b.hidden, false);
      assert.equal(b.vac_total, 12);
      assert.equal(b.vac_recent, 3);
      assert.equal(b.overdue, true); // 30d old against a 7d ttl
    },
  );
});

test("/api/health-detail assembles all four blocks", async () => {
  await withStubDb(
    [
      [
        "SELECT id, name, last_fetched, enabled, hidden",
        [
          {
            id: "b1",
            name: "Healthy",
            last_fetched: "2026-08-20",
            enabled: true,
            hidden: false,
            last_success: "2026-08-20",
            consecutive_failures: 0,
          },
          {
            id: "b2",
            name: "Broken",
            last_fetched: "2026-08-20",
            enabled: true,
            hidden: false,
            last_success: null,
            consecutive_failures: 5,
          },
          {
            id: "b3",
            name: "Off",
            last_fetched: null,
            enabled: false,
            hidden: false,
            last_success: null,
            consecutive_failures: 0,
          },
        ],
      ],
      ["GROUP BY source_board", [{ source_board: "Healthy", total: 9 }]],
      [
        "WHERE status = 'active'",
        [
          {
            canonical_name: "Failing Org",
            fetch_status: "js_required",
            last_fetched: "2026-08-01",
            fetch_strategy: "scrape",
            consecutive_failures: 1,
            coverage: "direct",
          },
          {
            canonical_name: "Hand Checked",
            fetch_status: "ok",
            last_fetched: null,
            fetch_strategy: "manual_check",
            consecutive_failures: 0,
            coverage: "direct",
          },
        ],
      ],
      ["WHERE status = 'candidate'", [{ n: 4 }]],
      ["SELECT first_seen FROM vacancy", [{ first_seen: "2026-08-01" }]],
      ["WHERE kind = 'reviewed'", [{ created_at: "2026-08-10T00:00:00Z" }]],
      ["WHERE kind = 'applied'", [{ n: 2 }]],
      ["WHERE status = ANY($1)", [{ n: 7 }]],
      // Both remaining COUNT(*) queries over vacancy (unseen_scored).
      ["llm_score IS NOT NULL", [{ n: 11 }]],
    ],
    async () => {
      const res = await call({ url: "/api/health-detail" });
      assert.equal(res.statusCode, 200);
      assert.equal(res.headers["Cache-Control"], "no-store");
      const body = JSON.parse(res.body);
      // Disabled boards are dropped; broken ones sort first.
      assert.deepEqual(
        body.boards.map((b) => b.name),
        ["Broken", "Healthy"],
      );
      assert.equal(body.boards[0].presumed_broken, true);
      assert.deepEqual(
        body.companies.failing.map((c) => c.name),
        ["Failing Org"],
      );
      assert.deepEqual(
        body.companies.manual_check.map((c) => c.name),
        ["Hand Checked"],
      );
      assert.equal(body.waiting.candidates_pending, 4);
      assert.equal(body.learning.applied_since, 2);
      assert.equal(body.learning.verdicts_pending, 7);
    },
  );
});

test("/api/health-detail counts verdicts over the full decision vocabulary", async () => {
  // The regression this guards: DECISION_STATUSES lost test_task/interview and
  // the Health tab quietly reported fewer pending verdicts than the board had.
  await withStubDb(
    [
      ["SELECT id, name, last_fetched, enabled, hidden", []],
      ["GROUP BY source_board", []],
      ["WHERE status = 'active'", []],
      ["WHERE status = 'candidate'", [{ n: 0 }]],
      ["SELECT first_seen FROM vacancy", []],
      ["WHERE kind = 'reviewed'", []],
      ["WHERE kind = 'applied'", [{ n: 0 }]],
      ["WHERE status = ANY($1)", [{ n: 0 }]],
      ["llm_score IS NOT NULL", [{ n: 0 }]],
    ],
    async (seen) => {
      await call({ url: "/api/health-detail" });
      const verdictQuery = seen.find((q) => q.sql.includes("status = ANY($1)"));
      assert.deepEqual(verdictQuery.params[0], DECISION_STATUSES);
      assert.ok(verdictQuery.params[0].includes("test_task"));
      assert.ok(verdictQuery.params[0].includes("interview"));
    },
  );
});

test("a database failure answers 500 without leaking the SQL error", async () => {
  const pgError = Object.assign(new Error('column "nope" does not exist'), {
    code: "42703",
    table: "vacancy",
  });
  await withStubDb([["SELECT id, status", pgError]], async () => {
    const res = await call({ url: "/api/company-statuses" });
    assert.equal(res.statusCode, 500);
    assert.deepEqual(JSON.parse(res.body), { error: "Database error" });
  });
});

// --- /api/save ------------------------------------------------------------

test("/api/save rejects a missing field with 400", async () => {
  await withStubDb([], async () => {
    const res = await call({
      method: "POST",
      url: "/api/save",
      body: { id: "v1" },
    });
    assert.equal(res.statusCode, 400);
    assert.deepEqual(JSON.parse(res.body), { error: "Missing id or status" });
  });
});

test("/api/save rejects a status outside the vocabulary with 400", async () => {
  await withStubDb([], async () => {
    const res = await call({
      method: "POST",
      url: "/api/save",
      body: { id: "v1", status: "not_a_status" },
    });
    assert.equal(res.statusCode, 400);
    assert.deepEqual(JSON.parse(res.body), { error: "Invalid status" });
  });
});

test("/api/save answers 404 for an id no row matches", async () => {
  await withStubDb([["UPDATE vacancy", []]], async () => {
    const res = await call({
      method: "POST",
      url: "/api/save",
      body: { id: "missing", status: "liked" },
    });
    assert.equal(res.statusCode, 404);
    assert.deepEqual(JSON.parse(res.body), {
      error: "Vacancy not found",
      id: "missing",
    });
  });
});

test("/api/save writes every valid status and answers 200", async () => {
  await withStubDb([["UPDATE vacancy", [{ id: "v1" }]]], async (seen) => {
    for (const status of VALID_STATUSES) {
      const res = await call({
        method: "POST",
        url: "/api/save",
        body: { id: "v1", status },
      });
      assert.equal(res.statusCode, 200, `status ${status} was refused`);
      assert.equal(JSON.parse(res.body).ok, true);
    }
    assert.equal(seen.length, VALID_STATUSES.length);
    assert.equal(seen[0].params[0], VALID_STATUSES[0]);
  });
});

test("/api/save stamps applied_at once, and only for an application", () => {
  // status_updated_at moves with every stage, so on a declined row it holds
  // the date of the REJECTION. The Applications table's "Sent on" column reads
  // applied_at instead — which only exists if this write sets it, and is only
  // right if a later stage never overwrites it.
  return withStubDb([["UPDATE vacancy", [{ id: "v1" }]]], async (seen) => {
    for (const status of VALID_STATUSES) {
      await call({
        method: "POST",
        url: "/api/save",
        body: { id: "v1", status },
      });
    }
    const bySql = Object.fromEntries(
      VALID_STATUSES.map((s, i) => [s, seen[i].sql]),
    );
    for (const status of APPLICATION_STATUSES) {
      assert.match(
        bySql[status],
        /applied_at = COALESCE\(applied_at, \$2::timestamptz\)/,
        `status ${status} did not stamp applied_at`,
      );
    }
    for (const status of VALID_STATUSES.filter(
      (s) => !APPLICATION_STATUSES.includes(s),
    )) {
      assert.doesNotMatch(
        bySql[status],
        /applied_at/,
        `status ${status} is not an application but touched applied_at`,
      );
    }
  });
});

test("APPLICATION_STATUSES mirrors scripts/statuses.py", () => {
  // Two hand-maintained copies of one vocabulary. Read the Python source and
  // compare, so adding a funnel status on one side fails the build on the
  // other — a status missing here is a row the table shows with no send date.
  const py = readFileSync(join(ROOT, "scripts/statuses.py"), "utf8");
  const block = py.match(
    /^APPLICATION_STATUSES: frozenset\[str\] = frozenset\(([\s\S]*?)\n\)/m,
  );
  assert.ok(block, "APPLICATION_STATUSES not found in scripts/statuses.py");
  const pyStatuses = [...block[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual([...APPLICATION_STATUSES].sort(), pyStatuses.sort());
  // Every one of them must also be a status the save door accepts at all.
  for (const status of APPLICATION_STATUSES) {
    assert.ok(VALID_STATUSES.includes(status));
  }
});

test("VALID_STATUSES carries the whole board vocabulary", () => {
  // An array assertion, not a substring grep: a status mentioned only in a
  // comment used to satisfy the old check while the save still refused it.
  for (const status of ["test_task", "interview", "declined", "accepted"]) {
    assert.ok(
      VALID_STATUSES.includes(status),
      `VALID_STATUSES is missing ${status}`,
    );
  }
});

// --- Cross-language drift -------------------------------------------------

test("DECISION_STATUSES mirrors scripts/learning.py", () => {
  // Two hand-maintained copies of one vocabulary. Read the Python source and
  // compare, so adding a status on one side fails the build on the other.
  const py = readFileSync(join(ROOT, "scripts/learning.py"), "utf8");

  const basket = py.match(/^LIKED_BASKET = \(([\s\S]*?)\n\)/m);
  assert.ok(basket, "LIKED_BASKET not found in scripts/learning.py");
  const liked = [...basket[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  assert.ok(liked.length > 0);

  // The formula itself, so a change from `+ ("passed",)` also fails here.
  assert.ok(
    /^DECISION_STATUSES = LIKED_BASKET \+ \("passed",\)/m.test(py),
    "scripts/learning.py no longer builds DECISION_STATUSES as LIKED_BASKET + passed — update server.js and this test",
  );

  assert.deepEqual(
    [...DECISION_STATUSES].sort(),
    [...liked, "passed"].sort(),
    "server.js DECISION_STATUSES has drifted from scripts/learning.py",
  );
});

// --- Static files ---------------------------------------------------------

test("static: / serves public/index.html with the vercel.json cache header", async () => {
  // HEAD, not GET — the GET path pipes a file stream into res, which the
  // mock is not; headers and status are what this test is about.
  const res = await call({ method: "HEAD", url: "/" });
  await res.finished;
  assert.equal(res.statusCode, 200);
  assert.equal(
    res.headers["Cache-Control"],
    "public, max-age=0, must-revalidate",
  );
  assert.match(res.headers["Content-Type"], /text\/html/);
});

test("static: encoded path traversal is refused", async () => {
  const res = await call({ method: "HEAD", url: "/%2e%2e/package.json" });
  assert.equal(res.statusCode, 404);
});

test("static: a GET streams the file body", async () => {
  const res = await call({ method: "GET", url: "/index.html" });
  await res.finished;
  assert.equal(res.statusCode, 200);
  assert.ok(res.body.length > 0);
});

test(
  "static: a file that stats but cannot be opened answers 500, not a crash",
  {
    // Root ignores the mode bits, so the unreadable file would still open.
    skip:
      typeof process.getuid === "function" && process.getuid() === 0
        ? "running as root"
        : false,
  },
  async () => {
    // The bug: stat() succeeds, then open() fails (a deploy rsync swapped the
    // file, a mode change locked it), the stream emits 'error' with no listener,
    // and an unhandled EventEmitter 'error' takes the ENTIRE process down — every
    // other in-flight request with it. One unreadable file must cost one request.
    const name = `__unreadable-${process.pid}.txt`;
    const path = join(ROOT, "public", name);
    writeFileSync(path, "secret");
    chmodSync(path, 0o000);
    try {
      const res = await call({ method: "GET", url: `/${name}` });
      await res.finished;
      assert.equal(res.statusCode, 500);
      assert.equal(res.body, "");
    } finally {
      chmodSync(path, 0o600);
      rmSync(path, { force: true });
    }
  },
);

// --- Diagnostics ----------------------------------------------------------

test("logError prints the stack, the pg fields and the request context", () => {
  const lines = [];
  const original = console.error;
  console.error = (line) => lines.push(line);
  try {
    const err = Object.assign(new Error("relation does not exist"), {
      code: "42P01",
      table: "vacancy",
      constraint: "vacancy_pkey",
      detail: "no such table",
    });
    logError("save", err, { rid: "7", route: "/api/save", id: "v1" });
  } finally {
    console.error = original;
  }
  const line = lines.join("\n");
  assert.match(line, /^save: relation does not exist/);
  for (const fragment of [
    '"code":"42P01"',
    '"table":"vacancy"',
    '"constraint":"vacancy_pkey"',
    '"detail":"no such table"',
    '"rid":"7"',
    '"route":"/api/save"',
    '"id":"v1"',
  ]) {
    assert.ok(line.includes(fragment), `log line lacks ${fragment}`);
  }
  assert.match(line, /server\.test\.js/); // the stack came along
});

test("isRecoverableError keeps I/O noise alive and lets real bugs exit", () => {
  assert.equal(
    isRecoverableError(Object.assign(new Error("x"), { code: "EPIPE" })),
    true,
  );
  assert.equal(
    isRecoverableError(Object.assign(new Error("x"), { code: "ECONNRESET" })),
    true,
  );
  assert.equal(
    isRecoverableError(Object.assign(new Error("x"), { code: "EACCES" })),
    true,
  );
  assert.equal(isRecoverableError(new TypeError("x is not a function")), false);
  assert.equal(isRecoverableError(undefined), false);
});

// --- /api/reports ----------------------------------------------------------
//
// Research reports written for this search live as markdown files in a private
// repo, which means the work is only reachable from the laptop it was written
// on. These endpoints put them behind the dashboard instead. Identity is the
// slug, so a re-import of an edited file must land on the SAME row — a second
// copy of a report is worse than no copy, because now two disagree.

const REPORT_ROW = {
  slug: "ea-funding-2026",
  title: "EA Funding Landscape 2026",
  kind: "research",
  body_md: "# EA Funding Landscape 2026\n\nThree funders matter here.",
  source_path: "research/sectors/ea-funding-2026.md",
  created_at: "2026-08-01T09:00:00Z",
  updated_at: "2026-08-17T09:00:00Z",
};

test("GET /api/reports lists reports newest first, with an excerpt not a body", () => {
  // The list must stay one cheap response however long the library gets: a
  // hundred full reports would be megabytes, and the list shows none of it.
  return withStubDb([["FROM report ORDER BY updated_at DESC", [REPORT_ROW]]], async () => {
    const res = await call({ url: "/api/reports" });
    assert.equal(res.statusCode, 200);
    const { reports } = JSON.parse(res.body);
    assert.equal(reports.length, 1);
    assert.equal(reports[0].slug, "ea-funding-2026");
    assert.equal(reports[0].excerpt, "Three funders matter here.");
    assert.equal(reports[0].body_md, undefined, "the list leaked a full body");
  });
});

test("GET /api/reports/<slug> returns the full report", () => {
  return withStubDb([["FROM report WHERE slug = $1", [REPORT_ROW]]], async (seen) => {
    const res = await call({ url: "/api/reports/ea-funding-2026" });
    assert.equal(res.statusCode, 200);
    const { report } = JSON.parse(res.body);
    assert.equal(report.body_md, REPORT_ROW.body_md);
    assert.deepEqual(seen[0].params, ["ea-funding-2026"]);
  });
});

test("GET /api/reports/<slug> answers 404 for a report that is not there", () => {
  return withStubDb([["FROM report WHERE slug = $1", []]], async () => {
    const res = await call({ url: "/api/reports/nope" });
    assert.equal(res.statusCode, 404);
    assert.deepEqual(JSON.parse(res.body), {
      error: "Report not found",
      slug: "nope",
    });
  });
});

test("POST /api/reports upserts on the slug, so a re-import updates one row", () => {
  return withStubDb(
    [["INSERT INTO report", [{ slug: "ea-funding-2026", inserted: false }]]],
    async (seen) => {
      const res = await call({
        method: "POST",
        url: "/api/reports",
        body: {
          slug: "ea-funding-2026",
          title: "EA Funding Landscape 2026",
          kind: "research",
          body_md: "# EA Funding Landscape 2026\n\nRevised.",
          source_path: "research/sectors/ea-funding-2026.md",
        },
      });
      assert.equal(res.statusCode, 200);
      assert.deepEqual(JSON.parse(res.body), {
        ok: true,
        slug: "ea-funding-2026",
        created: false,
      });
      assert.match(seen[0].sql, /ON CONFLICT \(slug\) DO UPDATE/);
      // created_at must NOT be in the update list: the report was first
      // written when it was first written.
      const updateClause = seen[0].sql.split("DO UPDATE")[1];
      assert.ok(!updateClause.includes("created_at"));
      assert.ok(updateClause.includes("updated_at = NOW()"));
    },
  );
});

test("POST /api/reports reports whether the row was new", () => {
  return withStubDb(
    [["INSERT INTO report", [{ slug: "new-one", inserted: true }]]],
    async () => {
      const res = await call({
        method: "POST",
        url: "/api/reports",
        body: { slug: "new-one", title: "New", kind: "grant", body_md: "text" },
      });
      assert.equal(JSON.parse(res.body).created, true);
    },
  );
});

test("POST /api/reports defaults a missing kind to 'other', never to nothing", () => {
  return withStubDb(
    [["INSERT INTO report", [{ slug: "s", inserted: true }]]],
    async (seen) => {
      await call({
        method: "POST",
        url: "/api/reports",
        body: { slug: "s", title: "T", body_md: "b" },
      });
      assert.equal(seen[0].params[2], "other");
    },
  );
});

test("POST /api/reports refuses an unknown kind and a missing field", () => {
  return withStubDb([["INSERT INTO report", []]], async () => {
    const bad = await call({
      method: "POST",
      url: "/api/reports",
      body: { slug: "s", title: "T", body_md: "b", kind: "memo" },
    });
    assert.equal(bad.statusCode, 400);
    assert.deepEqual(JSON.parse(bad.body), { error: "Invalid kind" });

    for (const body of [
      { title: "T", body_md: "b" },
      { slug: "s", body_md: "b" },
      { slug: "s", title: "T" },
    ]) {
      const res = await call({ method: "POST", url: "/api/reports", body });
      assert.equal(res.statusCode, 400);
      assert.deepEqual(JSON.parse(res.body), {
        error: "Missing slug, title or body_md",
      });
    }
  });
});

test("/api/reports allows both methods on one path and refuses the rest", () => {
  return withStubDb([["FROM report", []]], async () => {
    const preflight = await call({ method: "OPTIONS", url: "/api/reports" });
    assert.equal(preflight.statusCode, 204);
    assert.equal(
      preflight.headers["Access-Control-Allow-Methods"],
      "GET, POST, OPTIONS",
    );

    const wrong = await call({ method: "DELETE", url: "/api/reports" });
    assert.equal(wrong.statusCode, 405);
  });
});

test("REPORT_KINDS mirrors scripts/statuses.py", () => {
  // Two hand-maintained copies of one vocabulary, plus the SQL CHECK. A kind
  // accepted on one side and refused on the other is a write that fails only
  // for some reports.
  const py = readFileSync(join(ROOT, "scripts/statuses.py"), "utf8");
  const block = py.match(/^REPORT_KINDS: tuple\[str, \.\.\.\] = \(([\s\S]*?)\n\)/m);
  assert.ok(block, "REPORT_KINDS not found in scripts/statuses.py");
  const pyKinds = [...block[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual([...REPORT_KINDS].sort(), pyKinds.sort());
});

// --- The excerpt -----------------------------------------------------------

test("the excerpt skips the report's own H1 and front matter", () => {
  // A raw slice would spend its first line repeating the title the card
  // already shows above it.
  const excerpt = reportExcerpt(
    "---\n# EA Funding Landscape 2026\n\nThree funders matter here.",
  );
  assert.equal(excerpt, "Three funders matter here.");
});

test("a long excerpt is cut at a word boundary, with an ellipsis", () => {
  const body = "# T\n\n" + "word ".repeat(200);
  const excerpt = reportExcerpt(body, 60);
  assert.ok(excerpt.length <= 61, `too long: ${excerpt.length}`);
  assert.ok(excerpt.endsWith("…"));
  assert.ok(!/wor…$/.test(excerpt), "cut mid-word");
});

test("a short report needs no ellipsis", () => {
  assert.equal(reportExcerpt("# T\n\nShort."), "Short.");
});

test("an empty or heading-only report yields an empty excerpt, not undefined", () => {
  assert.equal(reportExcerpt(""), "");
  assert.equal(reportExcerpt(null), "");
  assert.equal(reportExcerpt("# Only a heading"), "");
});

test("the excerpt collapses the newlines a markdown file is full of", () => {
  const excerpt = reportExcerpt("# T\n\nOne line.\nAnother    line.\n\nThird.");
  assert.equal(excerpt, "One line. Another line. Third.");
});

// --- Slug routing ----------------------------------------------------------

test("the detail route reads the slug out of the path", () => {
  assert.equal(reportSlugFromPath("/api/reports/ea-funding-2026"), "ea-funding-2026");
  assert.equal(reportSlugFromPath("/api/reports/a%20b"), "a b");
});

test("the detail route ignores paths that are not one report", () => {
  // /api/reports itself is the list, and a nested path is malformed — the CLI
  // only ever produces flat slugs. Neither may fall through to the detail
  // handler and query for a slug that cannot exist.
  assert.equal(reportSlugFromPath("/api/reports"), "");
  assert.equal(reportSlugFromPath("/api/reports/"), "");
  assert.equal(reportSlugFromPath("/api/reports/a/b"), "");
  assert.equal(reportSlugFromPath("/api/vacancies"), "");
});

// --- The excerpt must be prose, not a slice of raw file --------------------
//
// These reports open with an H1 and often follow it with a fenced ASCII
// diagram or a table. A raw slice of one of those is a row of box-drawing
// characters — it tells the reader nothing and looks like a rendering bug.

test("the excerpt skips a fenced diagram and finds the real prose", () => {
  const body = [
    "# The Do Good Industry",
    "",
    "## Segments",
    "",
    "```",
    "┌───────────────┐",
    "│  THE ECOSYSTEM │",
    "└───────────────┘",
    "```",
    "",
    "Where in the ecosystem? Grantmakers versus tech enablers.",
  ].join("\n");
  const excerpt = reportExcerpt(body);
  assert.equal(
    excerpt,
    "Where in the ecosystem? Grantmakers versus tech enablers.",
  );
  assert.ok(!excerpt.includes("─"));
  assert.ok(!excerpt.includes("```"));
});

test("the excerpt skips headings at any depth, not only the title", () => {
  const excerpt = reportExcerpt("# Title\n\n## Section\n\n### Deeper\n\nReal prose.");
  assert.equal(excerpt, "Real prose.");
});

test("the excerpt skips horizontal rules and table rows", () => {
  const body = "# T\n\n---\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\nThe sentence.";
  assert.equal(reportExcerpt(body), "The sentence.");
});

test("the excerpt strips inline markers that would show as literal characters", () => {
  // "**Date:** 2026-07-06" in a text node reads as asterisks, not as emphasis.
  const excerpt = reportExcerpt(
    "# T\n\n**Date:** 2026-07-06. See [the notes](https://example.org) and `run.py`.",
  );
  assert.equal(
    excerpt,
    "Date: 2026-07-06. See the notes and run.py.",
  );
  assert.ok(!excerpt.includes("*"));
  assert.ok(!excerpt.includes("`"));
  assert.ok(!excerpt.includes("https://"));
});

test("the excerpt drops list and quote markers but keeps the text", () => {
  assert.equal(reportExcerpt("# T\n\n- First point\n- Second point"), "First point Second point");
  assert.equal(reportExcerpt("# T\n\n1. Step one"), "Step one");
  assert.equal(reportExcerpt("# T\n\n> A quotation."), "A quotation.");
});

test("a report that is nothing but a diagram yields an empty excerpt", () => {
  // Empty is honest. A row of box-drawing characters is not.
  assert.equal(reportExcerpt("# T\n\n```\n┌──┐\n└──┘\n```"), "");
});
