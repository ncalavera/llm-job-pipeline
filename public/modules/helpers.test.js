// "No longer actual" classification + Triage column routing. The helpers are
// pure (helpers.js touches neither the DOM nor window), so the decision that
// drives the Catalog freshness badge and the Triage "Expired" column is unit-
// tested here without a browser.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  STALE_SOURCE_DAYS,
  sourceAgeDays,
  isVacancyExpired,
  isVacancyStale,
  isVacancyGone,
  triageColumnFor,
  dedupeTriageEntries,
  screenScoreBadge,
  safeUrl,
  renderLocationChips,
  mdToHtml,
} from "./helpers.js";

const DAY = 86400000;
// Precise timestamp N days ago → sourceAgeDays floors to exactly N.
const daysAgoISO = (n) => new Date(Date.now() - n * DAY).toISOString();
// Date-only string offset from today (negative = past deadline).
const dateOnly = (offsetDays) =>
  new Date(Date.now() + offsetDays * DAY).toISOString().slice(0, 10);

// --- sourceAgeDays ---------------------------------------------------------

test("sourceAgeDays: null/blank/invalid → null", () => {
  assert.equal(sourceAgeDays(null), null);
  assert.equal(sourceAgeDays(""), null);
  assert.equal(sourceAgeDays("not-a-date"), null);
});

test("sourceAgeDays: whole-day age via floor", () => {
  assert.equal(sourceAgeDays(daysAgoISO(3)), 3);
});

// --- isVacancyExpired / isVacancyStale / isVacancyGone ---------------------

test("gone: deadline in the past", () => {
  assert.equal(isVacancyExpired({ deadline: dateOnly(-1) }), true);
  assert.equal(isVacancyGone({ deadline: dateOnly(-1) }), true);
});

test("boundary: a deadline of exactly today is not expired", () => {
  const g = { deadline: dateOnly(0) };
  assert.equal(isVacancyExpired(g), false);
  assert.equal(isVacancyGone(g), false);
});

test("not gone: deadline in the future, source fresh", () => {
  const g = { deadline: dateOnly(30), last_seen: daysAgoISO(1) };
  assert.equal(isVacancyExpired(g), false);
  assert.equal(isVacancyStale(g), false);
  assert.equal(isVacancyGone(g), false);
});

test("gone: no deadline, stale by last_seen (20d)", () => {
  const g = { last_seen: daysAgoISO(20) };
  assert.equal(isVacancyStale(g), true);
  assert.equal(isVacancyGone(g), true);
});

test("boundary: exactly STALE_SOURCE_DAYS is stale", () => {
  const g = { last_seen: daysAgoISO(STALE_SOURCE_DAYS) };
  assert.equal(isVacancyStale(g), true);
  assert.equal(isVacancyGone(g), true);
});

test("boundary: one day under threshold is not stale", () => {
  const g = { last_seen: daysAgoISO(STALE_SOURCE_DAYS - 1) };
  assert.equal(isVacancyStale(g), false);
  assert.equal(isVacancyGone(g), false);
});

test("not gone: neither deadline nor last_seen present", () => {
  assert.equal(isVacancyExpired({}), false);
  assert.equal(isVacancyStale({}), false);
  assert.equal(isVacancyGone({}), false);
});

// --- triageColumnFor -------------------------------------------------------

const COLS = new Set([
  "liked",
  "expired",
  "to_apply",
  "to_research",
  "to_network",
  "applied",
  "skipped",
]);

test("routing: DB status 'expiring' never lands on the board (→ Today tab)", () => {
  assert.equal(
    triageColumnFor({ _status: "expiring", deadline: dateOnly(-1) }, COLS),
    null,
  );
});

test("routing: fresh liked/to_apply stay in their own column", () => {
  assert.equal(
    triageColumnFor({ _status: "liked", last_seen: daysAgoISO(1) }, COLS),
    "liked",
  );
  assert.equal(
    triageColumnFor({ _status: "to_apply", deadline: dateOnly(30) }, COLS),
    "to_apply",
  );
});

test("routing: gone liked/to_apply/to_research/to_network collapse to 'expired'", () => {
  for (const s of ["liked", "to_apply", "to_research", "to_network"]) {
    assert.equal(
      triageColumnFor({ _status: s, deadline: dateOnly(-1) }, COLS),
      "expired",
    );
    assert.equal(
      triageColumnFor({ _status: s, last_seen: daysAgoISO(20) }, COLS),
      "expired",
    );
  }
});

test("routing: applied and skipped stay put even when gone", () => {
  assert.equal(
    triageColumnFor({ _status: "applied", deadline: dateOnly(-1) }, COLS),
    "applied",
  );
  assert.equal(
    triageColumnFor({ _status: "skipped", last_seen: daysAgoISO(30) }, COLS),
    "skipped",
  );
});

