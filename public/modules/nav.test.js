// nav.js — the six-section routing model. Pure functions, no DOM/state, so
// they unit-test directly (DHA-348).

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  SECTIONS,
  VACANCY_VIEWS,
  DEFAULT_VACANCY_VIEW,
  APPLICATIONS_VIEWS,
  DEFAULT_APPLICATIONS_VIEW,
  sectionForMode,
  isVacancyView,
  isApplicationsView,
  SYNC_STALE_AFTER,
  initialSyncState,
  nextSyncState,
  syncStatusLabelKey,
} from "./nav.js";

test("there are exactly six top-nav sections, in order", () => {
  assert.deepEqual(SECTIONS, [
    "today",
    "vacancies",
    "companies",
    "applications",
    "boards",
    "settings",
  ]);
});

test("every leaf mode maps to its owning section", () => {
  assert.equal(sectionForMode("today"), "today");
  assert.equal(sectionForMode("catalog"), "vacancies");
  assert.equal(sectionForMode("stats"), "vacancies");
  assert.equal(sectionForMode("archive"), "vacancies");
  assert.equal(sectionForMode("companies"), "companies");
  assert.equal(sectionForMode("applications"), "applications");
  // Triage moved from a Vacancies sub-view to an Applications sub-view.
  assert.equal(sectionForMode("pipeline"), "applications");
  assert.equal(sectionForMode("boards"), "boards");
  assert.equal(sectionForMode("settings"), "settings");
});

test("the deep-link detail screens map to their owning parent section (U4)", () => {
  // route.js's route.screen values — the router highlights the owning section
  // through the same function so a cold ?vacancy=/?company= deep link lights
  // up the right sidebar parent (AE6).
  assert.equal(sectionForMode("vacancy"), "vacancies");
  assert.equal(sectionForMode("company"), "companies");
});

test("an unknown mode falls back to the Today section (safe home)", () => {
  assert.equal(sectionForMode("nonsense"), "today");
  assert.equal(sectionForMode(undefined), "today");
});

test("the three vacancy sub-views are recognised; nothing else is", () => {
  assert.deepEqual(VACANCY_VIEWS, ["catalog", "stats", "archive"]);
  for (const v of VACANCY_VIEWS) assert.equal(isVacancyView(v), true);
  assert.equal(isVacancyView("pipeline"), false);
  assert.equal(isVacancyView("today"), false);
  assert.equal(isVacancyView("companies"), false);
  assert.equal(isVacancyView("applications"), false);
  assert.equal(isVacancyView("settings"), false);
});

test("the default vacancy view is one of the vacancy views", () => {
  assert.ok(VACANCY_VIEWS.includes(DEFAULT_VACANCY_VIEW));
});

test("the two applications sub-views are recognised; nothing else is", () => {
  assert.deepEqual(APPLICATIONS_VIEWS, ["applications", "pipeline"]);
  for (const v of APPLICATIONS_VIEWS) assert.equal(isApplicationsView(v), true);
  assert.equal(isApplicationsView("catalog"), false);
  assert.equal(isApplicationsView("today"), false);
  assert.equal(isApplicationsView("companies"), false);
  assert.equal(isApplicationsView("settings"), false);
});

test("the default applications view is one of the applications views", () => {
  assert.ok(APPLICATIONS_VIEWS.includes(DEFAULT_APPLICATIONS_VIEW));
});

test("the sidebar opens Triage directly, not the remembered sub-view (post-ship fast fix)", () => {
  assert.equal(DEFAULT_APPLICATIONS_VIEW, "pipeline");
});

// --- Sidebar sync-status state machine (DHA-387, U3) ------------------------

test("initial sync state is 'checking' with no failures", () => {
  assert.deepEqual(initialSyncState(), {
    status: "checking",
    consecutiveFailures: 0,
  });
});

test("checking -> ok on the first successful poll", () => {
  const s = nextSyncState(initialSyncState(), "ok");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 0 });
});

test("a single soft failure while checking doesn't flip the status yet", () => {
  const s = nextSyncState(initialSyncState(), "soft_fail");
  assert.equal(s.status, "checking");
  assert.equal(s.consecutiveFailures, 1);
});

test("soft failures accumulate to 'stale' exactly at SYNC_STALE_AFTER", () => {
  assert.equal(SYNC_STALE_AFTER, 3);
  let s = initialSyncState();
  for (let i = 1; i < SYNC_STALE_AFTER; i++) {
    s = nextSyncState(s, "soft_fail");
    assert.equal(s.status, "checking", `still checking after ${i} blip(s)`);
    assert.equal(s.consecutiveFailures, i);
  }
  s = nextSyncState(s, "soft_fail");
  assert.equal(s.status, "stale");
  assert.equal(s.consecutiveFailures, SYNC_STALE_AFTER);
});

test("a soft failure while ok holds 'ok' until the threshold is crossed", () => {
  let s = nextSyncState(initialSyncState(), "ok"); // ok, 0 failures
  s = nextSyncState(s, "soft_fail");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 1 });
  s = nextSyncState(s, "soft_fail");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 2 });
  s = nextSyncState(s, "soft_fail");
  assert.deepEqual(s, { status: "stale", consecutiveFailures: 3 });
});

test("a hard failure jumps straight to 'error', bypassing the soft-fail counter", () => {
  const s = nextSyncState(
    { status: "ok", consecutiveFailures: 0 },
    "hard_fail",
  );
  assert.deepEqual(s, { status: "error", consecutiveFailures: 0 });
});

test("full cycle: checking -> ok -> stale -> error -> ok", () => {
  let s = initialSyncState();
  s = nextSyncState(s, "ok");
  assert.equal(s.status, "ok");
  for (let i = 0; i < SYNC_STALE_AFTER; i++) s = nextSyncState(s, "soft_fail");
  assert.equal(s.status, "stale");
  s = nextSyncState(s, "hard_fail");
  assert.equal(s.status, "error");
  s = nextSyncState(s, "ok");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 0 });
});

test("alternate order: ok -> error -> ok (skipping stale entirely)", () => {
  let s = nextSyncState(initialSyncState(), "ok");
  s = nextSyncState(s, "hard_fail");
  assert.equal(s.status, "error");
  s = nextSyncState(s, "ok");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 0 });
});

test("'ok' recovers straight from 'stale' with the failure count reset", () => {
  let s = initialSyncState();
  for (let i = 0; i < SYNC_STALE_AFTER; i++) s = nextSyncState(s, "soft_fail");
  assert.equal(s.status, "stale");
  s = nextSyncState(s, "ok");
  assert.deepEqual(s, { status: "ok", consecutiveFailures: 0 });
});

test("syncStatusLabelKey maps every known status; unknown falls back to checking", () => {
  assert.equal(syncStatusLabelKey("checking"), "sync_checking");
  assert.equal(syncStatusLabelKey("ok"), "sync_live");
  assert.equal(syncStatusLabelKey("stale"), "sync_stale");
  assert.equal(syncStatusLabelKey("error"), "sync_error");
  assert.equal(syncStatusLabelKey("nonsense"), "sync_checking");
});
