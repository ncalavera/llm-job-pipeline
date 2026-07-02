// U3 — /api/vacancies security contract.
//
// Covers the DB-free, security-critical paths (the ones CEO/eng review locked):
// fail-closed when auth is unconfigured, no wildcard CORS, no-store caching,
// method guard. The happy-path 200 needs a live Supabase and is covered by
// manual/integration verification, not this unit suite.

import { test } from "node:test";
import assert from "node:assert/strict";

import handler, { computeETag, isNotModified } from "./vacancies.js";

/** Minimal Vercel-style res double that records status, body, and headers. */
function fakeRes() {
  return {
    statusCode: null,
    body: undefined,
    headers: {},
    ended: false,
    setHeader(k, v) {
      this.headers[k.toLowerCase()] = v;
    },
    status(code) {
      this.statusCode = code;
      return this;
    },
    json(obj) {
      this.body = obj;
      return this;
    },
    end() {
      this.ended = true;
      return this;
    },
  };
}

function withEnv(env, fn) {
  const keys = [
    "AUTH_USER",
    "AUTH_PASS",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
  ];
  const saved = Object.fromEntries(keys.map((k) => [k, process.env[k]]));
  for (const k of keys) delete process.env[k];
  Object.assign(process.env, env);
  return Promise.resolve(fn()).finally(() => {
    for (const k of keys) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  });
}

test("fails closed with 503 when AUTH_USER/AUTH_PASS are unset", async () => {
  await withEnv(
    { SUPABASE_URL: "x", SUPABASE_SERVICE_ROLE_KEY: "y" },
    async () => {
      const res = fakeRes();
      await handler({ method: "GET" }, res);
      assert.equal(res.statusCode, 503);
      // No PII payload leaked — body is the refusal, not vacancy data.
      assert.deepEqual(res.body, { error: "Auth not configured" });
    },
  );
});

test("never sets a wildcard Access-Control-Allow-Origin (PII is same-origin only)", async () => {
  await withEnv({}, async () => {
    const res = fakeRes();
    await handler({ method: "GET" }, res);
    assert.notEqual(res.headers["access-control-allow-origin"], "*");
    assert.equal(res.headers["access-control-allow-origin"], undefined);
  });
});

test("sends Cache-Control: no-store so a refresh is never stale", async () => {
  await withEnv({}, async () => {
    const res = fakeRes();
    await handler({ method: "GET" }, res);
    assert.equal(res.headers["cache-control"], "no-store");
  });
});

test("rejects non-GET with 405", async () => {
  await withEnv({ AUTH_USER: "u", AUTH_PASS: "p" }, async () => {
    const res = fakeRes();
    await handler({ method: "POST" }, res);
    assert.equal(res.statusCode, 405);
  });
});

test("returns 500 (not a payload) when Supabase config is missing but auth is set", async () => {
  await withEnv({ AUTH_USER: "u", AUTH_PASS: "p" }, async () => {
    const res = fakeRes();
    await handler({ method: "GET" }, res);
    assert.equal(res.statusCode, 500);
    assert.deepEqual(res.body, { error: "Server misconfigured" });
  });
});

// Conditional-GET (polling) — pure decision logic. The 200/304 branch itself
// needs a live Supabase (queries dashboard_snapshot twice) and is covered by
// manual/integration verification like the rest of the happy path above; what
// IS unit-tested here is the version-token math the branch relies on.

test("computeETag: quotes the updated_at timestamp as an opaque token", () => {
  assert.equal(
    computeETag("2026-07-02T10:15:30.123456+00:00"),
    '"2026-07-02T10:15:30.123456+00:00"',
  );
});

test("computeETag: null/undefined/empty updated_at -> no ETag", () => {
  assert.equal(computeETag(null), null);
  assert.equal(computeETag(undefined), null);
  assert.equal(computeETag(""), null);
});

test("isNotModified: matching If-None-Match -> true (304, skip the payload query)", () => {
  assert.equal(isNotModified('"abc"', '"abc"'), true);
});

test("isNotModified: stale If-None-Match -> false (snapshot changed since)", () => {
  assert.equal(isNotModified('"old"', '"new"'), false);
});

test("isNotModified: missing If-None-Match (first poll) -> false", () => {
  assert.equal(isNotModified(undefined, '"abc"'), false);
  assert.equal(isNotModified(null, '"abc"'), false);
});

test("isNotModified: no etag available -> false (never claim unchanged with nothing to compare)", () => {
  assert.equal(isNotModified('"abc"', null), false);
});

test("isNotModified: weak validator prefix on either side still matches (edge compression must not kill 304s)", () => {
  assert.equal(isNotModified('W/"abc"', '"abc"'), true);
  assert.equal(isNotModified('"abc"', 'W/"abc"'), true);
  assert.equal(isNotModified('W/"old"', '"new"'), false);
});

test("isNotModified: comma-separated If-None-Match list matches any member", () => {
  assert.equal(isNotModified('"a", "b", "c"', '"b"'), true);
  assert.equal(isNotModified('"a", W/"b"', '"b"'), true);
  assert.equal(isNotModified('"a", "b"', '"c"'), false);
});

test("isNotModified: '*' matches any current etag", () => {
  assert.equal(isNotModified("*", '"abc"'), true);
});