test("routing: statuses without a column (unseen/passed) → null", () => {
  assert.equal(triageColumnFor({ _status: "unseen" }, COLS), null);
  assert.equal(triageColumnFor({ _status: "passed" }, COLS), null);
});

// --- dedupeTriageEntries ---------------------------------------------------

// Regression: the same role from two boards dedupes to one card. If the stale
// copy is inserted first and wins the STATUS_PRI tie, the survivor must still
// inherit the FRESH copy's last_seen — otherwise a still-live role wrongly
// lands in "Expired". Must hold in BOTH insertion orders.
test("dedupe: a stale copy never routes a still-live role to 'expired'", () => {
  const statusPri = { to_apply: 0 };
  const stale = () => ({
    org: "Acme",
    title: "Engineer",
    _status: "to_apply",
    last_seen: daysAgoISO(30),
  });
  const fresh = () => ({
    org: "Acme",
    title: "Engineer",
    _status: "to_apply",
    last_seen: daysAgoISO(1),
  });
  for (const order of [
    [stale(), fresh()],
    [fresh(), stale()],
  ]) {
    const deduped = dedupeTriageEntries(order, statusPri);
    const survivors = Array.from(deduped.values());
    assert.equal(survivors.length, 1);
    assert.equal(triageColumnFor(survivors[0], COLS), "to_apply");
  }
});

// --- screenScoreBadge -------------------------------------------------------
// The backend (scripts/report/data_prep.py) decides WHEN a score is a
// screen-only score high enough to be mistaken for a confirmed one and bakes
// that into group.screen_only_score; this helper only renders the marker.

test("screenScoreBadge: renders nothing when screen_only_score is falsy", () => {
  assert.equal(screenScoreBadge({ screen_only_score: false }), "");
  assert.equal(screenScoreBadge({}), "");
  assert.equal(screenScoreBadge(null), "");
});

test("screenScoreBadge: renders a marker when screen_only_score is true", () => {
  const html = screenScoreBadge({ screen_only_score: true });
  assert.match(html, /screen-score-badge/);
  assert.match(html, /not yet confirmed by the main model/);
});

// --- safeUrl / XSS guard (DHA-363) ------------------------------------------

test("safeUrl: allows http, https, mailto", () => {
  assert.equal(safeUrl("https://example.org/job"), "https://example.org/job");
  assert.equal(safeUrl("http://example.org/job"), "http://example.org/job");
  assert.equal(safeUrl("mailto:jobs@example.org"), "mailto:jobs@example.org");
});

test("safeUrl: rejects javascript: and other dangerous schemes", () => {
  assert.equal(safeUrl("javascript:alert(1)"), "");
  assert.equal(safeUrl("JavaScript:alert(1)"), "");
  assert.equal(safeUrl("data:text/html,<script>alert(1)</script>"), "");
  assert.equal(safeUrl("vbscript:msgbox(1)"), "");
});

test("safeUrl: rejects javascript: obfuscated with leading/embedded whitespace", () => {
  assert.equal(safeUrl("  javascript:alert(1)"), "");
  assert.equal(safeUrl("\n\tjavascript:alert(1)"), "");
  // Browsers strip tabs/newlines from anywhere in the URL before parsing the
  // scheme, so "java\tscript:" is a live bypass of a naive prefix check.
  assert.equal(safeUrl("java\tscript:alert(1)"), "");
  assert.equal(safeUrl("java\nscript:alert(1)"), "");
});

test("safeUrl: rejects blank/missing input", () => {
  assert.equal(safeUrl(""), "");
  assert.equal(safeUrl(null), "");
  assert.equal(safeUrl(undefined), "");
});

test("renderLocationChips: a javascript: chip URL renders inert, not a link", () => {
  const chips = [
    { text: "Remote", region: "remote", url: "javascript:alert(1)" },
  ];
  const html = renderLocationChips(chips, {});
  assert.doesNotMatch(html, /<a /);
  assert.doesNotMatch(html, /javascript:/i);
  assert.match(html, /<span class="loc-chip[^"]*">/);
});

test("renderLocationChips: a normal https chip URL still renders as a link", () => {
  const chips = [
    { text: "Remote", region: "remote", url: "https://example.org/role" },
  ];
  const html = renderLocationChips(chips, {});
  assert.match(html, /<a href="https:\/\/example\.org\/role"/);
});

test("mdToHtml: a javascript: markdown link renders inert, not a link", () => {
  const html = mdToHtml("[click me](javascript:alert(1))");
  assert.doesNotMatch(html, /<a /);
  assert.doesNotMatch(html, /javascript:/i);
  assert.match(html, /click me/);
});

test("mdToHtml: a normal https markdown link still renders as a link", () => {
  const html = mdToHtml("[click me](https://example.org)");
  assert.match(html, /<a href="https:\/\/example\.org"/);
});
