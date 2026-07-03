// U4 — bootstrap source-selection logic. The DOM/fetch parts of boot() need a
// browser; the decision that matters (live vs static fallback vs hard error) is
// a pure function, unit-tested here.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  resolveSource,
  shouldApplyPollResponse,
  pollOutcome,
} from "./bootstrap.js";

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

// U-poll — startPolling()'s response-decision logic. The fetch/DOM/timer
// parts of startPolling() need a browser; this pure function is what decides
// whether a poll response is applied, unit-tested without one.

test("poll: 200 → apply (snapshot changed)", () => {
  assert.equal(shouldApplyPollResponse(200), true);
});

test("poll: 304 → skip (snapshot unchanged, no body to parse)", () => {
  assert.equal(shouldApplyPollResponse(304), false);
});

test("poll: 401/500/503 → skip (a background poll failure never disrupts the open dashboard)", () => {
  assert.equal(shouldApplyPollResponse(401), false);
  assert.equal(shouldApplyPollResponse(500), false);
  assert.equal(shouldApplyPollResponse(503), false);
});

// U3 — pollOutcome() feeds the sidebar sync-status footer (nav.js's state
// machine). Unlike shouldApplyPollResponse, it must tell 304 (fine, just
// unchanged) apart from a real error status (a hard failure worth surfacing).

test("pollOutcome: 200 → ok", () => {
  assert.equal(pollOutcome(200), "ok");
});

test("pollOutcome: 304 → ok (unchanged, but confirms the endpoint is live)", () => {
  assert.equal(pollOutcome(304), "ok");
});

test("pollOutcome: 401/500/503 → hard_fail (a real error, not a silent no-op)", () => {
  assert.equal(pollOutcome(401), "hard_fail");
  assert.equal(pollOutcome(500), "hard_fail");
  assert.equal(pollOutcome(503), "hard_fail");
});
