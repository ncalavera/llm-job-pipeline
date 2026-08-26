// state.js — the dbData seed reads each group's baked `status` as the BASE
// layer (DHA-412), so a static/simple-mode data.js shows its baked pipeline
// state instead of every row collapsing to "unseen". The live /api/statuses
// merge (mergeRemoteStatuses) still wins in full mode. state.js reads
// window.VACANCY_DATA + location at import time, so those globals go up first
// (mirrors today.test.js), then we dynamic-import.

import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = {
  VACANCY_DATA: {
    config: {},
    stats: {},
    vacancy_ids: [],
    groups: [
      { id: "v-liked", status: "liked", member_ids: [] },
      { id: "v-passed", status: "passed", member_ids: [] },
      { id: "v-none", member_ids: [] }, // no baked status in the payload
    ],
    companies: [],
    triage_reviews: [],
    archived_groups: [],
  },
};
globalThis.location = { protocol: "file:", origin: "" };

const {
  groups,
  getGroupStatus,
  mergeRemoteStatuses,
  STATUS_PRI,
  STATUS_BASKET,
  TRIAGE_COLUMNS,
} = await import("./state.js");

const byId = (id) => groups.find((g) => g.id === id);

test("simple mode: a group's baked status is the base layer, not forced to unseen", () => {
  // Before any /api/statuses fetch, the baked status is what the basket /
  // Triage / Applied views read — the old seed hard-coded "unseen" here.
  assert.equal(getGroupStatus(byId("v-liked")), "liked");
  assert.equal(getGroupStatus(byId("v-passed")), "passed");
});

test("a group with no baked status still defaults to unseen", () => {
  assert.equal(getGroupStatus(byId("v-none")), "unseen");
});

test("full mode: a live /api/statuses value overrides the baked base layer", () => {
  // The API poll merges after load; the freshly-fetched status wins so a real
  // decision made in another session isn't masked by a stale baked snapshot.
  const changed = mergeRemoteStatuses({ "v-liked": "applied" }, {});
  assert.equal(changed, 1);
  assert.equal(getGroupStatus(byId("v-liked")), "applied");
});

// --- 'test_task' column (the stage between Applied and Interview) -----------
// An employer's take-home assignment used to have no column: those roles sat in
// Applied, indistinguishable from "sent, waiting for a reply", while work was
// actually owed. The three lookup tables below must agree — a column with no
// STATUS_BASKET entry silently falls into the "unseen" basket, and a column
// with no STATUS_PRI entry loses every dedup tie.

test("test_task sits between Applied and Interview on the board", () => {
  const keys = TRIAGE_COLUMNS.map((c) => c.key);
  assert.equal(keys.indexOf("test_task"), keys.indexOf("applied") + 1);
  assert.equal(keys.indexOf("interview"), keys.indexOf("test_task") + 1);
});

test("test_task is a real (droppable) column with its own label and accent", () => {
  const col = TRIAGE_COLUMNS.find((c) => c.key === "test_task");
  assert.ok(col, "no test_task column");
  assert.equal(col.label, "Test task");
  assert.ok(!col.derived, "test_task is a real DB status, not a derived column");
  // Its own hue: the columns on either side must not share it.
  const neighbours = TRIAGE_COLUMNS.filter((c) => c.key !== "test_task").map(
    (c) => c.color,
  );
  assert.ok(!neighbours.includes(col.color), `colour ${col.color} is not unique`);
});

test("test_task ranks between applied and interview, and every column has a rank", () => {
  assert.ok(STATUS_PRI.applied < STATUS_PRI.test_task);
  assert.ok(STATUS_PRI.test_task < STATUS_PRI.interview);
  // No duplicate ranks: two statuses sharing a rank makes a dedup tie arbitrary.
  const ranks = Object.values(STATUS_PRI);
  assert.equal(new Set(ranks).size, ranks.length);
});

test("test_task is active work, so it stays in the liked basket", () => {
  assert.equal(STATUS_BASKET.test_task, "liked");
  assert.equal(STATUS_BASKET.test_task, STATUS_BASKET.interview);
});

test("every real triage column has a basket, or it lands in 'unseen'", () => {
  for (const col of TRIAGE_COLUMNS) {
    if (col.derived) continue;
    assert.ok(STATUS_BASKET[col.key], `column "${col.key}" has no basket`);
    assert.ok(STATUS_PRI[col.key] !== undefined, `column "${col.key}" has no rank`);
  }
});
