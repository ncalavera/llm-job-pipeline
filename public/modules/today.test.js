// today.js — daysUntil() boundary + tz-independence (DHA-369 #3), plus the
// U9 (DHA-393) row/group markup: click contract, action-button escaping, and
// the escaping regression for externally-sourced strings (R14).
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

const { daysUntil, todayRowHtml, todayGroupHtml, openTodayRow } =
  await import("./today.js");

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

// --- todayRowHtml: structure + click contract (U9, DHA-393) -----------------

const baseGroup = {
  id: "g1",
  org: "GiveWell",
  title: "Research Analyst",
  llm_score: 82,
  member_ids: ["m1"],
};

test("todayRowHtml renders a tinted score tile, title, org, and opens the vacancy on click", () => {
  const html = todayRowHtml(baseGroup, null, []);
  assert.match(html, /today-row-score q-good-bg">82</);
  assert.match(html, /today-row-title">Research Analyst</);
  assert.match(html, /today-row-sub">GiveWell</);
  assert.match(
    html,
    /class="today-row" data-id="g1" role="button" tabindex="0" onclick="openTodayRow\('g1'\)"/,
  );
});

test("todayRowHtml: row is keyboard-reachable and Enter opens it only when the row itself has focus (R12)", () => {
  const html = todayRowHtml(baseGroup, null, []);
  assert.match(html, /role="button" tabindex="0"/);
  assert.match(
    html,
    /onkeydown="if\(event\.key==='Enter'&&event\.target===event\.currentTarget\)\{openTodayRow\('g1'\)\}"/,
  );
});

test("todayRowHtml appends the 'why' text to the subline when given", () => {
  const html = todayRowHtml(baseGroup, "deadline in 3d", []);
  assert.match(html, /today-row-sub">GiveWell · deadline in 3d</);
});

test("todayRowHtml: null score renders a neutral tile, not a crimson one", () => {
  const html = todayRowHtml({ ...baseGroup, llm_score: null }, null, []);
  assert.match(html, /vac-score--none">—/);
  assert.doesNotMatch(html, /q-weak-bg/);
});

test("action buttons stop propagation so they never also trigger the row click", () => {
  const html = todayRowHtml(baseGroup, null, [
    { action: "pass", label: "Pass", cls: "act-pass" },
  ]);
  assert.match(
    html,
    /onclick="event\.stopPropagation\(\);todayAction\('g1',\[&quot;m1&quot;\],'pass'\)"/,
  );
});

test("openTodayRow forwards id + today context (no queue — no auto-advance, F3) to the router", () => {
  let called = null;
  globalThis.window.openVacancyRoute = (id, o) => {
    called = { id, opts: o };
  };
  openTodayRow("g7");
  assert.deepEqual(called, { id: "g7", opts: { context: "today" } });
});

// --- todayRowHtml: escaping regression, including attribute positions (R14) -

const xssGroup = {
  id: "g\"'></div><script>1</script>",
  org: '"><svg onload=alert(1)>',
  title: "<img src=x onerror=alert(1)>",
  llm_score: 70,
  member_ids: ["m1"],
};

test("title/org/why are escaped in text-content positions", () => {
  const html = todayRowHtml(xssGroup, "<script>alert('why')</script>", []);
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /<svg onload/);
  assert.doesNotMatch(html, /<script>alert/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /&lt;script&gt;alert\('why'\)&lt;\/script&gt;/);
});

test("an id with quotes/HTML is escaped in the data-id AND the onclick attribute", () => {
  const html = todayRowHtml(xssGroup, null, []);
  // data-id is HTML-escaped (escHtml) — the raw quote/tag never reaches the attribute.
  assert.doesNotMatch(html, /data-id="g"'/);
  assert.match(html, /data-id="g&quot;/);
  // the onclick's single-quoted JS string is jsAttr-escaped — no unescaped ' breaks out.
  assert.doesNotMatch(html, /openTodayRow\('g"'\)/);
});

// --- todayGroupHtml: empty-group rendering (a group never disappears) ------

test("todayGroupHtml: a populated group shows its rows and count", () => {
  const html = todayGroupHtml("Ready to send", ["<row-html>"], "nothing ready");
  assert.match(html, /today-section-count">1</);
  assert.match(html, /today-rows">.*<row-html>/s);
  assert.doesNotMatch(html, /today-empty/);
});

test("todayGroupHtml: an empty group still renders its label (count 0) and the empty note", () => {
  const html = todayGroupHtml("Ready to send", [], "nothing ready to send");
  assert.match(html, /today-section-count">0</);
  assert.match(html, /today-empty">nothing ready to send</);
  assert.doesNotMatch(html, /today-rows/);
});
