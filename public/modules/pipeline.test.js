// pipeline.js — triage card rendering. buildTriageCard interpolates the private
// triage-review fields (cv_notes, skip_reason, research_question, …) into the
// card's innerHTML. Those are free text the user typed, so every one must be
// HTML-escaped before it reaches the DOM or a stored note like
// "<img src=x onerror=…>" becomes stored XSS (DHA-373). pipeline.js imports
// state.js (destructures window.VACANCY_DATA at eval) and Sortable, so we stub
// the browser globals those read at import time, then dynamic-import.

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

const { buildTriageCard, buildTriageGroupCard, triageScoreHtml } =
  await import("./pipeline.js");

// A non-compact column so the review-meta block renders (compact cards omit it).
const COL = { key: "to_apply", label: "To apply", color: "#000" };
const LIKED_COL = {
  key: "liked",
  label: "Liked",
  color: "#000",
  compact: true,
};

function group() {
  return {
    id: "v1",
    org: "Example Org",
    title: "Some Role",
    locations: [],
    org_url: "",
  };
}

const XSS = '<img src=x onerror="alert(1)">';

test("buildTriageCard: a cv_note with <img onerror> renders inert, not a tag", () => {
  const html = buildTriageCard(group(), COL, { cv_notes: XSS });
  // No live tag and no live event-handler attribute may survive…
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /onerror="/); // the payload's quotes are escaped
  // …the note appears only as escaped, inert text instead.
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
});

test("buildTriageCard: every private review field is HTML-escaped", () => {
  const fields = [
    "cv_notes",
    "skip_reason",
    "research_question",
    "network_contact",
    "deadline",
    "github_issue",
  ];
  for (const field of fields) {
    const html = buildTriageCard(group(), COL, { [field]: XSS });
    assert.doesNotMatch(html, /<img/i, `${field} leaked a raw <img> tag`);
    assert.match(html, /&lt;img/, `${field} was not escaped`);
  }
});

test("buildTriageCard: benign notes still render their text", () => {
  const html = buildTriageCard(group(), COL, {
    cv_notes: "Tailor CV for growth role",
  });
  assert.match(html, /Tailor CV for growth role/);
});

// --- org-name link via resolveVacancyCompany (post-ship fast fix #6) -------
// The card's org link used to check the group's own company_slug, a field
// data_prep.py never sets — it was always the plain, non-clickable div.

test("buildTriageCard: org name links to the company when company_id matches", () => {
  const g = { ...group(), company_id: "c1" };
  const companies = [{ company_id: "c1", slug: "example-org" }];
  const html = buildTriageCard(g, COL, {}, companies);
  assert.match(html, /pipe-card-org-link/);
  assert.match(html, /data-company-slug="example-org"/);
});

test("buildTriageCard: no matching company (or no companies list) stays plain, non-clickable", () => {
  const g = { ...group(), company_id: "c1" };
  assert.doesNotMatch(buildTriageCard(g, COL, {}, []), /pipe-card-org-link/);
  assert.doesNotMatch(buildTriageCard(g, COL, {}), /pipe-card-org-link/); // omitted entirely
});

// --- location + compensation line, and the Source ↗ link ------------------

test("buildTriageCard: city + compensation render as one line, joined by ·", () => {
  const g = {
    ...group(),
    locations: [
      { city: "Brooklyn", compensation: "£70,000 - £105,000 / year" },
    ],
  };
  const html = buildTriageCard(g, COL, {});
  assert.match(html, /pipe-card-loc/);
  assert.match(html, /Brooklyn · £70,000 - £105,000 \/ year/);
});

