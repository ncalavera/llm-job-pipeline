// catalog.js — pure row assembly + click contract for Browse (U5, DHA-389).
//
// catalog.js imports state.js, which reads window.VACANCY_DATA at import
// time — so a minimal browser shell goes up before the dynamic import
// (mirrors vacancy.test.js / today.test.js). catalogRowHtml/catalogQueueIds
// take their own basket/opts, so no baked i18n or live groups are needed to
// test them (KTD2: pure assembly, thin DOM shell).

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

const { catalogQueueIds, catalogRowHtml, openCatalogRow } =
  await import("./catalog.js");
const { actionsFor } = await import("./keys.js");

const t = (key, fallback) => fallback;
const opts = { t, locale: "en-US" };

const baseGroup = {
  id: "g1",
  org: "GiveWell",
  company_name: "GiveWell",
  calculated_tier: "S",
  title: "Research Analyst",
  llm_score: 82,
  llm_summary: "Short summary of the role.",
  member_ids: ["m1"],
  locations: [{ location: "Remote" }],
  compensation: "$90k–$110k",
  first_seen: "2020-01-01",
};

// --- catalogQueueIds ---------------------------------------------------

test("catalogQueueIds returns ids in the given (already sorted) order", () => {
  const rows = [{ id: "b" }, { id: "a" }, { id: "c" }];
  assert.deepEqual(catalogQueueIds(rows), ["b", "a", "c"]);
});

test("catalogQueueIds on an empty list", () => {
  assert.deepEqual(catalogQueueIds([]), []);
});

// --- catalogRowHtml: action-button gating per basket --------------------

test("unseen basket: like + pass both render", () => {
  const html = catalogRowHtml(baseGroup, "unseen", opts);
  assert.match(html, /catalog-row-btn like/);
  assert.match(html, /catalog-row-btn pass/);
});

test("liked basket: only pass renders", () => {
  const html = catalogRowHtml(baseGroup, "liked", opts);
  assert.doesNotMatch(html, /catalog-row-btn like/);
  assert.match(html, /catalog-row-btn pass/);
});

test("passed basket: only like renders", () => {
  const html = catalogRowHtml(baseGroup, "passed", opts);
  assert.match(html, /catalog-row-btn like/);
  assert.doesNotMatch(html, /catalog-row-btn pass/);
});

// keys.js:actionsFor drives the keyboard l/x gating; it MUST mirror the thumb
// buttons catalogRowHtml renders, or a keyboard key fires where no button
// exists (e.g. a to_apply role in the Liked tab). Pin the mirror for all 9
// statuses so the two can't drift apart.
test("actionsFor mirrors catalogRowHtml button gating for every status", () => {
  const statuses = [
    "unseen",
    "liked",
    "passed",
    "to_apply",
    "to_research",
    "to_network",
    "applied",
    "expiring",
    "skipped",
  ];
  for (const status of statuses) {
    const html = catalogRowHtml(baseGroup, status, opts);
    const showsLike = /catalog-row-btn like/.test(html);
    const showsPass = /catalog-row-btn pass/.test(html);
    const a = actionsFor(status);
    assert.equal(a.like, showsLike, `like mismatch for status "${status}"`);
    assert.equal(a.pass, showsPass, `pass mismatch for status "${status}"`);
  }
});

// --- catalogRowHtml: click contract --------------------------------------

test("row click opens the vacancy via openCatalogRow with the row's id", () => {
  const html = catalogRowHtml(baseGroup, "unseen", opts);
  assert.match(html, /class="catalog-row" data-id="g1"/);
  assert.match(html, /onclick="openCatalogRow\('g1'\)"/);
});

test("like/pass buttons stop propagation so they never trigger the row click", () => {
  const html = catalogRowHtml(baseGroup, "unseen", opts);
  assert.match(
    html,
    /onclick="event\.stopPropagation\(\);catalogThumbAction\('g1',\[&quot;m1&quot;\],'like'\)"/,
  );
  assert.match(
    html,
    /onclick="event\.stopPropagation\(\);catalogThumbAction\('g1',\[&quot;m1&quot;\],'pass'\)"/,
  );
});

test("openCatalogRow forwards id + browse context + the given queue to the router", () => {
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

// --- catalogRowHtml: absent fields render a dash, never an empty label --

test("no compensation/location/first_seen -> dash placeholders, not blank", () => {
  const html = catalogRowHtml(
    { ...baseGroup, compensation: null, locations: [], first_seen: null },
    "unseen",
    opts,
  );
  assert.match(html, /<div class="catalog-row-loc">—<\/div>/);
  assert.match(html, /<div class="catalog-row-comp">—<\/div>/);
  assert.match(html, /<div class="catalog-row-seen">—<\/div>/);
});

test("no summary/snippet -> no subline element at all", () => {
  const html = catalogRowHtml(
    { ...baseGroup, llm_summary: null, snippet: null },
    "unseen",
    opts,
  );
  assert.doesNotMatch(html, /catalog-row-sub/);
});

test("no deadline -> no deadline pill in the title row", () => {
  const html = catalogRowHtml(baseGroup, "unseen", opts);
  assert.doesNotMatch(html, /card-deadline/);
});

test("a deadline renders the shared card-deadline pill", () => {
  const html = catalogRowHtml(
    { ...baseGroup, deadline: "2099-01-01" },
    "unseen",
    opts,
  );
  assert.match(html, /card-deadline/);
});

test("no score -> the neutral tile, not a fabricated number", () => {
  const html = catalogRowHtml(
    { ...baseGroup, llm_score: null },
    "unseen",
    opts,
  );
  assert.match(html, /catalog-row-score vac-score--none">—/);
});

test("score bands map to the shared quality classes", () => {
  assert.match(
    catalogRowHtml({ ...baseGroup, llm_score: 82 }, "unseen", opts),
    /catalog-row-score q-good-bg/,
  );
  assert.match(
    catalogRowHtml({ ...baseGroup, llm_score: 55 }, "unseen", opts),
    /catalog-row-score q-moderate-bg/,
  );
  assert.match(
    catalogRowHtml({ ...baseGroup, llm_score: 10 }, "unseen", opts),
    /catalog-row-score q-weak-bg/,
  );
});

// --- catalogRowHtml: escaping regression, including attribute positions -

const xssGroup = {
  ...baseGroup,
  id: "g\"'></div><script>1</script>",
  title: "<img src=x onerror=alert(1)>",
  company_name: '"><svg onload=alert(1)>',
  llm_summary: '<b>bold</b> & "quoted"',
  calculated_tier: "S",
  locations: [{ location: "<i>Remote</i>" }],
};

test("title/org/sub are escaped in text-content positions", () => {
  const html = catalogRowHtml(xssGroup, "unseen", opts);
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /<svg onload/);
  assert.doesNotMatch(html, /<b>bold<\/b>/);
  assert.doesNotMatch(html, /<i>Remote<\/i>/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /&lt;i&gt;Remote&lt;\/i&gt;/);
});

test("an id with quotes/HTML is escaped in the data-id AND the onclick attribute", () => {
  const html = catalogRowHtml(xssGroup, "unseen", opts);
  // data-id is HTML-escaped (escHtml) — the raw quote/tag never reaches the attribute.
  assert.doesNotMatch(html, /data-id="g"'/);
  assert.match(html, /data-id="g&quot;/);
  // the onclick's single-quoted JS string is jsAttr-escaped — no unescaped ' breaks out.
  assert.doesNotMatch(html, /openCatalogRow\('g"'\)/);
});
