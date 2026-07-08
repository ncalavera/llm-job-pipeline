// /api/health-detail security contract.
//
// The endpoint hands out pipeline status data (board/company failure detail,
// waiting counts, learning state), so it must fail closed exactly like the
// other Supabase-backed status endpoints: no Basic Auth configured → 503, and
// only GET is allowed. The four-block aggregate logic is validated separately
// against live Supabase (read-only) — see the U8 report — because it is pure
// Supabase query composition with no injectable seam here.

import { test } from "node:test";
import assert from "node:assert/strict";

import handler from "./health-detail.js";

function fakeRes() {
  return {
    statusCode: null,
    body: undefined,
    headers: {},
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

test("answers the OPTIONS preflight with 204", async () => {
  await withEnv({ AUTH_USER: "u", AUTH_PASS: "p" }, async () => {
    const res = fakeRes();
    await handler({ method: "OPTIONS" }, res);
    assert.equal(res.statusCode, 204);
  });
});
