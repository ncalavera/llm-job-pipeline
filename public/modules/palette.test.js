// palette.js — ⌘K command palette logic (U17, DHA-401). Pure functions (filter,
// ranking, id-keyed selection, routing contract, escaped markup), so they
// unit-test directly under `node --test` — no browser (KTD2). palette.js imports
// only the equally-pure escHtml/qualityBand from helpers.js.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  filterPalette,
  matchRank,
  flatKeys,
  routeForResult,
  resultsHtml,
  createSelection,
  RANK_PREFIX,
  RANK_WORD,
  RANK_SUBSTRING,
} from "./palette.js";

// A small mixed fixture: vacancies (title + org, with a fit score) and
// companies (name + fit score).
function fixture() {
  return {
    vacancies: [
      { id: "v1", title: "Program Officer", org: "GiveWell", score: 80 },
      {
        id: "v2",
        title: "Senior Program Manager",
        org: "Open Phil",
        score: 90,
      },
      { id: "v3", title: "Data Analyst", org: "Program Fund", score: 60 },
    ],
    companies: [
      { slug: "givewell", name: "GiveWell", score: 88 },
      { slug: "program-inc", name: "Program Inc", score: 70 },
    ],
  };
}

// --- matchRank: prefix > word-boundary > substring, case-insensitive --------

test("matchRank ranks prefix, word-boundary and substring distinctly", () => {
  assert.equal(matchRank("Program Officer", "prog"), RANK_PREFIX);
  assert.equal(matchRank("Senior Program Manager", "program"), RANK_WORD);
  assert.equal(matchRank("Reprogram", "program"), RANK_SUBSTRING);
  assert.equal(matchRank("Program Officer", "xyz"), -1);
});

test("matchRank is case-insensitive and treats punctuation as a boundary", () => {
  assert.equal(matchRank("PROGRAM officer", "program"), RANK_PREFIX);
  assert.equal(matchRank("Data-Program", "program"), RANK_WORD); // after '-'
  assert.equal(matchRank("Analyst (Program)", "program"), RANK_WORD); // after '('
});

test("matchRank returns -1 for empty query or null field", () => {
  assert.equal(matchRank("anything", ""), -1);
  assert.equal(matchRank("anything", null), -1);
  assert.equal(matchRank(null, "x"), -1);
});

// --- filterPalette: grouping + ranking + case-insensitivity -----------------

test("empty / whitespace query yields an all-empty result (no results yet)", () => {
  for (const q of ["", "   ", null, undefined]) {
    const fr = filterPalette(q, fixture());
    assert.deepEqual(fr.vacancies, []);
    assert.deepEqual(fr.companies, []);
    assert.deepEqual(fr.flat, []);
  }
});

test("filterPalette groups matches by type and searches title, org and name", () => {
  const fr = filterPalette("program", fixture());
  // Every vacancy matches ('Program' in title or org); of the companies only
  // 'Program Inc' does, not 'GiveWell'.
  assert.equal(fr.vacancies.length, 3);
  assert.equal(fr.companies.length, 1);
  // flat is vacancies-then-companies.
  assert.deepEqual(
    fr.flat.map((r) => r.type),
    ["vacancy", "vacancy", "vacancy", "company"],
  );
});

test("filterPalette orders prefix matches before substring, then by score", () => {
  const fr = filterPalette("program", fixture());
  // v1 (prefix, title 'Program Officer') and v3 (prefix, but via title 'Data
  // Analyst'? no — 'Program Fund' is the ORG, so word/substring). Ranking:
  //   v1 title 'Program …'      -> prefix (rank 0), score 80
  //   v3 org   'Program Fund'   -> prefix (rank 0), score 60
  //   v2 title 'Senior Program' -> word   (rank 1), score 90
  // So prefix pair first (higher score wins → v1 then v3), then v2.
  assert.deepEqual(
    fr.vacancies.map((r) => r.id),
    ["v1", "v3", "v2"],
  );
});

test("filterPalette matches an org even when the title does not", () => {
  const fr = filterPalette("open phil", fixture());
  assert.deepEqual(
    fr.vacancies.map((r) => r.id),
    ["v2"],
  );
});

test("filterPalette is case-insensitive", () => {
  const lower = filterPalette("givewell", fixture());
  const upper = filterPalette("GIVEWELL", fixture());
  assert.deepEqual(
    lower.flat.map((r) => r.key),
    upper.flat.map((r) => r.key),
  );
  // A vacancy (org GiveWell) and the company both surface.
  assert.ok(lower.flat.some((r) => r.key === "v:v1"));
  assert.ok(lower.flat.some((r) => r.key === "c:givewell"));
});

test("filterPalette caps each group at opts.limit", () => {
  const many = {
    vacancies: Array.from({ length: 40 }, (_, i) => ({
      id: "v" + i,
      title: "Program " + i,
      org: "Org",
      score: i,
    })),
    companies: [],
  };
  const fr = filterPalette("program", many, { limit: 5 });
  assert.equal(fr.vacancies.length, 5);
});

test("result keys are stable and typed", () => {
  const fr = filterPalette("givewell", fixture());
  const vac = fr.vacancies[0];
  assert.equal(vac.key, "v:" + vac.id);
  const co = fr.companies[0];
  assert.equal(co.key, "c:" + co.slug);
});

// --- routing contract -------------------------------------------------------

