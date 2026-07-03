// today.js — daysUntil() boundary + tz-independence (DHA-369 #3).
//
// today.js imports state.js, which reads window.VACANCY_DATA at import time —
// so a minimal browser shell goes up first (mirrors i18n.test.js). Forcing an
// extreme non-UTC offset before the import means "today" can't accidentally
// match just because the test machine happens to run in UTC.

import { test } from "node:test";
import assert from "node:assert/strict";

process.env.TZ = "Etc/GMT+12"; // UTC-12, as far behind UTC as IANA goes

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

const { daysUntil } = await import("./today.js");

const DAY = 86400000;
// Date-only string offset from today, anchored to UTC (toISOString), so the
// expectation doesn't itself depend on the forced local tz above.
const dateOnly = (offsetDays) =>
  new Date(Date.now() + offsetDays * DAY).toISOString().slice(0, 10);

test("daysUntil: null/blank/invalid -> null", () => {
  assert.equal(daysUntil(null), null);
  assert.equal(daysUntil(""), null);
  assert.equal(daysUntil("not-a-date"), null);
});

test("daysUntil: a deadline of exactly today is 0, never negative (DHA-369 #3 regression)", () => {
  // The old implementation diffed dateStr's UTC midnight against Date.now()'s
  // exact instant, so it returned -1 for "today" any time after 00:00 UTC —
  // silently dropping a same-day deadline from the Today "expiring" list.
  assert.equal(daysUntil(dateOnly(0)), 0);
});

test("daysUntil: tomorrow is 1, yesterday is -1", () => {
  assert.equal(daysUntil(dateOnly(1)), 1);
  assert.equal(daysUntil(dateOnly(-1)), -1);
});

test("daysUntil: unaffected by an extreme local tz (this file forces UTC-12)", () => {
  assert.equal(daysUntil(dateOnly(0)), 0);
  assert.equal(daysUntil(dateOnly(3)), 3);
});
