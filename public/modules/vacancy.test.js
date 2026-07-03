// vacancy.js — pure assembly for the vacancy detail page (U6, DHA-390).
//
// vacancy.js imports state.js, which reads window.VACANCY_DATA at import time —
// so a minimal browser shell goes up before the dynamic import (mirrors
// today.test.js / i18n.test.js). The functions under test take their own
// { t, locale }, so no baked i18n is needed.

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

const {
  sourceLabel,
  vacancyActions,
  statusChipLabel,
  buildFactsRail,
  nextUnreviewedId,
  vacancyPageHtml,
  vacancyNotFoundHtml,
} = await import("./vacancy.js");
const { toastMessage } = await import("./helpers.js");

// English fallback resolver + a fixed locale for deterministic dates.
const t = (key, fallback) => fallback;
const opts = { t, locale: "en-US" };

const fullGroup = {
  id: "g1",
  org: "GiveWell",
  company_name: "GiveWell",
  company_slug: "givewell",
  calculated_tier: "S",
  title: "Research Analyst",
  llm_score: 82,
  llm_summary: "Short summary of the role.",
  llm_reasoning: "Why it scored 82.",
  full_description:
    "## About\n\nA meaningfully longer description than the summary. " +
    "x".repeat(120),
  llm_hard_requirements: ["PhD", "5y experience"],
  us_eligibility: "unclear",
  compensation: "$90k–$110k",
  deadline: "2099-01-01",
  first_seen: "2026-06-01",
  last_seen: "2026-07-01",
  org_color: ["#0B5F44", "#E3F2EB"],
  org_url: "https://givewell.org/jobs/1",
  locations: [
    { location: "Remote · Americas", url: "https://givewell.org/jobs/1" },
    { location: "London, UK", url: "" },
  ],
  member_ids: ["m1"],
};

const company = {
  slug: "givewell",
  name: "GiveWell",
  strategy: "greenhouse",
  sector: "Global health & development",
  calculated_tier: "S",
};

// --- sourceLabel ------------------------------------------------------------

test("sourceLabel maps known strategies and omits manual/absent", () => {
  assert.equal(sourceLabel("greenhouse"), "Greenhouse");
  assert.equal(sourceLabel("workday_api"), "Workday");
  assert.equal(sourceLabel("manual_check"), "");
  assert.equal(sourceLabel(""), "");
  assert.equal(sourceLabel(undefined), "");
  // Unknown strategy → capitalised fallback, never a crash.
  assert.equal(sourceLabel("weirdats"), "Weirdats");
});

// --- vacancyActions ---------------------------------------------------------

test("vacancyActions mirror the catalog basket gating + apply CTA", () => {
  assert.deepEqual(vacancyActions("unseen"), {
    canLike: true,
    canPass: true,
    canApply: true,
  });
  assert.deepEqual(vacancyActions("liked"), {
    canLike: false,
    canPass: true,
    canApply: true,
  });
  assert.deepEqual(vacancyActions("passed"), {
    canLike: true,
    canPass: false,
    canApply: true,
  });
  // to_apply (basket=liked) and applied hide the apply CTA so it can't
  // contradict the status chip.
  assert.deepEqual(vacancyActions("to_apply"), {
    canLike: false,
    canPass: true,
    canApply: false,
  });
  assert.deepEqual(vacancyActions("applied"), {
    canLike: false,
    canPass: true,
    canApply: false,
  });
});

// --- statusChipLabel --------------------------------------------------------

test("statusChipLabel labels decisions, stays silent for unseen/expiring", () => {
  assert.equal(statusChipLabel("to_apply", t), "To apply");
  assert.equal(statusChipLabel("liked", t), "Liked");
  assert.equal(statusChipLabel("unseen", t), null);
  // expiring is shown as its own badge, not a chip.
  assert.equal(statusChipLabel("expiring", t), null);
});

// --- buildFactsRail (AE1) ---------------------------------------------------

