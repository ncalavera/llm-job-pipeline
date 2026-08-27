// reports.test.js — the Reports section: grouping, ordering, the markdown a
// report is rendered with, and the escaping every stored document must survive.
//
// reports.js imports state.js, which reads window.VACANCY_DATA at import time,
// so a minimal browser shell goes up first (mirrors today.test.js).

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
  REPORT_KIND_ORDER,
  REPORT_KIND_LABELS,
  groupReports,
  formatReportDate,
  buildReportRow,
  buildReportsList,
  buildReportDetail,
} = await import("./reports.js");

const { mdToHtml, headingSlug } = await import("./helpers.js");

function report(over) {
  return Object.assign(
    {
      slug: "ea-funding-2026",
      title: "EA Funding Landscape 2026",
      kind: "research",
      excerpt: "Three funders matter.",
      body_md: "# EA Funding Landscape 2026\n\nThree funders matter.",
      source_path: "research/sectors/ea-funding-2026.md",
      updated_at: "2026-08-17T09:00:00Z",
      created_at: "2026-08-01T09:00:00Z",
    },
    over,
  );
}

// ---------------------------------------------------------------------------
// Grouping and order
// ---------------------------------------------------------------------------

test("reports group by kind, in the section's own order", () => {
  const groups = groupReports([
    report({ slug: "a", kind: "other" }),
    report({ slug: "b", kind: "research" }),
    report({ slug: "c", kind: "grant" }),
  ]);
  assert.deepEqual(
    groups.map((g) => g.kind),
    ["research", "grant", "other"],
  );
});

test("an empty group is dropped, not shown as an empty heading", () => {
  // A heading over nothing reads as a section that failed to load.
  const groups = groupReports([report({ kind: "research" })]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].kind, "research");
});

test("within a group, the newest report comes first", () => {
  const groups = groupReports([
    report({ slug: "may", updated_at: "2026-05-04T09:00:00Z" }),
    report({ slug: "aug", updated_at: "2026-08-17T09:00:00Z" }),
    report({ slug: "jun", updated_at: "2026-06-21T09:00:00Z" }),
  ]);
  assert.deepEqual(
    groups[0].reports.map((r) => r.slug),
    ["aug", "jun", "may"],
  );
});

test("a report with no updated_at falls back to when it was created", () => {
  const groups = groupReports([
    report({ slug: "older", updated_at: null, created_at: "2026-07-01T09:00:00Z" }),
    report({ slug: "newer", updated_at: "2026-08-01T09:00:00Z" }),
  ]);
  assert.deepEqual(
    groups[0].reports.map((r) => r.slug),
    ["newer", "older"],
  );
});

test("an undated report sorts last, never first", () => {
  // Otherwise one row with no dates pushes every real report off the top.
  const groups = groupReports([
    report({ slug: "undated", updated_at: null, created_at: null }),
    report({ slug: "dated" }),
  ]);
  assert.deepEqual(
    groups[0].reports.map((r) => r.slug),
    ["dated", "undated"],
  );
});

test("a report with an unknown kind lands in Other, and is never hidden", () => {
  // Showing it in the wrong group is recoverable; silently dropping it is not.
  const groups = groupReports([report({ slug: "odd", kind: "memo" })]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].kind, "other");
  assert.equal(groups[0].reports[0].slug, "odd");
});

test("every kind in the order has a plain-word label", () => {
  for (const kind of REPORT_KIND_ORDER) {
    assert.ok(REPORT_KIND_LABELS[kind], `no label for ${kind}`);
    assert.ok(/^[A-Z]/.test(REPORT_KIND_LABELS[kind]));
  }
});

// ---------------------------------------------------------------------------
// Dates
// ---------------------------------------------------------------------------

test("a report's date carries the year, because reports are read by age", () => {
  assert.equal(formatReportDate("2026-08-17T09:00:00Z", "en-GB"), "17 Aug 2026");
  // And the day still comes first under en-US, the app's own locale.
  assert.equal(formatReportDate("2026-08-17T09:00:00Z", "en-US"), "17 Aug 2026");
});

test("a missing or unparseable date renders as nothing", () => {
  assert.equal(formatReportDate(null, "en-GB"), "");
  assert.equal(formatReportDate("not-a-date", "en-GB"), "");
});

// ---------------------------------------------------------------------------
// Markup
// ---------------------------------------------------------------------------

test("a list row is clickable and operable from the keyboard", () => {
  const html = buildReportRow(report(), { locale: "en-GB" });
  assert.ok(html.includes('role="button"'));
  assert.ok(html.includes('tabindex="0"'));
  assert.ok(html.includes("openReport('ea-funding-2026')"));
  assert.ok(html.includes("event.key==='Enter'"));
});

test("a stored title and excerpt are escaped, not executed", () => {
  const html = buildReportRow(
    report({
      title: '<img src=x onerror="alert(1)">',
      excerpt: "<script>alert(2)</script>",
    }),
    { locale: "en-GB" },
  );
  assert.ok(!html.includes("<img src=x"));
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&lt;img"));
});

