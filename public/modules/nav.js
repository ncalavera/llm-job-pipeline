// =============================================================================
// nav.js — Six-section navigation model (pure, unit-testable).
//
// The dashboard has SIX top-level sections. Several of them are single leaf
// views; "Vacancies" is a hub that holds four vacancy leaf views behind a
// sub-navigation (Browse / Triage / Geo / Archive). `state.currentMode` stays
// the LEAF the render dispatch keys on (unchanged for every existing module);
// this module only maps a leaf mode → the top-nav section it belongs to, so the
// section chrome (active button, sub-nav visibility) can follow along.
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
export const VACANCY_VIEWS = ["catalog", "pipeline", "stats", "archive"];

// The vacancy leaf shown when Vacancies is opened with no remembered view.
export const DEFAULT_VACANCY_VIEW = "catalog";

// Every leaf mode → the top-nav section that owns it.
const MODE_TO_SECTION = {
  today: "today",
  catalog: "vacancies",
  pipeline: "vacancies",
  stats: "vacancies",
  archive: "vacancies",
  companies: "companies",
  applications: "applications",
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
