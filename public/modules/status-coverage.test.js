// status-coverage.test.js — the R18 invariant, enforced.
//
// helpers.js promises it in a comment ("no status the UI can set may fall
// through showToast silently"), but nothing checked it, and every status added
// after the maps were written slipped through: `test_task` (migration 0020)
// reached six lookup tables as a missing key, and `interview`/`declined`
// (migration 0019) reached most of them the same way. The symptom is always
// quiet — a chip that says nothing, a toast that never fires, a funnel that
// shrinks as applications progress.
//
// So: walk EVERY status in the client vocabulary (state.js STATUS_BASKET +
// the non-derived TRIAGE_COLUMNS keys) against every status-keyed map. A
// status is covered when the map has an entry for it, or when this file lists
// it as a deliberate omission WITH the reason. Adding a status and forgetting
// a map fails here.
//
// state.js reads window.VACANCY_DATA at import time, so the browser shell goes
// up first (mirrors vacancy.test.js / today.test.js), then the dynamic import.

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

const { STATUS_BASKET, TRIAGE_COLUMNS } = await import("./state.js");
const { NON_APPLYABLE_STATUSES, selectTodayRoles } =
  await import("./derive.js");
const { TOAST_MESSAGES, computeTriageFunnel } = await import("./helpers.js");
const { _STATUS_CHIP_KEYS } = await import("./vacancy.js");
const { _ROLE_STATUS_GROUP } = await import("./companies.js");

// Columns the board renders from a real DB status. `expired` is display-only
// (derived from a lapsed deadline), so it is not a status any map keys on.
const COLUMN_STATUSES = TRIAGE_COLUMNS.filter((c) => !c.derived).map(
  (c) => c.key,
);

// The full client vocabulary: everything a status lookup can be handed.
const ALL_STATUSES = [
  ...new Set([...Object.keys(STATUS_BASKET), ...COLUMN_STATUSES]),
];

/** Assert each status is either in `map` or in `omitted` — and that `omitted`
 * lists nothing the map actually covers, so a stale exemption also fails. */
function assertCovers(name, has, omitted) {
  const missing = ALL_STATUSES.filter((s) => !has(s) && !omitted[s]);
  assert.deepEqual(
    missing,
    [],
    `${name} has no entry for: ${missing.join(", ")} — add one, or list it in this test with the reason it is deliberately absent`,
  );
  const stale = Object.keys(omitted).filter(
    (s) => has(s) && ALL_STATUSES.includes(s),
  );
  assert.deepEqual(
    stale,
    [],
    `${name} now covers ${stale.join(", ")} — drop the exemption in this test`,
  );
}

test("R18: the vocabulary itself carries every board column", () => {
  // A column whose status has no basket would render rows the counts ignore.
  const orphans = COLUMN_STATUSES.filter((s) => !STATUS_BASKET[s]);
  assert.deepEqual(orphans, []);
  // Guard against a silent shrink of the walked set.
  assert.ok(ALL_STATUSES.includes("test_task"));
  assert.ok(ALL_STATUSES.includes("interview"));
  assert.ok(ALL_STATUSES.includes("declined"));
});

test("R18: derive.js NON_APPLYABLE_STATUSES rules on every status", () => {
  // Here "covered" is the other way round: a status is either disqualifying or
  // deliberately still applyable. A status in neither list is an accident.
  const stillApplyable = {
    unseen: "nothing decided yet — the whole point of the count",
    liked: "wants it, has not queued it",
    to_apply: "queued to send",
    to_research: "in progress, still worth applying to",
    to_network: "in progress, still worth applying to",
  };
  assertCovers(
    "NON_APPLYABLE_STATUSES",
    (s) => NON_APPLYABLE_STATUSES.has(s),
    stillApplyable,
  );
});

