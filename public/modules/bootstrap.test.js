// U4 — bootstrap source-selection logic. The DOM/fetch parts of boot() need a
// browser; the decision that matters (live vs static fallback vs hard error) is
// a pure function, unit-tested here.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  resolveSource,
  shouldApplyPollResponse,
  pollOutcome,
  runPoll,
  applyPollResult,
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

// U3 review fix — runPoll() is the dependency-injected core of tick(): given
// a fake fetchImpl, it decides the outcome AND whether there's a payload to
// apply, with no DOM/timers involved.

function fakeResponse(status, { etag, json } = {}) {
  return {
    status,
    headers: { get: (h) => (h === "ETag" ? (etag ?? null) : null) },
    json: json || (async () => ({})),
  };
}

test("runPoll: 304 → ok outcome, no payload (unchanged, nothing to render)", async () => {
  const result = await runPoll(async () => fakeResponse(304), "etag-1");
  assert.equal(result.outcome, "ok");
  assert.equal(result.payload, undefined);
  assert.equal(result.etag, "etag-1"); // carried through unchanged
});

test("runPoll: 200 with a body → ok outcome, payload present, etag updated", async () => {
  const result = await runPoll(
    async () =>
      fakeResponse(200, { etag: "etag-2", json: async () => ({ groups: [] }) }),
    "etag-1",
  );
  assert.equal(result.outcome, "ok");
  assert.deepEqual(result.payload, { groups: [] });
  assert.equal(result.etag, "etag-2");
});

test("runPoll: a network failure → soft_fail, no payload", async () => {
  const result = await runPoll(async () => {
    throw new Error("offline");
  }, "etag-1");
  assert.equal(result.outcome, "soft_fail");
  assert.equal(result.payload, undefined);
});

test("runPoll: 500/401/503 → hard_fail, no payload", async () => {
  for (const status of [500, 401, 503]) {
    const result = await runPoll(async () => fakeResponse(status), null);
    assert.equal(result.outcome, "hard_fail");
    assert.equal(result.payload, undefined);
  }
});

test("runPoll: a 200 with an unparseable body → hard_fail, no payload", async () => {
  const result = await runPoll(
    async () =>
      fakeResponse(200, {
        json: async () => {
          throw new Error("bad json");
        },
      }),
    null,
  );
  assert.equal(result.outcome, "hard_fail");
  assert.equal(result.payload, undefined);
});

// U3 review fix — applyPollResult() wires a runPoll() result into the
// sync-status state machine and (only when there's data to apply) the render
// pipeline. Dependency-injected spies, no DOM: this is the regression guard
// for "a poll fired scheduleRender() every 60s regardless of whether
// anything changed", which blew away DOM-only UI state (expanded catalog
// cards, the triage board) on an unchanged snapshot.

function spyDeps() {
  const calls = {
    recordSyncOutcome: [],
    emit: [],
    applySnapshot: [],
    scheduleRender: 0,
  };
  const deps = {
    recordSyncOutcome: (o) => calls.recordSyncOutcome.push(o),
    emit: (e) => calls.emit.push(e),
    applySnapshot: (p) => {
      calls.applySnapshot.push(p);
      return true;
    },
    scheduleRender: () => {
      calls.scheduleRender++;
    },
  };
  return { calls, deps };
}

test("applyPollResult: an unchanged poll (304, no payload) syncs the footer but never renders", () => {
  const { calls, deps } = spyDeps();
  applyPollResult({ outcome: "ok", etag: "e1" }, deps);
  assert.deepEqual(calls.recordSyncOutcome, ["ok"]);
  assert.deepEqual(calls.emit, ["sync"]);
  assert.equal(calls.applySnapshot.length, 0);
  assert.equal(calls.scheduleRender, 0);
});

test("applyPollResult: a changed poll (payload present) syncs the footer AND schedules a render", () => {
  const { calls, deps } = spyDeps();
  applyPollResult({ outcome: "ok", etag: "e2", payload: { groups: [] } }, deps);
  assert.deepEqual(calls.recordSyncOutcome, ["ok"]);
  assert.deepEqual(calls.emit, ["sync"]);
  assert.deepEqual(calls.applySnapshot, [{ groups: [] }]);
  assert.equal(calls.scheduleRender, 1);
});

test("applyPollResult: a failed poll (soft_fail/hard_fail) syncs the footer but never renders", () => {
  for (const outcome of ["soft_fail", "hard_fail"]) {
    const { calls, deps } = spyDeps();
    applyPollResult({ outcome, etag: null }, deps);
    assert.deepEqual(calls.recordSyncOutcome, [outcome]);
    assert.deepEqual(calls.emit, ["sync"]);
    assert.equal(calls.scheduleRender, 0);
  }
});

test("applyPollResult: applySnapshot rejecting an invalid payload still doesn't render", () => {
  const { calls, deps } = spyDeps();
  deps.applySnapshot = (p) => {
    calls.applySnapshot.push(p);
    return false;
  };
  applyPollResult({ outcome: "ok", etag: "e3", payload: null }, deps);
  assert.equal(calls.scheduleRender, 0);
});
