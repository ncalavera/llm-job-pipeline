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

const { groups, getGroupStatus, mergeRemoteStatuses } =
  await import("./state.js");

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
