// U3 — middleware Basic Auth gate. This is the access control that protects
// /api/vacancies (and the whole dashboard). The endpoint relies on it, so we
// lock down the unauthenticated-request behavior here.

import { test } from "node:test";
import assert from "node:assert/strict";

import middleware from "../middleware.js";

/** A Web-Request-like double: only the bits middleware reads. */
function fakeRequest({ cookie = null, authorization = null } = {}) {
  const map = { cookie, authorization };
  return {
    url: "https://dashboard.example/",
    headers: { get: (name) => map[name.toLowerCase()] ?? null },
  };
}

function withAuthEnv(env, fn) {
  const saved = {
    AUTH_USER: process.env.AUTH_USER,
    AUTH_PASS: process.env.AUTH_PASS,
  };
  delete process.env.AUTH_USER;
  delete process.env.AUTH_PASS;
  Object.assign(process.env, env);
  return Promise.resolve(fn()).finally(() => {
    for (const k of ["AUTH_USER", "AUTH_PASS"]) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  });
}

test("challenges an unauthenticated request with 401 when auth is enabled", async () => {
  await withAuthEnv({ AUTH_USER: "me", AUTH_PASS: "secret" }, async () => {
    const resp = await middleware(fakeRequest());
    assert.ok(
      resp instanceof Response,
      "must return a Response, not pass through",
    );
    assert.equal(resp.status, 401);
    assert.match(resp.headers.get("www-authenticate") || "", /Basic/);
  });
});

test("rejects wrong Basic credentials with 401", async () => {
  await withAuthEnv({ AUTH_USER: "me", AUTH_PASS: "secret" }, async () => {
    const bad = "Basic " + Buffer.from("me:wrong").toString("base64");
    const resp = await middleware(fakeRequest({ authorization: bad }));
    assert.ok(resp instanceof Response);
    assert.equal(resp.status, 401);
  });
});

test("passes through (no Response) when auth is not configured — documents the opt-in gap the endpoint guards", async () => {
  await withAuthEnv({}, async () => {
    const resp = await middleware(fakeRequest());
    assert.equal(resp, undefined);
  });
});