test("a slug with a quote in it cannot break out of the click handler", () => {
  const html = buildReportRow(report({ slug: "it's-a-slug" }), {});
  assert.ok(!/onclick="openReport\('it's/.test(html));
});

test("an empty library says so instead of rendering nothing", () => {
  const html = buildReportsList([], {});
  assert.ok(html.includes("catalog-empty"));
  assert.ok(/vac report add/.test(html));
});

test("the list counts what it renders", () => {
  const html = buildReportsList(
    [report({ slug: "a" }), report({ slug: "b" }), report({ slug: "c" })],
    {},
  );
  assert.ok(html.includes("3 reports"));
  assert.equal((html.match(/class="report-row"/g) || []).length, 3);
});

test("one report is not counted as 'reports'", () => {
  const html = buildReportsList([report()], {});
  assert.ok(html.includes("1 report"));
  assert.ok(!html.includes("1 reports"));
});

test("a report body renders with anchored headings", () => {
  const html = buildReportDetail(report(), { locale: "en-GB" });
  assert.ok(html.includes('id="ea-funding-landscape-2026"'));
  assert.ok(html.includes('class="md-anchor"'));
  assert.ok(html.includes("report-back"));
});

test("the detail header shows kind, date and where the file came from", () => {
  const html = buildReportDetail(report(), { locale: "en-GB" });
  assert.ok(html.includes("Research"));
  assert.ok(html.includes("17 Aug 2026"));
  assert.ok(html.includes("research/sectors/ea-funding-2026.md"));
});

// ---------------------------------------------------------------------------
// The renderer a report is read through
// ---------------------------------------------------------------------------

test("a numbered list renders as an ordered list, not as loose paragraphs", () => {
  // Reports lean on these — findings, steps, ranked options. Without the
  // branch every "1." line became its own <p>, losing both the numbering and
  // the fact that the lines belong together.
  const html = mdToHtml("1. First finding\n2. Second finding\n3. Third");
  assert.ok(html.includes("<ol>"));
  assert.equal((html.match(/<li>/g) || []).length, 3);
  assert.ok(html.includes("<li>First finding</li>"));
});

test("a bulleted list and a numbered list do not merge into one", () => {
  const html = mdToHtml("- a bullet\n\n1. a number");
  assert.ok(html.includes("<ul>"));
  assert.ok(html.includes("</ul>"));
  assert.ok(html.includes("<ol>"));
});

test("a table renders with a head and a body, and its separator row is dropped", () => {
  const html = mdToHtml("| Funder | Budget |\n| --- | --- |\n| Open Phil | $600m |");
  assert.ok(html.includes("<th>Funder</th>"));
  assert.ok(html.includes("<td>Open Phil</td>"));
  assert.ok(!html.includes("---"));
});

test("headings h1 through h6 all render at their own level", () => {
  for (let level = 1; level <= 6; level++) {
    const html = mdToHtml("#".repeat(level) + " Heading");
    assert.ok(html.includes(`<h${level}`), `h${level} did not render`);
  }
});

test("anchors are off by default, so short descriptions emit no duplicate ids", () => {
  // The same renderer draws job descriptions and scoring reasoning. A page of
  // those, each with its own "## Summary", would emit one id many times over.
  const html = mdToHtml("## Summary\n\nSome text.");
  assert.ok(!html.includes("id="));
  assert.ok(!html.includes("md-anchor"));
});

test("two sections with the same name get distinct anchors", () => {
  // Otherwise the second "Risks" in a report is unreachable by link, and the
  // document is invalid HTML.
  const html = mdToHtml("## Risks\n\na\n\n## Risks\n\nb", { anchors: true });
  assert.ok(html.includes('id="risks"'));
  assert.ok(html.includes('id="risks-1"'));
});

test("a fenced code block is not re-read as headings and lists", () => {
  const html = mdToHtml("```\n# not a heading\n- not a list\n```", {
    anchors: true,
  });
  assert.ok(html.includes("<pre><code>"));
  assert.ok(!html.includes("<h1"));
  assert.ok(!html.includes("<ul>"));
});

test("an unclosed code fence still renders instead of swallowing the page", () => {
  // Half-written reports are normal. Losing the rest of the document to one
  // stray fence is not.
  const html = mdToHtml("```\nsome code that never closes");
  assert.ok(html.includes("<pre><code>"));
  assert.ok(html.includes("some code that never closes"));
});

test("markdown cannot inject markup, anchors on or off", () => {
  const evil = '# <img src=x onerror="alert(1)">\n\n<script>alert(2)</script>';
  for (const opts of [undefined, { anchors: true }]) {
    const html = mdToHtml(evil, opts);
    assert.ok(!html.includes("<img src=x"));
    assert.ok(!html.includes("<script>"));
  }
});

test("headingSlug survives punctuation, entities and non-ASCII", () => {
  assert.equal(headingSlug("Findings &amp; Risks"), "findings-risks");
  assert.equal(headingSlug("What now?"), "what-now");
  assert.equal(headingSlug("  Spaced  Out  "), "spaced-out");
  // A heading with nothing sluggable must not produce a bare "-" or "".
  assert.equal(headingSlug("###"), "");
});
