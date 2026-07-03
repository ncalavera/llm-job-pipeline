// =============================================================================
// nav.js — Six-section navigation model (pure, unit-testable).
//
// The dashboard has SIX top-level sections. Several of them are single leaf
// views; two are hubs that hold leaf views behind a sub-navigation:
// "Vacancies" (Browse / Geo / Archive) and "Applications" (Applications /
// Triage). `state.currentMode` stays the LEAF the render dispatch keys on
// (unchanged for every existing module); this module only maps a leaf mode →
// the top-nav section it belongs to, so the section chrome (active button,
// sub-nav visibility) can follow along.
// =============================================================================

// The six top-nav sections, in display order.
export const SECTIONS = [
  "today",
  "vacancies",
  "companies",
  "applications",
  "boards",
  "settings",
];

// Leaf views that live UNDER the Vacancies section, in sub-nav order.
export const VACANCY_VIEWS = ["catalog", "stats", "archive"];

// The vacancy leaf shown when Vacancies is opened with no remembered view.
export const DEFAULT_VACANCY_VIEW = "catalog";

// Leaf views that live UNDER the Applications section, in sub-nav order.
// Triage used to be a Vacancies sub-view; it moved here because triaging is
// the step that turns a liked vacancy into an application.
export const APPLICATIONS_VIEWS = ["applications", "pipeline"];

// The applications leaf shown when Applications is opened with no remembered
// view.
export const DEFAULT_APPLICATIONS_VIEW = "applications";

// Every leaf mode → the top-nav section that owns it.
const MODE_TO_SECTION = {
  today: "today",
  catalog: "vacancies",
  stats: "vacancies",
  archive: "vacancies",
  companies: "companies",
  applications: "applications",
  pipeline: "applications",
  boards: "boards",
  settings: "settings",
};

/** The top-nav section that owns a leaf mode. Unknown → "today" (safe home). */
export function sectionForMode(mode) {
  return MODE_TO_SECTION[mode] || "today";
}

/** True when a leaf mode is one of the Vacancies sub-views. */
export function isVacancyView(mode) {
  return VACANCY_VIEWS.indexOf(mode) !== -1;
}

/** True when a leaf mode is one of the Applications sub-views. */
export function isApplicationsView(mode) {
  return APPLICATIONS_VIEWS.indexOf(mode) !== -1;
}
