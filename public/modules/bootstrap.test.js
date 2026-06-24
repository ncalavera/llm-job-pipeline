// U4 — bootstrap source-selection logic. The DOM/fetch parts of boot() need a
// browser; the decision that matters (live vs static fallback vs hard error) is
// a pure function, unit-tested here.

import { test } from "node:test";
import assert from "node:assert/strict";

import { resolveSource } from "./bootstrap.js";

test("200 OK → live payload from the endpoint", () => {
  assert.equal(resolveSource({ ok: true, status: 200 }), "live");
});

test("404 → fall back to static data.js (endpoint absent = simple/local mode)", () => {
  assert.equal(resolveSource({ ok: false, status: 404 }), "fallback");
});

test("401 → reauth (reload to re-trigger the login prompt), never a stale fallback", () => {
  assert.equal(resolveSource({ ok: false, status: 401 }), "reauth");
});

test("503 (snapshot not generated / auth not configured) → error", () => {
  assert.equal(resolveSource({ ok: false, status: 503 }), "error");
});

test("500 → error", () => {
  assert.equal(resolveSource({ ok: false, status: 500 }), "error");
});
