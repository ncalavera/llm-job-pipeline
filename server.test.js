// Tests for server.js — the self-hosted dashboard server.
//
// The ETag helpers are duplicated from api/vacancies.js (which keeps its own
// copy for the Vercel deployment until cutover), so they get the same test
// cases here: a drifted copy must fail loudly, not silently break the 304
// poll path. Routing is exercised through handleRequest with mock req/res —
// no socket, no database (DATABASE_URL is cleared per test).

import { test } from "node:test";
import assert from "node:assert/strict";
import { computeETag, isNotModified, handleRequest } from "./server.js";

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

function mockReq({ method = "GET", url = "/", body } = {}) {
  const listeners = {};
  return {
    method,
    url,
    headers: {},
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
  res.end = (data) => {
    if (data) res.body += data;
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
