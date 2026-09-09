// catalog.test.js — the daily review screen's pure parts: row assembly, the
// section split, the window, and the key map.
//
// catalog.js imports state.js, which reads window.VACANCY_DATA at import time,
// so a minimal browser shell goes up before the dynamic import (mirrors
// vacancy.test.js / today.test.js).

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
  reviewRowHtml,
  sectionHeadHtml,
  reviewItems,
  openCatalogRow,
  catalogVisibility,
  REVIEW_KEYS,
  REVIEW_WINDOW,
} = await import("./catalog.js");
const { reviewSections, daysToDeadline, topConflict, requirementFacts } =
  await import("./review-batches.js");

const baseGroup = {
  id: "g1",
  org: "GiveWell",
  company_name: "GiveWell",
  title: "Research Analyst",
  llm_score: 82,
  member_ids: ["m1"],
  locations: [{ location: "Remote" }],
  source_board: "80,000 Hours",
  first_seen: "2020-01-01",
};

// A role carrying prepared facts with one required, conflicting requirement.
const conflicted = {
  ...baseGroup,
  id: "g2",
  llm_score: 20,
  screening_state: "ready",
  posting_fingerprint: "p",
  screening_fingerprint: "p:f",
  screening: {
    posting_facts: {
      requirements: [
        {
          kind: "language",
          strength: "required",
          value: "Dutch, C1",
          quote: "Fluent Dutch at C1 level is required.",
        },
      ],
    },
    profile_comparison: [
      { requirement: 0, finding: "possible_conflict", note: "Needs Dutch, C1" },
    ],
  },
};

// --- the seven fields -----------------------------------------------------

