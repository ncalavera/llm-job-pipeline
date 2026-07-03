// companies.test.js — pure assembly for the company profile page (U7, DHA-391).
//
// companies.js imports state.js, which reads window.VACANCY_DATA at import
// time — so a minimal browser shell goes up before the dynamic import
// (mirrors vacancy.test.js). companyProfileHtml itself takes a company object,
// a pre-resolved roles array and { t, reviewStatus, monStatus, counts }, so no
// further global state is needed to exercise it directly.

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

const { roleScoreDistribution, companyProfileHtml, _dimNumHtml, _buildRow } =
  await import("./companies.js");
const { state } = await import("./state.js");

const t = (key, fallback) => fallback;
const counts = { liked: 1, passed: 0, unseen: 2 };

// A fully WANT-scored company — every reading-order block should have data.
const scoredCompany = {
  name: "GiveWell",
  company_id: "c1",
  slug: "givewell",
  category: "Global health & development",
  calculated_tier: "S",
  description: "GiveWell finds outstanding giving opportunities.",
  executive_summary: "A rigorous, evidence-driven charity evaluator.",
  notes: "Applied here before, got to final round.",
  recent_news: [
    {
      title: "GiveWell adds a new top charity",
      url: "https://givewell.org/news/1",
    },
  ],
  hq_location: "Oakland, CA",
  offices: "Oakland, Remote",
  employee_count: "50-100",
  founded_year: "2007",
  funding_status: "Nonprofit",
  sector: "Effective altruism",
  glassdoor_rating: 4.2,
  linkedin_employees: "80",
  experience_match: 8,
  personal_interest: 9,
  website: "https://givewell.org",
  careers_url: "https://givewell.org/careers",
  is_enriched: true,
  alignment_score: 82,
  alignment_label: "Excellent fit",
  fit_dimensions: {
    mission_authenticity: 88, // good (>=70)
    domain_desirability: 60, // moderate (50-69)
    breadth_rotation: 40, // weak (<50)
  },
  fit_strengths: ["Direct, measurable impact.", "Strong research culture."],
  fit_risks: ["Small team, limited growth ceiling."],
  fit_evidence: [
    {
      source: "About page",
      claim: "Rigorous methodology",
      quote: "We publish our full reasoning.",
    },
  ],
  fit_approach: "Evidence-first grant evaluation.",
  experience_reasoning: "Background in econ research maps directly.",
  mission_verdict: "A strong, low-risk application for this candidate.",
  md_content: "## Deep dive\n\nMore detail here.",
  mpa_prestige: 70,
  composite_score: 75,
  strategy: "greenhouse",
  last_fetched: "2026-07-01T00:00:00Z",
  vacancy_count: 2,
  avg_llm_score: 74,
  applications: [
    {
      status: "applied",
      channel: "referral",
      applied_at: "2026-06-01",
      artifacts: { cv: true },
    },
  ],
  research: [
    {
      source: "linkedin",
      url: "https://linkedin.com/company/givewell",
      fetched_at: "2026-06-20",
    },
  ],
};

// A pending, never-scored company (AE2): raw evidence anchors but no
// alignment_score/fit_dimensions at all.
const pendingCompany = {
  name: "New Charity Org",
  company_id: "c2",
  slug: "new-charity-org",
  category: "Nonprofit",
  is_enriched: false,
  alignment_score: null,
  fit_dimensions: null,
  fit_evidence: [
    { source: "website", claim: "Mission is squarely EA-aligned" },
  ],
  vacancy_count: 0,
};

const roles = [
  {
    id: "r1",
    title: "Research Analyst",
    score: 85,
    status: "liked",
    loc: "Remote",
    seenRaw: "2026-06-01",
  },
  {
    id: "r2",
    title: "Program Officer",
    score: 62,
    status: "unseen",
    loc: "Oakland, CA",
    seenRaw: "2026-06-15",
  },
  {
    id: "r3",
    title: "Ops Associate",
    score: 30,
    status: "passed",
    loc: "",
    seenRaw: "",
  },
  {
    id: "r4",
    title: "Unscored Fellow",
    score: null,
    status: "unseen",
    loc: "",
    seenRaw: "",
  },
];

// --- roleScoreDistribution ---------------------------------------------------

test("roleScoreDistribution buckets by the shared 70/50 qualityBand", () => {
  const d = roleScoreDistribution([85, 62, 30]);
  assert.deepEqual(
    { strong: d.strong, moderate: d.moderate, weak: d.weak },
    { strong: 1, moderate: 1, weak: 1 },
  );
  assert.equal(d.total, 3);
  assert.ok(Math.abs(d.strongPct - 33.333) < 0.01);
});

test("roleScoreDistribution: boundary values land in the correct band (70/50)", () => {
  const d = roleScoreDistribution([70, 69, 50, 49]);
  assert.equal(d.strong, 1); // 70
  assert.equal(d.moderate, 2); // 69, 50
  assert.equal(d.weak, 1); // 49
});