test("buildTriageCard: city with no compensation renders just the city", () => {
  const g = { ...group(), locations: [{ city: "Brooklyn" }] };
  const html = buildTriageCard(g, COL, {});
  assert.match(html, /pipe-card-loc">Brooklyn</);
});

test("buildTriageCard: remote work_mode with no city renders Remote", () => {
  const g = { ...group(), locations: [{ work_mode: "remote" }] };
  const html = buildTriageCard(g, COL, {});
  assert.match(html, /pipe-card-loc">Remote</);
});

test("buildTriageCard: no city, not remote, no compensation omits the location line entirely", () => {
  const g = { ...group(), locations: [{ work_mode: "onsite" }] };
  const html = buildTriageCard(g, COL, {});
  assert.doesNotMatch(html, /pipe-card-loc/);
});

test("buildTriageCard: location text is HTML-escaped", () => {
  const g = { ...group(), locations: [{ city: XSS }] };
  const html = buildTriageCard(g, COL, {});
  assert.doesNotMatch(html, /<img/i);
  assert.match(html, /&lt;img/);
});

test("buildTriageCard: deadline pill still renders as before (regression)", () => {
  const g = {
    ...group(),
    deadline: new Date(Date.now() + 5 * 86400000).toISOString().slice(0, 10),
  };
  const html = buildTriageCard(g, COL, {});
  assert.match(html, /pipe-deadline/);
});

test("buildTriageCard: a resolvable posting URL renders the Source ↗ link", () => {
  const g = { ...group(), locations: [{ url: "https://example.com/job/1" }] };
  const html = buildTriageCard(g, COL, {});
  assert.match(html, /pipe-card-source-link/);
  assert.match(html, /href="https:\/\/example\.com\/job\/1"/);
  assert.match(html, /target="_blank"/);
});

test("buildTriageCard: no deadline, not stale, no resolvable URL renders no meta row", () => {
  const html = buildTriageCard(group(), COL, {});
  assert.doesNotMatch(html, /pipe-card-fresh/);
});

test("buildTriageCard: Liked (compact) column omits location line and source link even when data is present", () => {
  const g = {
    ...group(),
    locations: [
      {
        city: "Brooklyn",
        compensation: "£70k",
        url: "https://example.com/job/1",
      },
    ],
  };
  const html = buildTriageCard(g, LIKED_COL, {});
  assert.doesNotMatch(html, /pipe-card-loc/);
  assert.doesNotMatch(html, /pipe-card-source-link/);
});

test("buildTriageGroupCard: expanded column shows location/source per role, omitting fields the role lacks", () => {
  const roles = [
    {
      ...group(),
      id: "v1",
      title: "Role A",
      locations: [{ city: "Brooklyn", url: "https://example.com/job/a" }],
    },
    { ...group(), id: "v2", title: "Role B", locations: [] },
  ];
  const html = buildTriageGroupCard(roles, COL, []);
  assert.match(html, /pipe-card-loc">Brooklyn</);
  assert.match(html, /pipe-card-source-link/);
});

test("buildTriageGroupCard: Liked (compact) column shows no location or source link for any role", () => {
  const roles = [
    {
      ...group(),
      id: "v1",
      title: "Role A",
      locations: [{ city: "Brooklyn", url: "https://example.com/job/a" }],
    },
  ];
  const html = buildTriageGroupCard(roles, LIKED_COL, []);
  assert.doesNotMatch(html, /pipe-card-loc/);
  assert.doesNotMatch(html, /pipe-card-source-link/);
});

// --- A role with no score at all -------------------------------------------
//
// Applications added by hand (`vac add` — a course, a grant, a programme) are
// never seen by the scorer. The card used to render `score != null ? … : ""`,
// so those rows showed a silent hole where every other card carries a number,
// and the reader could not tell "never scored" from "the number failed to
// load". Three separate ways to get this wrong: a blank, a red zero, and NaN.

test("an unscored role shows an em dash, not a blank", () => {
  const g = group();
  g.llm_score = null;
  const html = buildTriageCard(g, COL, null);
  assert.ok(html.includes("pipe-card-score"), "no score badge at all");
  assert.ok(html.includes("—"));
  assert.ok(html.includes("pipe-card-score--none"));
});

test("an unscored role is never painted as a weak (red) score", () => {
  // qualityBand(null) answers "weak" — null is not >= 70 and not >= 50 — so
  // any unguarded path paints "never scored" the same red as "scored 12".
  const html = triageScoreHtml(null, "pipe-card-score");
  assert.ok(!html.includes("q-weak"));
  assert.ok(!html.includes("q-moderate"));
  assert.ok(!html.includes("q-good"));
});

test("no score ever renders as NaN, null, or undefined", () => {
  for (const score of [null, undefined, NaN, "", -1]) {
    const html = triageScoreHtml(score, "pipe-card-score");
    assert.ok(!/NaN|null|undefined/.test(html), `score ${String(score)} → ${html}`);
    assert.ok(html.includes("—"), `score ${String(score)} lost its em dash`);
  }
});

test("a negative score is the awaiting-scoring sentinel, not a real zero", () => {
  // The pipeline writes -1 for "queued for scoring". Rendering it as the
  // number -1 in a red badge would read as the worst role on the board.
  const html = triageScoreHtml(-1, "pipe-card-score");
  assert.ok(!html.includes("-1"));
  assert.ok(html.includes("—"));
});

test("a real score still renders as its number in its quality colour", () => {
  const html = triageScoreHtml(72, "pipe-card-score");
  assert.ok(html.includes(">72<"));
  assert.ok(html.includes("q-good"));
  assert.ok(!html.includes("--none"));
});

test("zero is a real score, not a missing one", () => {
  // The boundary that a truthiness check gets wrong: `score || "—"` turns a
  // genuine 0 into an em dash.
  const html = triageScoreHtml(0, "pipe-card-score");
  assert.ok(html.includes(">0<"));
  assert.ok(!html.includes("—"));
});

test("an unscored role in a grouped company card also shows the em dash", () => {
  const a = group();
  a.llm_score = null;
  const b = group();
  b.id = "v2";
  b.title = "Another Role";
  b.llm_score = 80;
  const html = buildTriageGroupCard([a, b], COL);
  assert.ok(html.includes("pipe-grp-role-score--none"));
  assert.ok(html.includes("—"));
  assert.ok(html.includes(">80<"));
});
