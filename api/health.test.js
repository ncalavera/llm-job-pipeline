// /api/health security + shape contract.
//
// health touches the DB and used to hand out env-presence flags, the Supabase
// project prefix, and a row count on an unauthenticated, public deployment. It
// must now (1) fail closed when the Basic Auth gate is unconfigured, exactly
// like the PII readers, and (2) return only a minimal liveness payload.

import { test } from "node:test";
import assert from "node:assert/strict";

import handler from "./health.js";

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
      assert.deepEqual(res.body, { error: "Auth not configured" });
    },
  );
});

test("rejects non-GET with 405", async () => {
  await withEnv({ AUTH_USER: "u", AUTH_PASS: "p" }, async () => {
    const res = fakeRes();
    await handler({ method: "POST" }, res);
    assert.equal(res.statusCode, 405);
  });
});

test("returns only a minimal liveness payload — no env flags, prefix, or counts", async () => {
  // Auth set, Supabase deliberately unset: the DB probe is skipped, so this
  // exercises the response shape without a network call. The shape is identical
  // whether the backend is reachable or not.
  await withEnv({ AUTH_USER: "u", AUTH_PASS: "p" }, async () => {
    const res = fakeRes();
    await handler({ method: "GET" }, res);

    assert.equal(res.statusCode, 200);
    assert.equal(res.body.backend, "supabase");
    assert.equal(typeof res.body.ok, "boolean");
    assert.equal(typeof res.body.ts, "string");

    // The old leaky fields are gone.
    assert.equal("env" in res.body, false);
    assert.equal("supabase" in res.body, false);
    assert.equal("url_prefix" in res.body, false);
    assert.equal("vacancy_count" in res.body, false);
    assert.equal("warnings" in res.body, false);
  });
});
