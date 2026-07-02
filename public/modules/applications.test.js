// applications.js — the pure projection that powers the Applications section
// (DHA-348). collectApplications / summarizeApplications take arrays and return
// plain data, so they unit-test without a DOM. applications.js imports state.js
// (which asserts window.VACANCY_DATA at eval) and i18n.js, so we stub the two
// browser globals those modules read at import time, then dynamic-import.

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

const { collectApplications, summarizeApplications } =
  await import("./applications.js");

function g(id, org, application, extra) {
  return Object.assign({ id, org, title: id + " role", application }, extra);
}

test("only groups carrying an application are collected", () => {
  const groups = [
    g("v1", "Acme", {
      status: "applied",
      channel: "email",
      applied_at: "2026-06-01",
    }),
    g("v2", "Acme", null),
    g("v3", "Beta", undefined),
  ];
  const out = collectApplications(groups, []);
  assert.equal(out.length, 1);
  assert.equal(out[0].org, "Acme");
  assert.equal(out[0].apps.length, 1);
  assert.equal(out[0].apps[0].id, "v1");
});

test("applications are grouped by company (slug preferred over org)", () => {
  const groups = [
    g(
      "v1",
      "Acme",
      { status: "applied", applied_at: "2026-06-01" },
      {
        company_slug: "acme",
      },
    ),
    g(
      "v2",
      "Acme Inc",
      { status: "interview", applied_at: "2026-06-02" },
      {
        company_slug: "acme",
      },
    ),
    g("v3", "Beta", { status: "offer", applied_at: "2026-06-03" }),
  ];
  const out = collectApplications(groups, []);
  assert.equal(out.length, 2);
  const acme = out.find((c) => c.key === "acme");
  assert.equal(acme.apps.length, 2);
});

test("a vacancy present in both live and archived sets is not double-counted", () => {
  const live = [
    g("v1", "Acme", { status: "applied", applied_at: "2026-06-01" }),
  ];
  const archived = [
    g("v1", "Acme", { status: "applied", applied_at: "2026-06-01" }),
  ];
  const out = collectApplications(live, archived);
  assert.equal(out.length, 1);
  assert.equal(out[0].apps.length, 1);
});

test("artifact keys are surfaced (values are never read)", () => {
  const groups = [
    g("v1", "Acme", {
      status: "applied",
      applied_at: "2026-06-01",
      artifacts: { cover_letter: true, cv: true },
    }),
  ];
  const out = collectApplications(groups, []);
  assert.deepEqual(out[0].apps[0].artifacts.sort(), ["cover_letter", "cv"]);
});

test("companies sort most-recent-application first; apps within a company newest-first", () => {
  const groups = [
    g("v1", "Old Co", { status: "applied", applied_at: "2026-01-01" }),
    g("v2", "New Co", { status: "applied", applied_at: "2026-06-10" }),
    g("v3", "New Co", { status: "interview", applied_at: "2026-06-20" }),
  ];
  const out = collectApplications(groups, []);
  assert.equal(out[0].org, "New Co");
  assert.equal(out[0].apps[0].id, "v3"); // newest first within company
  assert.equal(out[1].org, "Old Co");
});

test("summarizeApplications totals and breaks down by status", () => {
  const groups = [
    g("v1", "Acme", { status: "applied", applied_at: "2026-06-01" }),
    g("v2", "Acme", { status: "applied", applied_at: "2026-06-02" }),
    g("v3", "Beta", { status: "offer", applied_at: "2026-06-03" }),
  ];
  const companies = collectApplications(groups, []);
  const { total, byStatus } = summarizeApplications(companies);
  assert.equal(total, 3);
  assert.equal(byStatus.applied, 2);
  assert.equal(byStatus.offer, 1);
});
