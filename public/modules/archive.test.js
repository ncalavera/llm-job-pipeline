// archive.js — the Archive tab's filter + card rendering. The functions touch
// the DOM (getElementById on the search box, org <select>, results count, grid)
// and read archivedGroups off state.js, which destructures window.VACANCY_DATA
// at import. So we install a minimal-but-faithful DOM shim and seed the same
// archived_groups array reference the module binds, then drive the real
// initArchive / renderArchive exactly as the tab's markup does (switchMode →
// initArchive; the onchange/oninput handlers → renderArchive).
//
// Two regressions are pinned here:
//   1. The org filter opens on "All companies" (value ""), not a single org.
//   2. A single malformed archived row must not blank the whole grid — the
//      header count and the number of rendered cards must stay consistent.

import { test } from "node:test";
import assert from "node:assert/strict";

// --- Faithful-enough <select> / element shims ------------------------------

class FakeOption {
  constructor() {
    this.value = "";
    this.textContent = "";
  }
}

class FakeSelect {
  constructor() {
    this.options = [];
    this._explicit = undefined; // set only through `.value = ...`
  }
  set innerHTML(html) {
    // initArchive writes exactly one leading <option value="...">…</option>.
    this.options = [];
    const m = html.match(/<option value="([^"]*)"[^>]*>(.*?)<\/option>/);
    if (m) {
      const o = new FakeOption();
      o.value = m[1];
      o.textContent = m[2];
      this.options.push(o);
    }
    // Replacing the option set drops any prior selection — the browser selects
    // the new first option (value "").
    this._explicit = undefined;
  }
  appendChild(o) {
    this.options.push(o);
    return o;
  }
  get value() {
    if (this._explicit !== undefined) return this._explicit;
    return this.options.length ? this.options[0].value : "";
  }
  set value(v) {
    this._explicit = v;
  }
}

const sel = new FakeSelect();
const grid = { innerHTML: "" };
const count = { textContent: "" };
const search = { value: "" };
const byId = {
  archiveOrgFilter: sel,
  archiveGrid: grid,
  archiveResultsCount: count,
  archiveSearch: search,
};

// The same array reference archive.js will bind via state.js. Tests mutate its
// CONTENTS (never reassign) so the module keeps seeing fresh rows.
const rows = [];

globalThis.document = {
  getElementById: (id) => byId[id] || null,
  createElement: () => new FakeOption(),
};
globalThis.window = {
  VACANCY_DATA: {
    config: { i18n: {}, i18n_all: null, language: "en" },
    stats: {},
    vacancy_ids: [],
    groups: [],
    companies: [],
    triage_reviews: [],
    archived_groups: rows,
  },
};
globalThis.location = { protocol: "file:", origin: "" };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };

const { initArchive, renderArchive } = await import("./archive.js");

// --- Synthetic archived rows in the shipped _build_group shape -------------

function archivedRow(id, org, extra = {}) {
  return Object.assign(
    {
      id,
      org,
      title: org + " — Role",
      org_url: "https://ex.org",
      region: "europe",
      llm_score: 42,
      scored_by: "strong",
      screen_only_score: false,
      llm_summary: "summary",
      llm_reasoning: "",
      llm_hard_requirements: [],
      us_eligibility: "",
      snippet: "",
      full_description: "",
      compensation: "",
      deadline: "",
      first_seen: "2026-05-01",
      last_seen: "2026-05-20",
      org_color: ["#F97316", "#FFF7ED"],
      locations: [
        {
          location: "Berlin, Germany",
          url: "https://ex.org/1",
          work_mode: "",
          region: "europe",
          city: "Berlin",
          country: "Germany",
        },
      ],
      member_ids: [],
      company_id: "c-" + id,
      application: null,
      calculated_tier: null,
      company_slug: null,
      company_name: null,
    },
    extra,
  );
}

function setRows(next) {
  rows.length = 0;
  rows.push(...next);
}

const cardCount = () => (grid.innerHTML.match(/archive-card/g) || []).length;

// --- Tests -----------------------------------------------------------------

test("archive opens on All companies and renders every row", () => {
  setRows([
    archivedRow("v0", "9 Mothers YC P26"), // sorts first (leading digit)
    archivedRow("v1", "Acme"),
    archivedRow("v2", "Beta"),
  ]);
  sel.value = "prev-stale-org"; // a value a reload/bfcache might have restored
  initArchive();

  assert.equal(sel.value, "", "filter must open on All companies, not one org");
  assert.equal(count.textContent, "3 of 3 vacancies");
  assert.equal(cardCount(), 3, "every archived row renders a card");
});

test("one malformed row does not blank the grid (count matches rendered cards)", () => {
  const bad = archivedRow("bad", "Broken Co");
  // A field that throws when read — stands in for any unforeseen corruption a
  // stored snapshot row might carry. buildArchiveCard must not take the grid
  // down with it.
  Object.defineProperty(bad, "title", {
    get() {
      throw new Error("corrupt row");
    },
    enumerable: true,
  });
  setRows([archivedRow("g1", "Alpha"), bad, archivedRow("g2", "Gamma")]);

  sel.value = ""; // All companies
  grid.innerHTML = "STALE"; // pretend the grid already had content

  assert.doesNotThrow(() => renderArchive());
  assert.equal(count.textContent, "3 of 3 vacancies");
  assert.equal(
    cardCount(),
    3,
    "all three rows render — the bad one falls back",
  );
  assert.ok(
    grid.innerHTML.includes("Broken Co"),
    "fallback shows the org name",
  );
});

test("a single malformed match still yields a card, never a blank grid", () => {
  const bad = archivedRow("only", "Solo Broken");
  Object.defineProperty(bad, "org_color", {
    get() {
      throw new Error("corrupt color");
    },
    enumerable: true,
  });
  setRows([archivedRow("g1", "Alpha"), bad]);

  sel.value = "Solo Broken"; // the exact single-org-filter case from the bug
  renderArchive();

  assert.equal(count.textContent, "1 of 2 vacancies");
  assert.equal(cardCount(), 1, "the one match renders instead of a blank grid");
  assert.notEqual(grid.innerHTML.trim(), "");
});
