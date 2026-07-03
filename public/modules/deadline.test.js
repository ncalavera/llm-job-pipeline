// formatDeadlineHtml / relativeTime — deadline + relative-time rendering.
// Regression tests for DHA-369/DHA-372:
//   - the displayed calendar date must never drift by a day from the deadline
//     actually stored, regardless of the viewer's local timezone (#2);
//   - the label text and unit suffixes route through an injected translator
//     instead of hardcoded English (#1, DHA-372 relativeTime finding).
//
// helpers.js has no imports (pure), so no browser shell is needed here.

import { test } from "node:test";
import assert from "node:assert/strict";

// Force an extreme non-UTC offset before any Date/Intl call in this process —
// node --test isolates each test file into its own process, so this only
// affects this file. Etc/GMT+12 is UTC-12: a UTC-midnight instant renders a
// full calendar day earlier in the viewer's local zone if the tz shift bug
// resurfaces.
process.env.TZ = "Etc/GMT+12";

import { formatDeadlineHtml, relativeTime } from "./helpers.js";

const DAY = 86400000;
const dateOnly = (offsetDays) =>
  new Date(Date.now() + offsetDays * DAY).toISOString().slice(0, 10);

// --- formatDeadlineHtml: today-inclusion (#2 label vs. date consistency) ---

test("formatDeadlineHtml: a deadline of today renders the 'today' label", () => {
  const html = formatDeadlineHtml(dateOnly(0), "dl");
  assert.match(html, /\(today!\)/);
  assert.doesNotMatch(html, /expired/);
});

test("formatDeadlineHtml: a past deadline renders 'expired'", () => {
  const html = formatDeadlineHtml(dateOnly(-1), "dl");
  assert.match(html, /\(expired\)/);
});

// --- formatDeadlineHtml: tz-independence (#2) --------------------------------

test("formatDeadlineHtml: the displayed date matches the deadline's own calendar day under an extreme tz", () => {
  const html = formatDeadlineHtml("2026-12-25", "dl");
  // Under the forced UTC-12 offset, a naive toLocaleDateString (no explicit
  // timeZone) would print "Dec 24" — a day behind the stored deadline.
  assert.match(html, /Dec 25, 2026/);
  assert.doesNotMatch(html, /Dec 24/);
});

test("formatDeadlineHtml: a datetime-with-no-offset deadline still resolves to its own calendar day", () => {
  // Some sources may hand back a full timestamp with no "Z"; JS would parse
  // that as LOCAL midnight (not UTC) if handed to Date() whole. Slicing to
  // the first 10 chars before parsing keeps the calendar date deterministic.
  const html = formatDeadlineHtml("2026-12-25T00:00:00", "dl");
  assert.match(html, /Dec 25, 2026/);
});

// --- formatDeadlineHtml: i18n hook (#1) --------------------------------------

test("formatDeadlineHtml: falls back to English with no translator supplied", () => {
  const html = formatDeadlineHtml(dateOnly(0), "dl");
  assert.match(html, /Deadline:/);
});

// Non-Latin placeholder strings (not real Cyrillic — this file is scanned by
// tests/test_dashboard_no_cyrillic.py) stand in for "some other language" to
// prove the translator hook actually drives the output.
test("formatDeadlineHtml: a translator localizes the prefix + urgency label", () => {
  const t = (key, fallback) =>
    ({ deadline_prefix: "XX-PREFIX:", deadline_today: "(XX-TODAY!)" })[key] ||
    fallback;
  const html = formatDeadlineHtml(dateOnly(0), "dl", { t, locale: "de-DE" });
  assert.match(html, /XX-PREFIX:/);
  assert.match(html, /\(XX-TODAY!\)/);
  assert.doesNotMatch(html, /Deadline:/);
});

// --- relativeTime: i18n hook (DHA-372) ---------------------------------------

test("relativeTime: falls back to English unit suffixes with no translator", () => {
  const twoHoursAgo = new Date(Date.now() - 2 * 3600000).toISOString();
  assert.equal(relativeTime(twoHoursAgo), "2h ago");
});

test("relativeTime: a translator localizes the unit suffix", () => {
  const t = (key, fallback) => ({ time_hour_ago: "{n}h-XX" })[key] || fallback;
  const twoHoursAgo = new Date(Date.now() - 2 * 3600000).toISOString();
  assert.equal(relativeTime(twoHoursAgo, t), "2h-XX");
});

test("relativeTime: null/invalid -> em dash, translator or not", () => {
  assert.equal(relativeTime(null), "—");
  assert.equal(relativeTime("not-a-date"), "—");
});