test("routeForResult emits a vacancy route for a vacancy result", () => {
  assert.deepEqual(routeForResult({ type: "vacancy", id: "g9" }), {
    kind: "vacancy",
    id: "g9",
  });
});

test("routeForResult emits a company route for a company result", () => {
  assert.deepEqual(routeForResult({ type: "company", slug: "acme" }), {
    kind: "company",
    slug: "acme",
  });
});

test("routeForResult is null for missing/garbage input", () => {
  assert.equal(routeForResult(null), null);
  assert.equal(routeForResult({ type: "vacancy" }), null); // no id
  assert.equal(routeForResult({ type: "company" }), null); // no slug
  assert.equal(routeForResult({ type: "other", id: "x" }), null);
});

// --- id-keyed selection survives a refresh (KTD7) ---------------------------

test("reset highlights the top match; move walks and clamps", () => {
  const sel = createSelection();
  const fr = filterPalette("program", fixture());
  const keys = flatKeys(fr);
  assert.equal(sel.reset(keys), keys[0]); // top match highlighted
  assert.equal(sel.move(1, keys), keys[1]);
  assert.equal(sel.move(-1, keys), keys[0]);
  assert.equal(sel.move(-1, keys), keys[0]); // clamps at the top
});

test("KTD7: the highlighted key survives a refresh that inserts a row above", () => {
  const sel = createSelection();
  sel.set("v2", ["v1", "v2", "v3"]); // highlight v2 at index 1
  // A poll inserts a higher-scored vacancy above v2.
  assert.equal(sel.reconcile(["vNew", "v1", "v2", "v3"]), "v2");
  assert.equal(sel.index, 2); // index shifted, key (and pending Enter) did not
});

test("KTD7: when the highlighted key is gone, fall to the nearest index", () => {
  const sel = createSelection();
  sel.set("v2", ["v1", "v2", "v3"]); // index 1
  // v2 dropped out of the new results — the slot it held now holds v3.
  assert.equal(sel.reconcile(["v1", "v3"]), "v3");
  // Removing the LAST-held slot clamps up to the new last.
  const sel2 = createSelection();
  sel2.set("v3", ["v1", "v2", "v3"]);
  assert.equal(sel2.reconcile(["v1", "v2"]), "v2");
});

test("reconcile clears on an emptied list and leaves a dormant cursor dormant", () => {
  const sel = createSelection();
  sel.set("v2", ["v1", "v2"]);
  assert.equal(sel.reconcile([]), null);
  assert.equal(sel.key, null);
  const dormant = createSelection();
  assert.equal(dormant.reconcile(["a", "b"]), null);
});

test("integration: reset after a real re-filter keeps a still-present key via reconcile", () => {
  const sel = createSelection();
  const fr1 = filterPalette("program", fixture());
  sel.reset(flatKeys(fr1)); // top match
  sel.move(1, flatKeys(fr1)); // second match
  const held = sel.key;
  // Same query, but the data hot-swapped (identical here) — reconcile keeps it.
  const fr2 = filterPalette("program", fixture());
  assert.equal(sel.reconcile(flatKeys(fr2)), held);
});

// --- escaping regression: text AND attribute positions ----------------------

test("resultsHtml escapes titles/orgs/names in text and attribute positions", () => {
  const payload = {
    vacancies: [
      {
        id: 'v"1',
        title: "<img src=x onerror=alert(1)>",
        org: 'A&B "Quotes"',
        score: 75,
      },
    ],
    companies: [{ slug: "c1", name: "<script>evil()</script>", score: 60 }],
  };
  const fr = filterPalette("", payload); // empty query → nothing, so query one:
  const fr2 = filterPalette("script", payload); // matches the company name
  const html = resultsHtml(fr2, 0, {
    labelVacancies: "Vacancies",
    labelCompanies: "Companies",
  });
  // The raw payload markup never reaches the output verbatim.
  assert.ok(!html.includes("<script>evil()</script>"));
  assert.ok(html.includes("&lt;script&gt;evil()&lt;/script&gt;"));

  // And the vacancy XSS payload, escaped in both text and the title="" attr.
  const html2 = resultsHtml(filterPalette("img", payload), 0, {});
  assert.ok(!html2.includes("<img src=x"));
  assert.ok(html2.includes("&lt;img src=x onerror=alert(1)&gt;"));
  // Double-quote in the org escapes so it can't break out of an attribute.
  const html3 = resultsHtml(filterPalette("quotes", payload), 0, {});
  assert.ok(!html3.includes('"Quotes"'));
  assert.ok(html3.includes("&quot;Quotes&quot;"));
  // fr is unused beyond documenting the empty-query contract elsewhere.
  assert.deepEqual(fr.flat, []);
});

test("resultsHtml marks exactly the active option aria-selected", () => {
  const fr = filterPalette("program", fixture());
  const html = resultsHtml(fr, 1, {});
  const selectedCount = (html.match(/aria-selected="true"/g) || []).length;
  assert.equal(selectedCount, 1);
  assert.ok(
    html.includes('id="palette-opt-1" data-idx="1" aria-selected="true"'),
  );
});

test("resultsHtml renders both group labels and returns '' for an empty result", () => {
  const fr = filterPalette("program", fixture());
  const html = resultsHtml(fr, 0, {
    labelVacancies: "Roles",
    labelCompanies: "Orgs",
  });
  assert.ok(html.includes(">Roles<"));
  assert.ok(html.includes(">Orgs<"));
  assert.equal(resultsHtml({ flat: [] }, -1, {}), "");
});