test("roleScoreDistribution: empty input never divides by zero", () => {
  const d = roleScoreDistribution([]);
  assert.deepEqual(d, {
    strong: 0,
    moderate: 0,
    weak: 0,
    total: 0,
    strongPct: 0,
    moderatePct: 0,
    weakPct: 0,
  });
});

// --- companyProfileHtml: WANT-scored variant --------------------------------

test("companyProfileHtml (scored): renders full reading order without throwing", () => {
  const html = companyProfileHtml(scoredCompany, roles, {
    t,
    reviewStatus: "approved",
    monStatus: {
      label: "Working",
      dotCls: "mon-dot--ok",
      tooltip: "Last fetch 1d ago",
    },
    counts,
  });
  assert.equal(typeof html, "string");
  assert.ok(html.includes("GiveWell"));
  assert.ok(html.includes("Fit analysis"));
  assert.ok(html.includes("Want breakdown"));
  assert.ok(html.includes("Strengths"));
  assert.ok(html.includes("Risks"));
  assert.ok(html.includes("Approach"));
  assert.ok(html.includes("Experience match"));
  assert.ok(html.includes("Deep analysis"));
  assert.ok(html.includes("A strong, low-risk application")); // verdict
  assert.ok(
    html.includes("Applications &amp; research") ||
      html.includes("Applications & research"),
  );
  assert.ok(html.includes("Open roles here"));
  // The AE2 evidence-list variant must NOT appear alongside a full breakdown.
  assert.ok(!html.includes("Why this tier"));
});

test("companyProfileHtml (scored): WANT bars use the shared q-good/q-moderate/q-weak classes at 70/50", () => {
  const html = companyProfileHtml(scoredCompany, [], {
    t,
    reviewStatus: "approved",
    counts,
  });
  // 88 -> good, 60 -> moderate, 40 -> weak (fit_dimensions above)
  assert.ok(html.includes('class="cp-bar-value q-good"'));
  assert.ok(html.includes('class="cp-bar-value q-moderate"'));
  assert.ok(html.includes('class="cp-bar-value q-weak"'));
});

test("companyProfileHtml (scored): the big fit score also uses the shared band class", () => {
  // alignment_score 82 -> good
  const html = companyProfileHtml(scoredCompany, [], {
    t,
    reviewStatus: "approved",
    counts,
  });
  assert.ok(html.includes('class="cp-fit-score q-good"'));
});

test("companyProfileHtml (scored): open-role rows route via the U4/U6 contract", () => {
  const html = companyProfileHtml(scoredCompany, roles, {
    t,
    reviewStatus: "approved",
    counts,
  });
  assert.ok(
    html.includes("openVacancyRoute('r1',{context:'company'})"),
    "role row must call openVacancyRoute with context:'company' so Move-to-apply never auto-advances",
  );
});

test("companyProfileHtml (scored): distribution strip counts match qualityBand over the fixture roles", () => {
  const html = companyProfileHtml(scoredCompany, roles, {
    t,
    reviewStatus: "approved",
    counts,
  });
  // Scored roles: 85 (strong), 62 (moderate), 30 (weak); the null-score role
  // (r4) must not be counted anywhere.
  assert.ok(html.includes(">1 strong<"));
  assert.ok(html.includes(">1 moderate<"));
  assert.ok(html.includes(">1 weak<"));
});

// --- companyProfileHtml: AE2 evidence-list variant --------------------------

test("companyProfileHtml (never scored, AE2): renders the evidence-list variant, no bars, no fabricated zeros", () => {
  const html = companyProfileHtml(pendingCompany, [], {
    t,
    reviewStatus: "pending",
    counts: { liked: 0, passed: 0, unseen: 0 },
  });
  assert.ok(html.includes("Why this tier"));
  assert.ok(html.includes("Mission is squarely EA-aligned"));
  assert.ok(!html.includes("Want breakdown"));
  assert.ok(!html.includes("cp-fit-score"));
  assert.ok(!html.includes("Run /enrich"));
});

test("companyProfileHtml: a company with neither analysis nor evidence shows the /enrich placeholder", () => {
  const bare = {
    name: "Bare Co",
    slug: "bare-co",
    is_enriched: false,
    alignment_score: null,
  };
  const html = companyProfileHtml(bare, [], {
    t,
    reviewStatus: "pending",
    counts: { liked: 0, passed: 0, unseen: 0 },
  });
  assert.ok(html.includes("Run /enrich to add mission fit data"));
});

// --- escaping regression (R14) ----------------------------------------------

test("companyProfileHtml: XSS payloads in about/evidence/verdict/strengths/role titles are inert", () => {
  const payload = '"><script>window.pwned=1</script>';
  const evil = {
    ...scoredCompany,
    name: payload,
    description: payload,
    fit_strengths: [payload],
    fit_risks: [payload],
    fit_evidence: [{ source: payload, claim: payload, quote: payload }],
    mission_verdict: payload,
    notes: payload,
    recent_news: [{ title: payload, url: "https://example.com" }],
    company_id: `c1${payload}`,
  };
  const evilRoles = [
    {
      id: `r1${payload}`,
      title: payload,
      score: 50,
      status: "unseen",
      loc: payload,
      seenRaw: "",
    },
  ];
  const html = companyProfileHtml(evil, evilRoles, {
    t,
    reviewStatus: "pending",
    counts,
  });
  assert.ok(!html.includes("<script>"), "raw <script> leaked into the page");
  assert.ok(
    html.includes("&lt;script&gt;"),
    "payload should be HTML-escaped, not dropped",
  );
});

