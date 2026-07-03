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
