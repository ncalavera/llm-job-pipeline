// Contract for the shared withHandler() wrapper that every Supabase-backed
// dashboard endpoint (save, statuses, company-review, board-statuses,
// company-statuses) is built on. Locks the CORS/preflight/method/env preamble
// so a change there can't silently alter every endpoint at once.

import { test } from "node:test";
import assert from "node:assert/strict";

import { withHandler } from "./_handler.js";

/** Minimal Vercel-style res double that records status, body, headers, end. */
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

const CONFIGURED = { SUPABASE_URL: "x", SUPABASE_SERVICE_ROLE_KEY: "y" };

test("OPTIONS preflight short-circuits with 204 and no body, business logic never runs", async () => {
  await withEnv({}, async () => {
    let ran = false;
    const handler = withHandler({ method: "POST", label: "t" }, async () => {
      ran = true;
    });
    const res = fakeRes();
    await handler({ method: "OPTIONS" }, res);
    assert.equal(res.statusCode, 204);
    assert.equal(res.ended, true);
    assert.equal(ran, false);
  });
});

test("sets permissive CORS headers keyed to the declared method (POST)", async () => {
  await withEnv(CONFIGURED, async () => {
    const handler = withHandler({ method: "POST", label: "t" }, async () => {});
    const res = fakeRes();
    await handler({ method: "OPTIONS" }, res);
    assert.equal(res.headers["access-control-allow-origin"], "*");
    assert.equal(res.headers["access-control-allow-methods"], "POST, OPTIONS");
    assert.equal(
      res.headers["access-control-allow-headers"],
      "Content-Type, Authorization",
    );
  });
});

test("GET endpoints omit Content-Type from allowed headers", async () => {
  await withEnv(CONFIGURED, async () => {
    const handler = withHandler({ method: "GET", label: "t" }, async () => {});
    const res = fakeRes();
    await handler({ method: "OPTIONS" }, res);
    assert.equal(res.headers["access-control-allow-methods"], "GET, OPTIONS");
    assert.equal(res.headers["access-control-allow-headers"], "Authorization");
  });
});

test("rejects a mismatched method with 405 before touching business logic", async () => {
  await withEnv(CONFIGURED, async () => {
    let ran = false;
    const handler = withHandler({ method: "POST", label: "t" }, async () => {
      ran = true;
    });
    const res = fakeRes();
    await handler({ method: "GET" }, res);
    assert.equal(res.statusCode, 405);
    assert.deepEqual(res.body, { error: "Method not allowed" });
    assert.equal(ran, false);
  });
});

test("returns 500 (not the payload) when Supabase env is missing", async () => {
  await withEnv({}, async () => {
    let ran = false;
    const handler = withHandler({ method: "GET", label: "t" }, async () => {
      ran = true;
    });
    const res = fakeRes();
    await handler({ method: "GET" }, res);
    assert.equal(res.statusCode, 500);
    assert.deepEqual(res.body, { error: "Server misconfigured" });
    assert.equal(ran, false);
  });
});

test("delegates to the business logic when method and env are satisfied", async () => {
  await withEnv(CONFIGURED, async () => {
    const handler = withHandler(
      { method: "GET", label: "t" },
      async (req, res) => res.status(200).json({ ok: true }),
    );
    const res = fakeRes();
    await handler({ method: "GET" }, res);
    assert.equal(res.statusCode, 200);
    assert.deepEqual(res.body, { ok: true });
  });
});