test("companyProfileHtml: a quote/apostrophe in an id-bearing attribute position stays inert (jsAttr)", () => {
  const evilId = "c1'-alert(1)-'";
  const company = { ...scoredCompany, company_id: evilId };
  const html = companyProfileHtml(company, [], {
    t,
    reviewStatus: "pending",
    counts,
  });
  // jsAttr must neutralize the quote so the onclick string can't be broken out of.
  assert.ok(!html.includes(`reviewCompany('${evilId}'`));
});

// --- companies table row builder (U8, DHA-392) -------------------------------
//
// _buildRow dispatches on state.companySubTab to _buildApprovedRow /
// _buildPendingRow / _buildArchivedRow. All three take a plain company object
// and read no DOM, so they're directly testable like companyProfileHtml above.

const tableCompany = {
  name: "GiveWell",
  slug: "givewell",
  company_id: "c1",
  org_color: ["#0B5F44", "#E3F2EB"],
  calculated_tier: "S",
  alignment_score: 82,
  applyable_count: 3,
  liked_count: 2,
  new_count: 1,
  offices: "Oakland, Remote",
  last_fetched: "2026-07-01T00:00:00Z",
  fit_dimensions: {
    mission_authenticity: 70, // boundary: good
    domain_desirability: 69, // boundary: moderate
    breadth_rotation: 50, // boundary: moderate
    builder_stage: 49, // boundary: weak
    career_entry_value: null, // missing -> dash
    // money_stability, culture_fit intentionally absent (also -> dash)
  },
};

test("_dimNumHtml: boundary values land in the shared 70/50 qualityBand classes", () => {
  assert.ok(_dimNumHtml(70).includes('class="ct-dim-num q-good"'));
  assert.ok(_dimNumHtml(69).includes('class="ct-dim-num q-moderate"'));
  assert.ok(_dimNumHtml(50).includes('class="ct-dim-num q-moderate"'));
  assert.ok(_dimNumHtml(49).includes('class="ct-dim-num q-weak"'));
});

test("_dimNumHtml: a missing dimension renders a quiet dash, never a zero", () => {
  const html = _dimNumHtml(null);
  assert.ok(html.includes('class="ct-dim-empty"'));
  assert.ok(!html.includes(">0<"));
});

test("_buildRow (approved tab): renders the monogram, tier badge, WANT dims, and Open column", () => {
  state.companySubTab = "approved";
  const html = _buildRow(tableCompany);
  assert.ok(html.includes('class="ct-mono"'), "row monogram missing");
  assert.ok(
    html.includes('class="vac-tier tier-s">S</span>'),
    "tier badge should use the shared tierClass, not the retired .ctier-* classes",
  );
  assert.ok(!html.includes("ctier-"), "retired .ctier-* class leaked back in");
  assert.ok(
    html.includes('class="ct-td ct-col-open">3</td>'),
    "Open column should surface applyable_count",
  );
  // 4 present dims should be colored; career_entry_value (explicit null) plus
  // the 2 absent keys (money/culture) should dash — 3 total.
  const dimEmptyCount = (html.match(/ct-dim-empty/g) || []).length;
  assert.equal(dimEmptyCount, 3);
});

test("_buildRow (pending tab): still renders the monogram alongside the hot-vacancy badge", () => {
  state.companySubTab = "pending";
  const pending = {
    ...tableCompany,
    vacancy_count: 4,
    hot_vacancy: { score: 91, deadline_label: null },
  };
  const html = _buildRow(pending);
  assert.ok(html.includes('class="ct-mono"'));
  assert.ok(html.includes("ct-hot-badge"));
  state.companySubTab = "approved";
});

test("_buildRow (archived tab): still renders the monogram, no crash without org_color", () => {
  state.companySubTab = "archived";
  const archived = {
    name: "Old Org",
    slug: "old-org",
    status_reason: "closed",
  };
  const html = _buildRow(archived);
  assert.ok(html.includes('class="ct-mono"'));
  assert.ok(html.includes("ct-row--archived"));
  state.companySubTab = "approved";
});

test("_buildRow: an XSS payload in the company name is inert in both text and the monogram's style attribute", () => {
  const payload = '"><script>window.pwned=1</script>';
  const evil = {
    ...tableCompany,
    name: payload,
    org_color: [`red;"><script>window.pwned=1</script>`],
  };
  state.companySubTab = "approved";
  const html = _buildRow(evil);
  assert.ok(!html.includes("<script>"), "raw <script> leaked into the row");
  assert.ok(
    html.includes("&lt;script&gt;"),
    "payload should be HTML-escaped, not dropped",
  );
  // The monogram's inline style="background:..." must not let the payload
  // break out of the attribute.
  assert.ok(!/style="background:[^"]*<script>/.test(html));
});