test("buildFactsRail: full field set yields every row + all locations", () => {
  const { facts, locations } = buildFactsRail(fullGroup, company, opts);
  const labels = facts.map((f) => f.label);
  assert.ok(labels.includes("Compensation"));
  assert.ok(labels.includes("Deadline"));
  assert.ok(labels.includes("First seen"));
  assert.ok(labels.includes("Source"));
  assert.equal(locations.length, 2);
  // The location with a safe URL keeps it; the blank one has none.
  assert.equal(locations[0].url, "https://givewell.org/jobs/1");
  assert.equal(locations[1].url, "");
});

test("buildFactsRail: absent fields produce NO row (AE1)", () => {
  const partial = { ...fullGroup, compensation: "", deadline: "" };
  const labels = buildFactsRail(partial, company, opts).facts.map(
    (f) => f.label,
  );
  assert.ok(!labels.includes("Compensation"));
  assert.ok(!labels.includes("Deadline"));
  assert.ok(labels.includes("First seen"));
  assert.ok(labels.includes("Source"));
});

test("buildFactsRail: empty group + no company → empty rail, no labels", () => {
  const empty = { id: "e", org: "X", title: "Y", locations: [] };
  const { facts, locations } = buildFactsRail(empty, null, opts);
  assert.equal(facts.length, 0);
  assert.equal(locations.length, 0);
});

test("buildFactsRail: Source omitted for a manual_check company", () => {
  const manual = { ...company, strategy: "manual_check" };
  const labels = buildFactsRail(fullGroup, manual, opts).facts.map(
    (f) => f.label,
  );
  assert.ok(!labels.includes("Source"));
});

// --- application entity signal (relocated from the retired Browse card's
// "✉ applied" badge, U5 parity — its own richer lifecycle isn't representable
// by the header status chip, e.g. "interview"/"offer" have no chip label) ---

test("buildFactsRail: an application entity adds an Application row with status + date", () => {
  const withApp = {
    ...fullGroup,
    application: { status: "interview", applied_at: "2026-06-15" },
  };
  const { facts } = buildFactsRail(withApp, company, opts);
  const row = facts.find((f) => f.label === "Application");
  assert.ok(row, "no Application row rendered");
  assert.ok(row.value.includes("interview"));
  assert.ok(row.value.includes("Jun"));
});

test("buildFactsRail: an application with no applied_at shows just the status", () => {
  const withApp = {
    ...fullGroup,
    application: { status: "draft", applied_at: null },
  };
  const { facts } = buildFactsRail(withApp, company, opts);
  const row = facts.find((f) => f.label === "Application");
  assert.equal(row.value, "draft");
});

test("buildFactsRail: no application entity -> no Application row (AE1)", () => {
  const labels = buildFactsRail(fullGroup, company, opts).facts.map(
    (f) => f.label,
  );
  assert.ok(!labels.includes("Application"));
});

// --- nextUnreviewedId (auto-advance / AE5) ----------------------------------

test("nextUnreviewedId advances to the next still-unreviewed id", () => {
  const status = { g1: "unseen", g2: "liked", g3: "unseen", g4: "unseen" };
  const isUnseen = (id) => status[id] === "unseen";
  const queue = ["g1", "g2", "g3", "g4"];
  // skips g2 (already liked)
  assert.equal(nextUnreviewedId("g1", queue, isUnseen), "g3");
  assert.equal(nextUnreviewedId("g3", queue, isUnseen), "g4");
  // nothing unreviewed after the last → done banner (null)
  assert.equal(nextUnreviewedId("g4", queue, isUnseen), null);
});

test("nextUnreviewedId scans from the start when current left the queue", () => {
  const isUnseen = () => true;
  assert.equal(nextUnreviewedId("gX", ["g1", "g2"], isUnseen), "g1");
  assert.equal(nextUnreviewedId("g1", [], isUnseen), null);
});

test("nextUnreviewedId is keyed by id, not list position (AE5)", () => {
  // A poll inserted a higher-scored row above the cursor. Because we search by
  // id, "next" is still the row after g1 in id terms, not whatever now sits at
  // g1's old index.
  const isUnseen = (id) => id !== "g2";
  const queueAfterPoll = ["gNew", "g1", "g2", "g3"];
  assert.equal(nextUnreviewedId("g1", queueAfterPoll, isUnseen), "g3");
});

