// stats.js — the Geo table. renderStats aggregates the visible vacancy set by
// place and renders a click-to-filter table; filterCatalogByLocation rides the
// existing switchToCatalog seam. state.js reads window.VACANCY_DATA at import,
// so we seed a minimal shell (with a couple of located groups) before importing
// — same shape as archive.test.js / today.test.js.

import { test } from "node:test";
import assert from "node:assert/strict";

// A city group and a remote group, seeded into the SAME array reference
// state.js destructures at import. No company_id → approved by default
// (isGroupCompanyApproved's legacy path); llm_score clears the 40 floor.
const seededGroups = [
  {
    id: "gc",
    org: "Alpha",
    company_name: "Alpha",
    title: "Analyst",
    llm_score: 70,
    locations: [
      { location: "Berlin, Germany", city: "Berlin", country: "Germany" },
    ],
    member_ids: [],
  },
  {
    id: "gr",
    org: "Beta",
    company_name: "Beta",
    title: "Engineer",
    llm_score: 70,
    // No city/country in any location → the Remote/Unknown bucket.
    locations: [{ location: "Remote", city: "", country: "" }],
    member_ids: [],
  },
];

const statsSection = { innerHTML: "" };
const byId = { statsSection };

globalThis.document = {
  getElementById: (id) => byId[id] || null,
};
globalThis.window = {
  VACANCY_DATA: {
    config: { i18n: {}, i18n_all: null, language: "en" },
    stats: {},
    vacancy_ids: [],
    groups: seededGroups,
    companies: [],
    triage_reviews: [],
    archived_groups: [],
  },
};
globalThis.location = { protocol: "file:", origin: "" };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };

const { renderStats, filterCatalogByLocation } = await import("./stats.js");
const { on } = await import("./state.js");

// --- filterCatalogByLocation: the emit contract ----------------------------

test("filterCatalogByLocation emits switchToCatalog with the location term", () => {
  let payload = null;
  on("switchToCatalog", (p) => {
    payload = p;
  });
  filterCatalogByLocation("Berlin");
  assert.deepEqual(payload, { locSearch: "Berlin" });
});

// --- renderStats: rows are click-to-filter + keyboard-operable --------------

test("a city row is a keyboard-operable button that filters Browse by that city", () => {
  statsSection.innerHTML = "";
  renderStats();
  const html = statsSection.innerHTML;
  assert.ok(html.includes('role="button"'), "rows are buttons");
  assert.ok(
    html.includes("filterCatalogByLocation('Berlin')"),
    "clicking the Berlin row filters Browse to Berlin",
  );
  assert.ok(
    html.includes("event.key==='Enter'") && html.includes("event.key===' '"),
    "Enter/Space activate the row (R12 keyboard access)",
  );
});

test("the Remote/Unknown row filters Browse by the remote location term", () => {
  statsSection.innerHTML = "";
  renderStats();
  assert.ok(
    statsSection.innerHTML.includes("filterCatalogByLocation('Remote')"),
    "the remote bucket uses the 'Remote' filter term (matches remote roles)",
  );
});
