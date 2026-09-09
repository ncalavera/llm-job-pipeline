// review-decision.test.js — the two rules the daily review screen must not
// break: a single-row decision joins the SAME undo history the bulk path uses,
// and an "unsure" row comes back to the Inbox the next day.
//
// Before this, the row and keyboard Like/Pass wrote through a fire-and-forget
// path that recorded nothing to undo (2026-09-09 UX audit, P1 finding 1): one
// mis-keyed pass on a good role was unrecoverable from the interface.

import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = {
  VACANCY_DATA: {
    config: {},
    stats: {},
    vacancy_ids: [],
    groups: [],
    companies: [],
    triage_reviews: [],
    archived_groups: [],
  },
};
globalThis.location = { protocol: "file:", origin: "" };

const { bulkSet, undoLast, decisionState } = await import("./screen.js");
const { state, getGroupStatus, isUnsureToday, STATUS_BASKET } =
  await import("./state.js");

/** The injectable write path screen.js's bulkSet/undoLast take. */
function fakeIo(db) {
  return {
    members: (id) => [id],
    current: (id) => db[id],
    revision: () => "1",
    async write(ids, targetOf) {
      const previous = {};
      for (const id of ids) {
        previous[id] = db[id];
        db[id] = targetOf(id);
      }
      return previous;
    },
  };
}

test("one row decision lands in the undo history and undo restores it", async () => {
  const db = { role: "unseen" };
  const io = fakeIo(db);
  const before = decisionState().canUndo;
  const result = await bulkSet(["role"], "passed", io);
  assert.equal(result.saved, 1);
  assert.equal(db.role, "passed");
  assert.equal(
    decisionState().canUndo,
    true,
    "a row decision must be undoable",
  );
  await undoLast(io);
  assert.equal(db.role, "unseen");
  void before;
});

test("each of the three decisions writes its own status", async () => {
  const db = { a: "unseen", b: "unseen", c: "unseen" };
  const io = fakeIo(db);
  await bulkSet(["a"], "liked", io);
  await bulkSet(["b"], "passed", io);
  await bulkSet(["c"], "unsure", io);
  assert.deepEqual(db, { a: "liked", b: "passed", c: "unsure" });
});

test("undo walks back one decision at a time, newest first", async () => {
  const db = { a: "unseen", b: "unseen" };
  const io = fakeIo(db);
  await bulkSet(["a"], "liked", io);
  await bulkSet(["b"], "unsure", io);
  await undoLast(io);
  assert.deepEqual(db, { a: "liked", b: "unseen" });
  await undoLast(io);
  assert.deepEqual(db, { a: "unseen", b: "unseen" });
});

// --- "unsure" returns tomorrow --------------------------------------------

const group = { id: "u1", member_ids: [] };

test("a role marked unsure today leaves the Inbox", () => {
  state.dbData.u1 = {
    status: "unsure",
    status_changed_at: new Date().toISOString(),
  };
  assert.equal(getGroupStatus(group), "unsure");
  assert.notEqual(STATUS_BASKET.unsure, "unseen");
});

test("the same role is back in the Inbox the next day", () => {
  const yesterday = new Date(Date.now() - 86400000).toISOString();
  state.dbData.u1 = { status: "unsure", status_changed_at: yesterday };
  assert.equal(getGroupStatus(group), "unseen");
});

test("an unsure row with no recorded time comes back rather than disappearing", () => {
  state.dbData.u1 = { status: "unsure" };
  assert.equal(isUnsureToday(state.dbData.u1), false);
  assert.equal(getGroupStatus(group), "unseen");
});

test("a real decision on any member outranks another member's deferral", () => {
  state.dbData.u1 = {
    status: "unsure",
    status_changed_at: new Date().toISOString(),
  };
  state.dbData.u2 = { status: "liked" };
  assert.equal(getGroupStatus({ id: "u1", member_ids: ["u2"] }), "liked");
});
