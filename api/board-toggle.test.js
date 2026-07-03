// /api/board-toggle — the write endpoint behind the Boards section's toggle.
//
// Covers the DB-free contract: the method guard (via withHandler) and the
// payload validation that runs BEFORE any Supabase call — a missing/mistyped
// board_id, and the type-check on `enabled` that lets `false` through while
// rejecting non-booleans (the bug a `!enabled` guard would introduce). The
// 200 / 404 / 500 branches need a live Supabase and are covered end-to-end on
// SQLite by tests/test_dashboard_local.py's local twin.

import { test } from "node:test";
import assert from "node:assert/strict";

import handler from "./board-toggle.js";

/** Minimal Vercel-style res double that records status, body, headers. */
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
  const keys = ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"];
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

// Env present so the withHandler preamble passes and we reach the validation
// (which returns before ever calling getSupabase).
const CONFIGURED = {
  SUPABASE_URL: "x",
  SUPABASE_SERVICE_ROLE_KEY: "y",
  AUTH_USER: "u",
  AUTH_PASS: "p",
};

test("fails closed with 503 when AUTH_USER/AUTH_PASS are unset", async () => {
  await withEnv(
    { SUPABASE_URL: "x", SUPABASE_SERVICE_ROLE_KEY: "y" },
    async () => {
      const res = fakeRes();
      await handler(
        { method: "POST", body: { board_id: "80k_hours", enabled: true } },
        res,
      );
      assert.equal(res.statusCode, 503);
    },
  );
});

test("rejects a non-POST method with 405 before any business logic", async () => {
  await withEnv(CONFIGURED, async () => {
    const res = fakeRes();
    await handler({ method: "GET", body: {} }, res);
    assert.equal(res.statusCode, 405);
  });
});

test("400 when board_id is missing", async () => {
  await withEnv(CONFIGURED, async () => {
    const res = fakeRes();
    await handler({ method: "POST", body: { enabled: true } }, res);
    assert.equal(res.statusCode, 400);
    assert.match(res.body.error, /board_id/);
  });
});

test("400 when board_id is not a string", async () => {
  await withEnv(CONFIGURED, async () => {
    const res = fakeRes();
    await handler(
      { method: "POST", body: { board_id: 123, enabled: true } },
      res,
    );
    assert.equal(res.statusCode, 400);
    assert.match(res.body.error, /board_id/);
  });
});

test("400 when board_id is blank", async () => {
  await withEnv(CONFIGURED, async () => {
    const res = fakeRes();
    await handler(
      { method: "POST", body: { board_id: "   ", enabled: true } },
      res,
    );
    assert.equal(res.statusCode, 400);
    assert.match(res.body.error, /board_id/);
  });
});

test("400 when enabled is missing", async () => {
  await withEnv(CONFIGURED, async () => {
    const res = fakeRes();
    await handler({ method: "POST", body: { board_id: "80k_hours" } }, res);
    assert.equal(res.statusCode, 400);
    assert.match(res.body.error, /enabled/);
  });
});

test("400 when enabled is a non-boolean (string) — not silently coerced", async () => {
  await withEnv(CONFIGURED, async () => {
    const res = fakeRes();
    await handler(
      { method: "POST", body: { board_id: "80k_hours", enabled: "true" } },
      res,
    );
    assert.equal(res.statusCode, 400);
    assert.match(res.body.error, /enabled/);
  });
});

test("400 when enabled is a number — the trap a `!enabled` guard would miss", async () => {
  await withEnv(CONFIGURED, async () => {
    const res = fakeRes();
    await handler(
      { method: "POST", body: { board_id: "80k_hours", enabled: 1 } },
      res,
    );
    assert.equal(res.statusCode, 400);
    assert.match(res.body.error, /enabled/);
  });
});