// --- toast coverage (R18 regression for the silent to_apply gap) ------------

test("toastMessage covers every status the UI can set", () => {
  for (const s of [
    "liked",
    "passed",
    "skipped",
    "to_apply",
    "to_research",
    "to_network",
    "applied",
  ]) {
    const m = toastMessage(s);
    assert.ok(m && m.key && m.fallback, `toast missing for "${s}"`);
  }
  // Not a "you did X" moment → deliberately no toast.
  assert.equal(toastMessage("unseen"), null);
  assert.equal(toastMessage("expiring"), null);
});

// --- vacancyPageHtml structure ---------------------------------------------

test("vacancyPageHtml renders score tile, reading column, rail, actions", () => {
  const html = vacancyPageHtml(fullGroup, company, "unseen", opts);
  assert.ok(html.includes("vac-score-tile"));
  assert.ok(html.includes("q-good-bg")); // 82 → good band
  assert.ok(html.includes("Research Analyst"));
  assert.ok(html.includes("vac-reasoning")); // model reasoning block
  assert.ok(html.includes("vac-rail"));
  assert.ok(html.includes("vacancyMoveToApply")); // apply CTA
  assert.ok(html.includes("vacancyLike")); // unseen → like shown
  assert.ok(html.includes("openCompanyProfile")); // company mini-card
  assert.ok(html.includes("Open posting")); // outbound link present
});

test("vacancyPageHtml: the header org name and the rail company card are keyboard-reachable (R12)", () => {
  const html = vacancyPageHtml(fullGroup, company, "unseen", opts);
  // Header band: .vac-org--link (span acting as a link to the company page).
  assert.match(
    html,
    /class="vac-org vac-org--link" role="button" tabindex="0" onclick="openCompanyProfile\('givewell'\)" onkeydown="if\(event\.key==='Enter'\)\{openCompanyProfile\('givewell'\)\}"/,
  );
  // Rail: .vac-company-card (mini company card, also opens the profile).
  assert.match(
    html,
    /class="vac-company-card" role="button" tabindex="0" onclick="openCompanyProfile\('givewell'\)" onkeydown="if\(event\.key==='Enter'\)\{openCompanyProfile\('givewell'\)\}"/,
  );
});

test("vacancyPageHtml shows a status chip for a decided role, not for unseen", () => {
  assert.ok(
    !vacancyPageHtml(fullGroup, company, "unseen", opts).includes(
      "vac-status-chip",
    ),
  );
  assert.ok(
    vacancyPageHtml(fullGroup, company, "to_apply", opts).includes(
      "vac-status-chip",
    ),
  );
});

test("vacancyPageHtml: null score renders a neutral tile, not a crimson one", () => {
  const noScore = { ...fullGroup, llm_score: null };
  const html = vacancyPageHtml(noScore, company, "unseen", opts);
  assert.ok(html.includes("vac-score--none"));
  assert.ok(!html.includes("q-weak-bg"));
});

// --- source-freshness badge (relocated from the retired Browse card, U5
// parity: "relocation counts as parity, dropping does not") -------------------

const DAY_MS = 86400000;
const daysAgoIso = (days) => new Date(Date.now() - days * DAY_MS).toISOString();

test("vacancyPageHtml: a stale source (14d+ unconfirmed) shows the badge (AE1)", () => {
  const stale = { ...fullGroup, last_seen: daysAgoIso(20) };
  const html = vacancyPageHtml(stale, company, "unseen", opts);
  assert.ok(html.includes("vac-badge--stale"));
  assert.ok(html.includes("stale, likely closed"));
});

test("vacancyPageHtml: a freshly-confirmed source shows no badge (AE1)", () => {
  const fresh = { ...fullGroup, last_seen: daysAgoIso(2) };
  const html = vacancyPageHtml(fresh, company, "unseen", opts);
  assert.ok(!html.includes("vac-badge--stale"));
  assert.ok(!html.includes("stale, likely closed"));
});

