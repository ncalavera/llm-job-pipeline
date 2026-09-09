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
  triageBuckets,
  screenScoreBadge,
  safeUrl,
  renderLocationChips,
  mdToHtml,
  pluralForm,
  qualityBand,
  qualityClass,
  tierClass,
  scoreLabel,
  resolveVacancyCompany,
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

test("routing: gone liked/to_apply/to_research/to_network keep their current stage", () => {
  for (const s of ["liked", "to_apply", "to_research", "to_network"]) {
    assert.equal(
      triageColumnFor({ _status: s, deadline: dateOnly(-1) }, COLS),
      ["to_research", "to_network"].includes(s) ? "to_apply" : s,
    );
    assert.equal(
      triageColumnFor({ _status: s, last_seen: daysAgoISO(20) }, COLS),
      ["to_research", "to_network"].includes(s) ? "to_apply" : s,
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

test("progress states partition decisions and preserve applications at untracked companies", () => {
  const columnKeys = new Set(["liked", "applied", "declined"]);
  const entries = [
    {id: "keep", org: "A", title: "One", _status: "liked", _approved: false, deadline: dateOnly(-1)},
    {id: "apply", org: "B", title: "Two", _status: "applied", _approved: false},
    {id: "reject", org: "C", title: "Three", _status: "declined", _approved: true},
    {id: "new", org: "D", title: "Four", _status: "unseen", _approved: true},
  ];
  const buckets = triageBuckets(entries, {columnKeys, statusPri: {liked: 0, applied: 1, declined: 2}});
  assert.deepEqual(Object.values(buckets).map(rows => rows.map(r => r.id)), [["keep"], ["apply"], ["reject"]]);
  assert.equal(new Set(Object.values(buckets).flat().map(r => r.id)).size, 3);
  assert.deepEqual(triageBuckets([], {columnKeys, statusPri: {}}), {liked: [], applied: [], declined: []});
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

// --- qualityBand / qualityClass (DHA-385, U1) -------------------------------

test("qualityBand: boundary at 70 (good/moderate)", () => {
  assert.equal(qualityBand(70), "good");
  assert.equal(qualityBand(69), "moderate");
});

test("qualityBand: boundary at 50 (moderate/weak)", () => {
  assert.equal(qualityBand(50), "moderate");
  assert.equal(qualityBand(49), "weak");
});

test("qualityBand: well inside each band", () => {
  assert.equal(qualityBand(95), "good");
  assert.equal(qualityBand(60), "moderate");
  assert.equal(qualityBand(10), "weak");
});

test("qualityClass: mirrors qualityBand as a 'q-' prefixed CSS class", () => {
  assert.equal(qualityClass(70), "q-good");
  assert.equal(qualityClass(69), "q-moderate");
  assert.equal(qualityClass(50), "q-moderate");
  assert.equal(qualityClass(49), "q-weak");
});

test("qualityBand: null/undefined/NaN fall back to weak", () => {
  assert.equal(qualityBand(null), "weak");
  assert.equal(qualityBand(undefined), "weak");
  assert.equal(qualityBand(NaN), "weak");
});

test("qualityClass: null/undefined/NaN fall back to q-weak", () => {
  assert.equal(qualityClass(null), "q-weak");
  assert.equal(qualityClass(undefined), "q-weak");
  assert.equal(qualityClass(NaN), "q-weak");
});

// --- tierClass ---------------------------------------------------------------

test("tierClass: maps S/A/B/C to fixed classes", () => {
  assert.equal(tierClass("S"), "tier-s");
  assert.equal(tierClass("A"), "tier-a");
  assert.equal(tierClass("B"), "tier-b");
  assert.equal(tierClass("C"), "tier-c");
});

test("tierClass: unknown/missing tier falls back safely", () => {
  assert.equal(tierClass("Z"), "tier-unknown");
  assert.equal(tierClass(""), "tier-unknown");
  assert.equal(tierClass(null), "tier-unknown");
  assert.equal(tierClass(undefined), "tier-unknown");
});

// --- scoreLabel ----------------------------------------------------------

test("scoreLabel: boundaries at 50/70/80/90", () => {
  assert.equal(scoreLabel(49), "Weak");
  assert.equal(scoreLabel(50), "Moderate");
  assert.equal(scoreLabel(69), "Moderate");
  assert.equal(scoreLabel(70), "Good");
  assert.equal(scoreLabel(79), "Good");
  assert.equal(scoreLabel(80), "Strong");
  assert.equal(scoreLabel(89), "Strong");
  assert.equal(scoreLabel(90), "Exceptional");
});

test("scoreLabel: null/undefined/NaN fall back to Weak", () => {
  assert.equal(scoreLabel(null), "Weak");
  assert.equal(scoreLabel(undefined), "Weak");
  assert.equal(scoreLabel(NaN), "Weak");
});

// --- resolveVacancyCompany (post-ship fast fix #6) --------------------------

test("resolveVacancyCompany matches by company_id, the only reliable join", () => {
  const companies = [
    { company_id: "1", slug: "givewell", calculated_tier: "S" },
    { company_id: "2", slug: "founders-pledge", calculated_tier: "A" },
  ];
  const g = { org: "Founders Pledge", company_id: "2" };
  assert.deepEqual(resolveVacancyCompany(g, companies), companies[1]);
});

test("resolveVacancyCompany returns null when nothing matches (org untracked)", () => {
  const companies = [{ company_id: "1", slug: "givewell" }];
  assert.equal(
    resolveVacancyCompany({ org: "Untracked Co", company_id: "9" }, companies),
    null,
  );
});

test("resolveVacancyCompany returns null, never throws, on missing inputs", () => {
  assert.equal(resolveVacancyCompany(null, []), null);
  assert.equal(resolveVacancyCompany({ org: "X" }, []), null); // no company_id
  assert.equal(resolveVacancyCompany({ company_id: "1" }, null), null);
  assert.equal(resolveVacancyCompany({ company_id: "1" }, undefined), null);
});

test("resolveVacancyCompany never matches on org/name text alone (the bug it replaces)", () => {
  // Same display name, DIFFERENT company_id — must not match by name/org.
  const companies = [{ company_id: "1", name: "Acme", slug: "acme-old" }];
  const g = { org: "Acme", company_id: "2" };
  assert.equal(resolveVacancyCompany(g, companies), null);
});


// ---------------------------------------------------------------------------
// pluralForm — the last digit decides, not the size of the number.
// ---------------------------------------------------------------------------

test("pluralForm picks the singular form for 1 and for anything ending in 1", () => {
  for (const n of [1, 21, 31, 101, 1001]) {
    assert.equal(pluralForm(n), "one", `${n} should take the "one" form`);
  }
});

test("pluralForm picks the small-plural form for 2-4 and their echoes", () => {
  for (const n of [2, 3, 4, 22, 33, 44, 104]) {
    assert.equal(pluralForm(n), "few", `${n} should take the "few" form`);
  }
});

test("pluralForm picks the big-plural form for 5-20 and for zero", () => {
  for (const n of [0, 5, 9, 10, 20, 25, 100]) {
    assert.equal(pluralForm(n), "many", `${n} should take the "many" form`);
  }
});

test("pluralForm treats the 11-14 teens as the big plural, not as their last digit", () => {
  // The exception that a naive last-digit rule gets wrong: 11 ends in 1 but is
  // not singular, and 12-14 end in 2-4 but are not the small plural.
  for (const n of [11, 12, 13, 14, 111, 112, 113, 114]) {
    assert.equal(pluralForm(n), "many", `${n} should take the "many" form`);
  }
});

test("pluralForm never throws on junk", () => {
  for (const n of [null, undefined, NaN, "x", 1.5, -1]) {
    assert.ok(["one", "few", "many"].includes(pluralForm(n)));
  }
});