test("a row carries title, company, score, deadline, conflict, source and age", () => {
  const html = reviewRowHtml(
    { ...baseGroup, deadline: futureDay(4) },
    "unseen",
    {},
  );
  assert.match(html, /Research Analyst/);
  assert.match(html, /GiveWell/);
  assert.match(html, /review-score q-good-bg"[^>]*>.*?>score<\/span>82/s);
  assert.match(html, /in 4 days/);
  assert.match(html, /No conflict found/);
  assert.match(html, /80,000 Hours/);
  assert.match(html, /review-cell--age/);
});

test("no deadline says so instead of leaving the cell blank", () => {
  const html = reviewRowHtml(baseGroup, "unseen", {});
  assert.match(html, /review-deadline--none">no deadline/);
});

test("an unscored role shows a dash, never a fabricated number", () => {
  const html = reviewRowHtml({ ...baseGroup, llm_score: null }, "unseen", {});
  assert.match(html, /review-score vac-score--none"[^>]*>.*?>score<\/span>—/s);
});

test("the top conflict is a short phrase built from the requirement itself", () => {
  const html = reviewRowHtml(conflicted, "unseen", { fingerprint: "f" });
  // The requirement's own value, not the model's free-text note. The kind and
  // the note stay in the hover title and in the expanded panel.
  assert.match(html, /review-conflict" title="Language — Dutch, C1 — Needs Dutch, C1"/);
  assert.match(html, />Dutch, C1</);
});

test("a long requirement is cut at a word, never mid-word", () => {
  const g = structuredClone(conflicted);
  g.screening.posting_facts.requirements[0].value =
    "eight or more years of experience running large international programmes";
  const html = reviewRowHtml(g, "unseen", { fingerprint: "f" });
  const value = g.screening.posting_facts.requirements[0].value;
  const shown = /review-conflict"[^>]*>([^<]*)</.exec(html)[1];
  assert.ok(shown.length <= 46, shown);
  assert.match(shown, /…$/);
  // What survives is a whole-word prefix of the requirement: the next
  // character in the original is a space, so no word was cut in half.
  const kept = shown.replace(/…$/, "");
  assert.ok(value.startsWith(kept), kept);
  assert.equal(value[kept.length], " ");
});

test("no conflict reads as a plain phrase, not an alarm", () => {
  const html = reviewRowHtml(baseGroup, "unseen", {});
  assert.match(html, /review-conflict--none">No conflict found</);
});

test("the row leads with the model's suggestion, as a pill", () => {
  const html = reviewRowHtml(baseGroup, "unseen", { defaultStatus: "passed" });
  assert.match(html, /review-cell--default"><span class="review-pill review-pill--passed">Pass</);
  // With no suggestion the column is present and empty, ready for real values.
  assert.match(
    reviewRowHtml(baseGroup, "unseen", {}),
    /review-cell--default"><span class="review-default-none">—</,
  );
});

test("a per-role suggestion outranks the batch's, when one exists", () => {
  const html = reviewRowHtml({ ...baseGroup, batch_default: "liked" }, "unseen", {
    defaultStatus: "passed",
  });
  assert.match(html, /review-pill--liked/);
});

// --- decisions -------------------------------------------------------------

test("an undecided row offers all three decisions at least 44px each", () => {
  const html = reviewRowHtml(baseGroup, "unseen", {});
  assert.match(html, /data-decide="like"/);
  assert.match(html, /data-decide="pass"/);
  assert.match(html, /data-decide="unsure"/);
});

test("the decision a row already holds is not offered again", () => {
  assert.doesNotMatch(
    reviewRowHtml(baseGroup, "liked", {}),
    /data-decide="like"/,
  );
  assert.doesNotMatch(
    reviewRowHtml(baseGroup, "passed", {}),
    /data-decide="pass"/,
  );
  assert.match(reviewRowHtml(baseGroup, "liked", {}), /data-decide="pass"/);
  assert.match(reviewRowHtml(baseGroup, "passed", {}), /data-decide="like"/);
});

test("every decision key maps to one of the three decisions", () => {
  assert.deepEqual([...new Set(Object.values(REVIEW_KEYS))].sort(), [
    "like",
    "pass",
    "unsure",
  ]);
  assert.equal(REVIEW_KEYS.L, "like");
  assert.equal(REVIEW_KEYS.P, "pass");
  assert.equal(REVIEW_KEYS.S, "unsure");
});

// --- expansion -------------------------------------------------------------

test("expanding a row shows every quoted requirement in three levels", () => {
  const html = reviewRowHtml(conflicted, "unseen", {
    expanded: true,
    fingerprint: "f",
  });
  // The label says what the posting asks, not the raw enum value.
  assert.match(html, /review-fact-label">Must have · language</);
  assert.match(html, /review-finding--conflict">Possible conflict</);
  assert.match(html, /<blockquote>Fluent Dutch at C1 level is required\./);
  // The panel is a sibling of the row, so the row keeps its own height.
  assert.match(html, /<\/div><div class="review-expand" id="expand-g2">/);
  assert.match(html, /aria-expanded="true" aria-controls="expand-g2"/);
  assert.match(html, /review-row--expanded/);
});

test("the panel carries the posting facts the row has no room for", () => {
  const html = reviewRowHtml(conflicted, "unseen", {
    expanded: true,
    fingerprint: "f",
  });
  assert.match(html, /Posting details/);
  for (const term of ["Location", "Deadline", "Kind of contract", "Source"])
    assert.match(html, new RegExp("<dt>" + term + "</dt>"));
  // Nothing is left blank and nothing shows a raw "unknown".
  assert.doesNotMatch(html, /<dd>unknown<\/dd>/);
  assert.doesNotMatch(html, /<dd><\/dd>/);
});

test("the way out of the panel is a link, not a lone button row", () => {
  const html = reviewRowHtml(conflicted, "unseen", {
    expanded: true,
    fingerprint: "f",
  });
  assert.match(html, /review-expand-link" data-open="g2">Open the full role page/);
  assert.doesNotMatch(html, /class="review-open"/);
});

test("required conditions are read before preferred ones", () => {
  const g = structuredClone(conflicted);
  g.screening.posting_facts.requirements.unshift({
    kind: "skill",
    strength: "preferred",
    value: "SQL",
    quote: "SQL is a plus.",
  });
  g.screening.profile_comparison = [
    { requirement: 1, finding: "possible_conflict", note: "Needs Dutch, C1" },
  ];
  const html = reviewRowHtml(g, "unseen", { expanded: true, fingerprint: "f" });
  assert.ok(html.indexOf("Must have") < html.indexOf("Preferred"));
});

test("a collapsed row renders no panel", () => {
  assert.doesNotMatch(reviewRowHtml(conflicted, "unseen", {}), /review-expand/);
});

test("requirementFacts drops a requirement with no quote", () => {
  const g = {
    screening: {
      posting_facts: { requirements: [{ kind: "skill", value: "SQL" }] },
      profile_comparison: [],
    },
  };
  assert.deepEqual(requirementFacts(g), []);
});

// --- sections --------------------------------------------------------------

test("roles without a batch land in one trailing section", () => {
  const sections = reviewSections([baseGroup], "f");
  assert.equal(sections.length, 1);
  assert.equal(sections[0].key, "unbatched");
  assert.equal(sections[0].defaultStatus, null);
});

test("a batch keeps its proposed default and the unbatched rest comes last", () => {
  const sections = reviewSections([conflicted, baseGroup], "f");
  assert.deepEqual(
    sections.map((s) => s.key),
    ["eligibility", "unbatched"],
  );
  assert.equal(sections[0].defaultStatus, "passed");
  // Batches are numbered in reading order; the unbatched rest is not a batch.
  assert.equal(sections[0].number, 1);
  assert.equal(sections[1].number, undefined);
});

test("every section, batched or not, is ordered by its nearest deadline", () => {
  const far = { ...conflicted, id: "far", deadline: futureDay(30) };
  const near = { ...baseGroup, id: "near", deadline: futureDay(2) };
  // The unbatched roles come first when theirs is the nearer deadline: the top
  // of the list is the strongest position, and it belongs to what is urgent,
  // not to the batch the screen proposes to discard.
  assert.deepEqual(
    reviewSections([far, near], "f").map((s) => s.key),
    ["unbatched", "eligibility"],
  );
  const nearBatch = { ...conflicted, id: "nb", deadline: futureDay(1) };
  const farRest = { ...baseGroup, id: "fr", deadline: futureDay(20) };
  assert.deepEqual(
    reviewSections([farRest, nearBatch], "f").map((s) => s.key),
    ["eligibility", "unbatched"],
  );
});

test("a section header names the count and the nearest deadline", () => {
  const html = sectionHeadHtml({
    key: "eligibility",
    title: "Location or language",
    note: "a reason",
    defaultStatus: "passed",
    rows: [{ ...baseGroup, deadline: futureDay(2) }],
  });
  assert.match(html, /Location or language/);
  assert.match(html, /1 role · nearest deadline in 2 days/);
  assert.match(html, /Model default:<\/span><span class="review-pill review-pill--passed">Pass/);
  assert.match(html, /data-accept="eligibility"/);
  assert.match(html, /Accept defaults for batch<span class="review-key-cap">A</);
});

test("a section with no proposed default offers no accept button", () => {
  const html = sectionHeadHtml({
    key: "unbatched",
    title: "New, not batched",
    note: "",
    defaultStatus: null,
    rows: [baseGroup],
  });
  assert.doesNotMatch(html, /data-accept=/);
});

// --- the render window -----------------------------------------------------

test("reviewItems interleaves one header per section with its rows", () => {
  const items = reviewItems([conflicted, baseGroup], "f");
  assert.deepEqual(
    items.map((i) => i.type),
    ["head", "row", "head", "row"],
  );
});

test("the window is smaller than a full inbox, so the first paint is bounded", () => {
  assert.ok(REVIEW_WINDOW > 0 && REVIEW_WINDOW <= 100);
  const rows = Array.from({ length: 300 }, (_, i) => ({
    ...baseGroup,
    id: "g" + i,
  }));
  assert.equal(reviewItems(rows, "f").length, 301);
});

// --- deadlines -------------------------------------------------------------

test("daysToDeadline counts whole days and returns null without a deadline", () => {
  const today = new Date("2026-09-09T12:00:00Z");
  assert.equal(daysToDeadline({ deadline: "2026-09-11" }, today), 2);
  assert.equal(daysToDeadline({ deadline: "2026-09-08" }, today), -1);
  assert.equal(daysToDeadline({}, today), null);
  assert.equal(daysToDeadline({ deadline: "soon" }, today), null);
});

// --- the click contract ----------------------------------------------------

test("row click opens the vacancy detail via openCatalogRow", () => {
  const html = reviewRowHtml(baseGroup, "unseen", {});
  assert.match(html, /class="review-row" data-id="g1"/);
  assert.match(html, /openCatalogRow\('g1'\)/);
  assert.match(html, /role="button" tabindex="0"/);
});

test("openCatalogRow forwards id + browse context + the given queue", () => {
  let called = null;
  globalThis.window.openVacancyRoute = (id, o) => {
    called = { id, opts: o };
  };
  openCatalogRow("g7", ["g5", "g7", "g9"]);
  assert.deepEqual(called, {
    id: "g7",
    opts: { context: "browse", queue: ["g5", "g7", "g9"] },
  });
});

// --- escaping --------------------------------------------------------------

const xssGroup = {
  ...baseGroup,
  id: "g\"'></div><script>1</script>",
  title: "<img src=x onerror=alert(1)>",
  company_name: '"><svg onload=alert(1)>',
  source_board: "<i>board</i>",
};

test("title, company and source are escaped in text positions", () => {
  const html = reviewRowHtml(xssGroup, "unseen", {});
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /<svg onload/);
  assert.doesNotMatch(html, /<i>board<\/i>/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test("an id with quotes is escaped in data-id AND in the onclick attribute", () => {
  const html = reviewRowHtml(xssGroup, "unseen", {});
  assert.doesNotMatch(html, /data-id="g"'/);
  assert.match(html, /data-id="g&quot;/);
  assert.doesNotMatch(html, /openCatalogRow\('g"'\)/);
});

test("a conflict quote is escaped inside the title attribute", () => {
  const g = structuredClone(conflicted);
  g.screening.posting_facts.requirements[0].quote = '"><script>1</script>';
  const html = reviewRowHtml(g, "unseen", { fingerprint: "f" });
  assert.doesNotMatch(html, /<script>1<\/script>/);
});

// --- visibility ------------------------------------------------------------

test("the screen opens on the canvas's score band, not on everything", async () => {
  const { groupsInBasket, basketCounts } = await import("./derive.js");
  const visibility = catalogVisibility();
  assert.equal(visibility.minScore, 40);
  // A role under the band is out of the opening list, and out of its count, so
  // the badge and the list still agree.
  const rows = [
    { ...baseGroup, id: "strong", llm_score: 70 },
    { ...baseGroup, id: "weak", llm_score: 12 },
    { ...baseGroup, id: "unscored", llm_score: null },
  ];
  assert.deepEqual(
    groupsInBasket(rows, "unseen", visibility).map((g) => g.id),
    ["strong"],
  );
  assert.equal(basketCounts(rows, visibility).unseen, 1);
});

test("topConflict returns null when the facts name no conflict", () => {
  assert.equal(topConflict(baseGroup, "f"), null);
});

/** A YYYY-MM-DD n days from today, in the same UTC frame daysToDeadline uses. */
function futureDay(n) {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
