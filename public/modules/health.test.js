// health.test.js — the verdict line (HEALTH-001). healthVerdict() is a pure
// function over the /api/health-detail payload, so it tests directly: an
// all-clear payload must yield the calm green one-liner; any broken board or
// failing company must yield a warning that names the problems.
//
// location.protocol is "file:" below so state.js's API_BASE resolves to ""
// (no live fetch fires on import); the DOM shim mirrors boards.test.js.

import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.document = {
  getElementById: () => null,
  head: { appendChild: () => {} },
  createElement: () => ({}),
};
globalThis.window = {
  VACANCY_DATA: {
    config: { i18n: {}, i18n_all: null, language: "en" },
    stats: {},
    vacancy_ids: [],
    groups: [],
    companies: [],
    triage_reviews: [],
    archived_groups: [],
    boards_catalog: [],
  },
};
globalThis.location = { protocol: "file:", origin: "" };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };

const { healthVerdict } = await import("./health.js");

test("healthy payload → calm green one-liner", () => {
  const v = healthVerdict({
    boards: [
      { id: "a", presumed_broken: false },
      { id: "b", presumed_broken: false },
      { id: "c", presumed_broken: false },
    ],
    companies: { failing: [] },
  });
  assert.equal(v.ok, true);
  assert.equal(
    v.text,
    "All systems healthy — 3 boards fetching, no failing companies",
  );
});

test("single fetching board is singularised", () => {
  const v = healthVerdict({
    boards: [{ id: "a", presumed_broken: false }],
    companies: { failing: [] },
  });
  assert.equal(v.ok, true);
  assert.equal(
    v.text,
    "All systems healthy — 1 board fetching, no failing companies",
  );
});

test("broken board + failing companies → warning names both", () => {
  const v = healthVerdict({
    boards: [
      { id: "a", presumed_broken: true },
      { id: "b", presumed_broken: false },
    ],
    companies: { failing: [{ name: "X" }, { name: "Y" }] },
  });
  assert.equal(v.ok, false);
  assert.equal(v.text, "1 board presumed broken · 2 companies failing");
});

test("only a failing company → warning omits the boards clause", () => {
  const v = healthVerdict({
    boards: [{ id: "a", presumed_broken: false }],
    companies: { failing: [{ name: "X" }] },
  });
  assert.equal(v.ok, false);
  assert.equal(v.text, "1 company failing");
});

test("missing/empty payload → healthy with zero boards", () => {
  const v = healthVerdict({});
  assert.equal(v.ok, true);
  assert.equal(
    v.text,
    "All systems healthy — 0 boards fetching, no failing companies",
  );
});
