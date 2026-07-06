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

// Leaf views that live UNDER the Applications section. "applications"
// (Applied) has no sidebar sub-item anymore (post-ship fast fix) — it stays
// listed here because it's still a real leaf mode, reachable programmatically
// (palette, nav parent mapping), just not from a sidebar affordance.
export const APPLICATIONS_VIEWS = ["applications", "pipeline"];

// The applications leaf the sidebar opens: always Triage now, direct with no
// remembered sub-view (there's nothing left to remember between).
export const DEFAULT_APPLICATIONS_VIEW = "pipeline";

// Every leaf mode → the top-nav section that owns it. The last two entries are
// not leaf modes but the deep-link DETAIL screens (route.js's route.screen
// values): a routed vacancy detail belongs to Vacancies, a company profile to
// Companies. Mapping them here lets app.js's routing highlight the owning
// section through this one function (fixing the cold-deep-link nav gap, AE6)
// instead of hardcoding the parent in the router.
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
  // Detail-overlay screens (route.js).
  vacancy: "vacancies",
  company: "companies",
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

// =============================================================================
// Sidebar sync-status state machine (DHA-387, U3).
//
// The sidebar footer shows one of four states: "checking" (before the first
// poll resolves — the initial state), "ok" (the last poll confirmed live
// data), "stale" (SYNC_STALE_AFTER consecutive soft failures — offline blips —
// with no hard error yet), or "error" (a hard failure: the endpoint answered
// with a real error status, or its body could not be parsed). A poll outcome
// is one of "ok" | "soft_fail" | "hard_fail"; "ok" always clears straight back
// to the live state regardless of what preceded it, so the indicator never
// gets stuck showing stale/error once the connection recovers.
// =============================================================================

// Consecutive soft failures tolerated before the footer calls the snapshot
// stale. One or two transient blips over 60s polling shouldn't flip the
// indicator; a real outage (3+ minutes) should.
export const SYNC_STALE_AFTER = 3;

/** The sync-status footer's state before any poll has resolved. */
export function initialSyncState() {
  return { status: "checking", consecutiveFailures: 0 };
}

/** Advance the sync-status state machine by one poll outcome. */
export function nextSyncState(prev, outcome) {
  if (outcome === "ok") return { status: "ok", consecutiveFailures: 0 };
  if (outcome === "hard_fail")
    return { status: "error", consecutiveFailures: 0 };
  // soft_fail: count it; only flip to "stale" once the threshold is crossed,
  // otherwise hold whatever status was already showing.
  const consecutiveFailures = ((prev && prev.consecutiveFailures) || 0) + 1;
  const status =
    consecutiveFailures >= SYNC_STALE_AFTER
      ? "stale"
      : (prev && prev.status) || "checking";
  return { status, consecutiveFailures };
}

// The i18n key for each sync-status word, for the thin DOM shell to look up.
const SYNC_LABEL_KEYS = {
  checking: "sync_checking",
  ok: "sync_live",
  stale: "sync_stale",
  error: "sync_error",
};

/** The i18n key for a sync-status word. Unknown status → "checking" (safe). */
export function syncStatusLabelKey(status) {
  return SYNC_LABEL_KEYS[status] || SYNC_LABEL_KEYS.checking;
}

// =============================================================================
// Fallback banner (DHA-422) — a prominent, dismissible notice that the page is
// running off the baked public/data.js snapshot, not live /api/vacancies.
//
// Deliberately keyed on bootstrap.js's boot-time `source` ("fallback"), NOT on
// the sync-status machine above: `state.sync.status` also turns "stale" after
// a few polling blips on an otherwise-live session (SYNC_STALE_AFTER) — that's
// a live-loaded page whose refresh is lagging, not the offline snapshot this
// banner warns about. `source` is set once in boot() and never changes for the
// life of the page, so it's the honest signal for "are we even looking at
// data.js".
// =============================================================================

// A fallback snapshot older than this reads as a warning, not an info note —
// ~48h, per DHA-422.
export const FALLBACK_STALE_AFTER_MS = 48 * 60 * 60 * 1000;

/**
 * Parse config.last_updated to epoch ms, treating an offset-less datetime as
 * UTC. The generator now writes a timezone-aware UTC stamp ("+00:00"), but
 * bakes from before that fix carry a NAIVE local-time isoformat — and
 * browsers parse an offset-less ISO datetime as browser-LOCAL time, skewing
 * the 48h check by up to ±14h depending on the viewer's timezone. Pinning
 * the interpretation to UTC keeps the error bounded by the PRODUCER's offset
 * only, and deterministic across viewers.
 * @returns {number} epoch ms, or NaN when unparseable
 */
export function parseSnapshotStamp(lastUpdated) {
  if (!lastUpdated) return NaN;
  // A "T" time part with no trailing designator (Z or ±hh[:]mm) → append Z.
  const naive =
    /T/.test(lastUpdated) && !/(Z|[+-]\d{2}:?\d{2})$/.test(lastUpdated);
  return Date.parse(naive ? lastUpdated + "Z" : lastUpdated);
}

/**
 * Decide the fallback banner's state. Pure — unit-tested without DOM/Date.now.
 * @param {string} source - window.__DASHBOARD_SYNC_SOURCE__ ("live"|"fallback"|…)
 * @param {string|null|undefined} lastUpdated - config.last_updated (ISO), absent on a pre-stamp bake
 * @param {number} now - Date.now(), injected for deterministic tests
 * @returns {{show: boolean, level: "info"|"warning", age: "known"|"unknown"}}
 */
export function fallbackBannerState(source, lastUpdated, now) {
  if (source !== "fallback")
    return { show: false, level: "info", age: "known" };
  const t = parseSnapshotStamp(lastUpdated);
  if (Number.isNaN(t)) return { show: true, level: "warning", age: "unknown" };
  const isStale = now - t > FALLBACK_STALE_AFTER_MS;
  return { show: true, level: isStale ? "warning" : "info", age: "known" };
}