test("vacancyPageHtml: no last_seen at all -> no stale badge", () => {
  const noLastSeen = { ...fullGroup, last_seen: null };
  const html = vacancyPageHtml(noLastSeen, company, "unseen", opts);
  assert.ok(!html.includes("vac-badge--stale"));
});

// --- escaping regression (R14) ----------------------------------------------

test("vacancyPageHtml escapes every externally-sourced string", () => {
  const xss = {
    id: "x1",
    org: "<script>alert('org')</script>",
    company_name: "<script>alert('org')</script>",
    company_slug: "givewell",
    title: "<script>alert('title')</script>",
    llm_score: 60,
    llm_summary: "<script>alert('sum')</script>",
    llm_reasoning: "<script>alert('reason')</script>",
    full_description: "<script>alert('desc')</script> " + "q".repeat(120),
    llm_hard_requirements: ["<script>alert('req')</script>"],
    us_eligibility: "unclear",
    compensation: "<script>alert('comp')</script>",
    deadline: "",
    first_seen: "",
    org_url: "javascript:alert('url')",
    locations: [
      { location: "<script>alert('loc')</script>", url: "javascript:alert(1)" },
    ],
    member_ids: [],
  };
  const badCompany = {
    slug: "givewell",
    name: "<script>alert('co')</script>",
    strategy: "greenhouse",
    sector: "<script>alert('sector')</script>",
    calculated_tier: "S",
  };
  const html = vacancyPageHtml(xss, badCompany, "unseen", opts);
  // No raw script tag survives anywhere in the output.
  assert.ok(!html.includes("<script>"), "raw <script> leaked");
  // No javascript: scheme ever becomes a live href.
  assert.ok(!/href="javascript:/i.test(html), "javascript: href leaked");
  // Content is still present, just escaped.
  assert.ok(html.includes("&lt;script&gt;"), "payload not rendered escaped");
});

test("buildFactsRail escapes the application status/date (R14)", () => {
  const xssApp = {
    ...fullGroup,
    application: {
      status: "<script>alert('app')</script>",
      applied_at: "2026-06-15",
    },
  };
  const { facts } = buildFactsRail(xssApp, company, opts);
  const row = facts.find((f) => f.label === "Application");
  assert.ok(!row.value.includes("<script>"), "raw <script> leaked");
  assert.ok(row.value.includes("&lt;script&gt;"), "payload not escaped");
});

test("vacancyPageHtml escapes a quote-breakout URL in href attributes (R14)", () => {
  // safeUrl passes this (scheme is https) but does not escape the quote; without
  // escHtml it would close href="…" and inject an <img onerror>. Covers both the
  // "Open posting" primary URL and the Locations rail link.
  const breakout = 'https://x.com/"><img src=x onerror=alert(1)>';
  const grp = {
    id: "b1",
    org: "Acme",
    company_name: "Acme",
    company_slug: null,
    title: "Role",
    llm_score: 60,
    llm_summary: "s",
    llm_reasoning: "",
    full_description: "",
    llm_hard_requirements: [],
    us_eligibility: "",
    compensation: "",
    deadline: "",
    first_seen: "",
    org_url: breakout,
    locations: [{ location: "Remote", url: breakout, region: "remote" }],
    member_ids: [],
  };
  const html = vacancyPageHtml(grp, null, "unseen", opts);
  assert.ok(!html.includes('"><img'), "quote broke out of an href attribute");
  assert.ok(html.includes("&quot;"), "the breakout quote was not escaped");
});

// --- not-found shape --------------------------------------------------------

test("vacancyNotFoundHtml is a fixed, parameter-free panel", () => {
  const html = vacancyNotFoundHtml(opts);
  assert.ok(html.includes("Not found"));
  assert.ok(html.includes("switchVacancies()")); // back to Browse
  // No raw script and no interpolated id (the fn takes none).
  assert.ok(!html.includes("<script>"));
});