test("R18: selectTodayRoles routes every status it should", () => {
  // One approved, live, in-window role per status; Today then has to put each
  // one somewhere or deliberately drop it.
  const groups = ALL_STATUSES.map((s) => ({
    id: `v-${s}`,
    _status: s,
    llm_score: 90,
    deadline: null,
    first_seen: "2020-01-01",
  }));
  const out = selectTodayRoles(groups, {
    isApproved: () => true,
    getStatus: (g) => g._status,
    isLiveRole: () => true,
    // Real day arithmetic: `unseen` only reaches a Today block through its age.
    daysUntil: (d) =>
      d == null
        ? null
        : Math.round((new Date(d).getTime() - Date.now()) / 86400000),
    soonDays: 7,
  });
  const placed = new Set();
  for (const g of out.committed) placed.add(g.g._status);
  for (const r of out.closingSoon) placed.add(r.g._status);
  for (const list of [
    out.testTask,
    out.awaiting,
    out.liked,
    out.dontRot,
    out.working,
  ]) {
    for (const g of list) placed.add(g._status);
  }
  const notInToday = {
    passed: "decided against — nothing left to do",
    skipped: "deferred on purpose; Today is for what needs a decision now",
    declined: "the employer's no closed it; no action is owed",
  };
  assertCovers("selectTodayRoles", (s) => placed.has(s), notInToday);

  // And the take-home gets its own population, not folded into Awaiting.
  assert.deepEqual(
    out.testTask.map((g) => g.id),
    ["v-test_task"],
  );
  assert.deepEqual(out.awaiting.map((g) => g.id).sort(), [
    "v-applied",
    "v-interview",
  ]);
});

test("R18: helpers.js TOAST_MESSAGES covers every status a UI action sets", () => {
  const noToast = {
    unseen: "the default, never set by an action",
    expiring: "set by the pipeline, not by the user",
  };
  assertCovers("TOAST_MESSAGES", (s) => !!TOAST_MESSAGES[s], noToast);
});

test("R18: vacancy.js status chip covers every status", () => {
  const noChip = {
    unseen: "nothing decided — an empty chip is worse than none",
    expiring: "rendered as its own banner, not as a status chip",
  };
  assertCovers("_STATUS_CHIP_KEYS", (s) => !!_STATUS_CHIP_KEYS[s], noChip);
});

test("R18: companies.js role sort groups every status", () => {
  // Missing here is not cosmetic: the fallback is 1 (the "unseen" group), so
  // an in-flight application sorted below untouched roles on its own company
  // page.
  assertCovers("_ROLE_STATUS_GROUP", (s) => _ROLE_STATUS_GROUP[s] != null, {});
  // The three application stages must sort together, ahead of unseen.
  assert.equal(_ROLE_STATUS_GROUP.test_task, _ROLE_STATUS_GROUP.applied);
  assert.equal(_ROLE_STATUS_GROUP.interview, _ROLE_STATUS_GROUP.applied);
  assert.ok(_ROLE_STATUS_GROUP.applied < _ROLE_STATUS_GROUP.unseen);
  // The employer's no is a closed outcome, with the user's own no.
  assert.equal(_ROLE_STATUS_GROUP.declined, _ROLE_STATUS_GROUP.passed);
});

test("R18: the triage funnel counts every triaged column", () => {
  // One approved entry per board column. `liked` is reported separately
  // (liked_queue), so triaged_total must equal every OTHER column.
  const columnKeys = new Set(COLUMN_STATUSES);
  // Distinct org/title per entry: the funnel dedupes by them, and identical
  // fixtures would collapse to one row.
  const entries = COLUMN_STATUSES.map((s) => ({
    id: `v-${s}`,
    org: `Org ${s}`,
    title: `Role ${s}`,
    locations: [],
    _status: s,
    _approved: true,
    last_seen: new Date().toISOString().slice(0, 10),
    deadline: null,
  }));
  const { metrics } = computeTriageFunnel(entries, {
    statusPri: Object.fromEntries(COLUMN_STATUSES.map((s, i) => [s, i])),
    statusBasket: STATUS_BASKET,
    columnKeys,
  });
  assert.equal(metrics.liked_queue, 1);
  assert.equal(metrics.triaged_total, COLUMN_STATUSES.length - 1);
});
