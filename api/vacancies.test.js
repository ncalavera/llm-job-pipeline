// U3 — /api/vacancies security contract.
//
// Covers the DB-free, security-critical paths (the ones CEO/eng review locked):
// fail-closed when auth is unconfigured, no wildcard CORS, no-store caching,
// method guard. The happy-path 200 needs a live Supabase and is covered by
// manual/integration verification, not this unit suite.

import { test } from "node:test";
import assert from "node:assert/strict";

import handler from "./vacancies.js";

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
