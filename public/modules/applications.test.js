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

const {
  collectApplications,
  summarizeApplications,
  stageChipClass,
  applicationRowHtml,
  openApplicationRow,
  applicationsEmptyHtml,
} = await import("./applications.js");

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

// --- stageChipClass: 4 colors for 6 statuses (U13, DHA-397) -----------------

test("stageChipClass maps all 6 VALID_STATUSES to the 4-color scheme", () => {
  assert.equal(stageChipClass("offer"), "apl-stage-good");
  assert.equal(stageChipClass("applied"), "apl-stage-moderate");
  assert.equal(stageChipClass("interview"), "apl-stage-moderate");
  assert.equal(stageChipClass("rejected"), "apl-stage-weak");
  assert.equal(stageChipClass("withdrawn"), "apl-stage-weak");
  assert.equal(stageChipClass("draft"), "apl-stage-neutral");
});

test("stageChipClass falls back to neutral for an unknown status", () => {
  assert.equal(stageChipClass("bogus"), "apl-stage-neutral");
  assert.equal(stageChipClass(undefined), "apl-stage-neutral");
});

// --- applicationRowHtml: linked vs unlinked, structure, escaping (U13) ------

const baseApp = {
  id: "v1",
  title: "Chief of Staff",
  org: "GiveDirectly",
  company_slug: "givedirectly",
  status: "applied",
  applied_at: "2026-06-01",
  score: 82,
  live: true,
};

test("a linked row (live vacancy) opens the vacancy page on click", () => {
  const html = applicationRowHtml(baseApp);
  assert.match(
    html,
    /class="apl-row" data-id="v1" onclick="openApplicationRow\('v1'\)"/,
  );
  assert.doesNotMatch(html, /apl-row-unlinked/);
});

test("an unlinked row (vacancy no longer live) has no onclick and is visually dimmed", () => {
  const html = applicationRowHtml({ ...baseApp, live: false });
  assert.match(html, /class="apl-row apl-row-unlinked" data-id="v1">/);
  assert.doesNotMatch(html, /onclick="openApplicationRow/);
});

test("applicationRowHtml renders the tinted fit tile, stage chip, and a company link", () => {
  const html = applicationRowHtml(baseApp);
  assert.match(html, /apl-score q-good-bg">82</);
  assert.match(html, /apl-stage apl-stage-moderate">applied</);
  assert.match(
    html,
    /class="apl-company-link" onclick="event\.stopPropagation\(\);openCompanyProfile\('givedirectly'\)"[^>]*>GiveDirectly</,
  );
});

test("applicationRowHtml: null score renders a neutral tile, not a crimson one", () => {
  const html = applicationRowHtml({ ...baseApp, score: null });
  assert.match(html, /vac-score--none">—/);
  assert.doesNotMatch(html, /q-weak-bg/);
});

test("applicationRowHtml: a row with no company_slug renders plain text, no link", () => {
  const html = applicationRowHtml({ ...baseApp, company_slug: null });
  assert.doesNotMatch(html, /apl-company-link/);
  assert.match(html, /apl-company">GiveDirectly</);
});

test("openApplicationRow forwards id + applied context (no queue — no auto-advance, F3) to the router", () => {
  let called = null;
  globalThis.window.openVacancyRoute = (id, o) => {
    called = { id, opts: o };
  };
  openApplicationRow("v9");
  assert.deepEqual(called, { id: "v9", opts: { context: "applied" } });
});

// --- applicationRowHtml: escaping regression, text + attribute positions ---

const xssApp = {
  id: "v\"'></div><script>1</script>",
  title: "<img src=x onerror=alert(1)>",
  org: '"><svg onload=alert(1)>',
  company_slug: "co\"'></button>",
  status: "applied",
  applied_at: "2026-06-01",
  score: 70,
  live: true,
};

test("title/org are escaped in text-content positions", () => {
  const html = applicationRowHtml(xssApp);
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /<svg onload/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test("an id with quotes/HTML is escaped in the data-id AND the onclick attribute", () => {
  const html = applicationRowHtml(xssApp);
  assert.doesNotMatch(html, /data-id="v"'/);
  assert.match(html, /data-id="v&quot;/);
  assert.doesNotMatch(html, /openApplicationRow\('v"'\)/);
});

test("a company_slug with quotes is escaped in its nested onclick attribute", () => {
  const html = applicationRowHtml(xssApp);
  assert.doesNotMatch(html, /openCompanyProfile\('co"'\)/);
  assert.doesNotMatch(html, /<\/button><\/div>.*<script>/);
});

// --- applicationsEmptyHtml: first-run empty state (U13) ---------------------

test("applicationsEmptyHtml renders the icon and the fallback empty message", () => {
  const html = applicationsEmptyHtml();
  assert.match(html, /apl-empty-icon">✉️</);
  assert.match(
    html,
    /No applications yet\. Mark a role applied in Triage or Today\./,
  );
});
