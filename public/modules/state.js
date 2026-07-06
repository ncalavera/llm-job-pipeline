// =============================================================================
// state.js — Centralized state, pub/sub events, constants
// =============================================================================

import { initialSyncState, nextSyncState } from "./nav.js";

// Contract: data.js must load before this module
if (!window.VACANCY_DATA) {
  throw new Error("data.js must load before app.js");
}

// ---------------------------------------------------------------------------
// Data from VACANCY_DATA (immutable after init)
// ---------------------------------------------------------------------------

export const {
  config,
  stats,
  vacancy_ids,
  groups,
  companies,
  triage_reviews: triageReviews,
} = window.VACANCY_DATA;

export const archivedGroups = window.VACANCY_DATA.archived_groups || [];

// API base resolution:
//  - Served over http(s) (Vercel OR the local dashboard server): use the page's
//    own origin and POST to same-origin /api/* — both deployments answer there.
//  - Opened as a file:// (no server): fall back to the baked config.api_base,
//    or "" which means truly offline (no save).
export const API_BASE =
  location.protocol === "http:" || location.protocol === "https:"
    ? location.origin
    : config.api_base || "";

// How the initial payload was sourced (bootstrap.js's boot()), stashed on
// window before this module loads. "fallback" means the baked public/data.js
// was used because /api/vacancies wasn't available — the fallback banner
// (nav.js's fallbackBannerState, DHA-422) reads this directly rather than the
// sync-status machine below, since that machine's "stale" also covers a
// live-loaded session with a few polling blips, which is a different thing.
export const dashboardSource = window.__DASHBOARD_SYNC_SOURCE__;

// O(1) lookup maps
export const groupsById = new Map(groups.map((g) => [g.id, g]));
// Archived rows live in their OWN index, deliberately NOT in groupsById: the
// company rollups (companies.js _companyRoles / derive.js companyRollup) join a
// company's vacancy_ids through groupsById, and archived ids CAN appear in that
// list (data_prep ships all statuses) — folding them into groupsById would
// silently change every company's vacancy/applyable counts. Kept separate so
// the vacancy detail route can still resolve an archived id (read-only page).
export const archivedGroupsById = new Map(archivedGroups.map((g) => [g.id, g]));
export const companiesBySlug = new Map(companies.map((c) => [c.slug, c]));
export const companiesList = Array.isArray(companies) ? companies : [];

// Company data source: live rows from /api/companies once loaded, else the
// (possibly stale) snapshot baked into VACANCY_DATA. The live rows are merged
// with snapshot deep-profile fields at load time (see api.js loadCompanies).
export function getCompanies() {
  return state.liveCompanies || companiesList;
}

export function getCompanyBySlug(slug) {
  if (state.liveCompanies) {
    return state.liveCompanies.find((c) => c.slug === slug) || null;
  }
  return companiesBySlug.get(slug) || null;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const STATUS_PRI = {
  to_apply: 0,
  to_research: 1,
  to_network: 2,
  applied: 3,
  skipped: 4,
  liked: 5,
  expiring: 6,
  passed: 7,
  unseen: 8,
};

export const STATUS_BASKET = {
  liked: "liked",
  to_apply: "liked",
  to_research: "liked",
  to_network: "liked",
  applied: "liked",
  // A protected, about-to-disappear role with a decision still pending belongs
  // in the active (Liked) basket, not Passed.
  expiring: "liked",
  unseen: "unseen",
  passed: "passed",
  skipped: "passed",
};

export const TRIAGE_COLUMNS = [
  {
    key: "liked",
    label: "Liked",
    color: "var(--gold)",
    compact: true,
  },
  {
    key: "to_apply",
    label: "To apply",
    color: "var(--emerald)",
  },
  {
    key: "to_research",
    label: "Research",
    color: "var(--amber)",
  },
  {
    key: "to_network",
    label: "Networking",
    color: "var(--lavender)",
  },
  {
    key: "applied",
    label: "Applied",
    color: "var(--coral)",
  },
  {
    // Roles that fell out of actuality (deadline lapsed or gone from source):
    // surfaced together for an explicit decision instead of silently passing.
    // Display-only — 'expired' is not a real DB status, so cards can be dragged
    // OUT to a decision but never dropped IN (see `derived`). Sits next to the
    // other dead-end column (Skipped) at the end of the board.
    key: "expired",
    label: "Expired",
    color: "var(--terracotta)",
    derived: true,
  },
  { key: "skipped", label: "Skipped", color: "var(--muted)" },
];

export const CHIP_TO_COL = {
  applyable: "applyable",
  liked: "liked",
  score: "score",
  interest: "fit",
  az: "name",
};

// ---------------------------------------------------------------------------
// Mutable state — single object for all UI state
// ---------------------------------------------------------------------------

export const state = {
  dbData: {},
  statusesLoaded: false,
  apiHealthy: true,
  currentBasket: "unseen",
  activeCatalogLocs: new Set(),
  // Discovery score-floor toggle. When true the VISIBLE_MIN_SCORE floor is
  // lifted so sub-threshold roles show. Shared state (not a catalog-local flag)
  // so the Geo table honours the SAME show-all as the Catalog — the two browse
  // surfaces read one visible set and can't disagree (DHA-376 Nit B).
  // Default ON: every scored role is visible in Browse; toggle to "Top only"
  // re-applies the floor. The floor was hiding sub-40 roles the user paid to
  // score and wants to review (recall-first).
  catalogShowAll: true,
  catalogSortDesc: true,
  companySortCol: "applyable",
  companySortAsc: false,
  statsSortCol: "count",
  statsSortAsc: false,
  // The active LEAF view (render dispatch keys on this). Today is the default
  // entry (DHA-348). The six-section chrome derives from it via nav.js.
  currentMode: "today",
  // Remembered Vacancies sub-view (Browse/Geo/Archive) so re-opening the
  // Vacancies section returns to where the user was.
  vacancyView: "catalog",
  // Remembered Applications sub-view (Applications/Triage) so re-opening the
  // Applications section returns to where the user was.
  applicationsView: "applications",
  // Applications section status filter ("all" | applied | interview | …).
  appStatusFilter: "all",
  companyStatuses: {},
  companyStatusesLoaded: false,
  companySubTab: "approved",
  companyMonitorFilters: new Set(),
  currentProfileSlug: null,
  // Routed vacancy detail page open over the list (?vacancy=<group-id>), or
  // null. Mirror of currentProfileSlug for the second detail surface (U4).
  currentVacancyId: null,
  // How the open vacancy page was entered (U6/F3). { context, queue } when
  // opened from the Browse unreviewed list (drives "Move to apply" auto-
  // advance); null for cold deep links, popstate, Today, and company roles —
  // those confirm in place with no advance.
  vacancyEntry: null,
  // Live company rows from /api/companies (null until loaded → snapshot used).
  liveCompanies: null,
  // Sidebar footer sync status (nav.js state machine). bootstrap.js's boot()
  // stashes how the initial payload was sourced on window before importing
  // this module: "live" starts already confirmed ok; "fallback" (simple/local
  // mode, baked data.js, no /api/vacancies to poll) is honestly shown as a
  // stale snapshot rather than silently rendering as live. Anything else
  // (e.g. no window, under a test runner) starts "checking".
  sync:
    window.__DASHBOARD_SYNC_SOURCE__ === "live"
      ? nextSyncState(initialSyncState(), "ok")
      : window.__DASHBOARD_SYNC_SOURCE__ === "fallback"
        ? { status: "stale", consecutiveFailures: 0 }
        : initialSyncState(),
};

/** Feed one poll outcome into the sidebar sync-status state machine. */
export function recordSyncOutcome(outcome) {
  state.sync = nextSyncState(state.sync, outcome);
}

// Seed dbData from each group's baked status as the BASE layer, defaulting to
// "unseen" when the payload carries none (DHA-412). In full mode the live
// /api/statuses fetch (mergeRemoteStatuses) overwrites this base right after
// load, so the API still wins; in simple/static mode (no API) the baked status
// is what the basket/Triage/Applied views read, instead of everything showing
// "unseen" regardless of what data.js baked in.
for (const g of groups) {
  g.member_ids = Array.isArray(g.member_ids) ? g.member_ids : [];
  state.dbData[g.id] = { status: g.status || "unseen" };
}

// ---------------------------------------------------------------------------
// Pub/sub event system
// ---------------------------------------------------------------------------

const handlers = {};

export function on(event, fn) {
  if (!handlers[event]) handlers[event] = [];
  handlers[event].push(fn);
}

export function emit(event, data) {
  (handlers[event] || []).forEach((fn) => fn(data));
}

// ---------------------------------------------------------------------------
// Render debounce via requestAnimationFrame
// ---------------------------------------------------------------------------

let renderPending = false;

export function scheduleRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => {
    renderPending = false;
    window.__renderCount = (window.__renderCount || 0) + 1;
    emit("render");
  });
}

// ---------------------------------------------------------------------------
// State queries
// ---------------------------------------------------------------------------

export function getGroupStatus(g) {
  let best = "unseen";
  let bestP = 99;
  const allIds = new Set(g.member_ids);
  allIds.add(g.id);
  for (const mid of allIds) {
    if (state.dbData[mid]) {
      const p = STATUS_PRI[state.dbData[mid].status] ?? 1;
      if (p < bestP) {
        bestP = p;
        best = state.dbData[mid].status;
      }
    }
  }
  return best;
}

/**
 * Check if a vacancy group belongs to an approved company.
 * Uses live companyStatuses (from API) with fallback to baked companies data.
 * Hide-by-default: unknown company_id → hidden.
 */
export function isGroupCompanyApproved(g) {
  const cid = g.company_id;
  if (!cid) return true; // legacy data without company_id — show (backward compat)
  // Live status from API takes priority
  if (state.companyStatuses[cid]) {
    return state.companyStatuses[cid] === "approved";
  }
  // Fallback to baked companies data
  for (const c of companiesList) {
    if (c.company_id === cid) {
      return c.review_status === "approved";
    }
  }
  // Unknown company — hide by default
  return false;
}

// ---------------------------------------------------------------------------
// Live snapshot refresh (polling — see bootstrap.js)
// ---------------------------------------------------------------------------

/** Replace an array's contents in place, keeping the same reference. */
function replaceArrayContents(arr, next) {
  arr.length = 0;
  if (Array.isArray(next)) arr.push(...next);
}

/** Replace a plain object's own keys in place, keeping the same reference. */
function replaceObjectContents(obj, next) {
  for (const k of Object.keys(obj)) delete obj[k];
  if (next && typeof next === "object") Object.assign(obj, next);
}

/**
 * Hot-swap a freshly-polled /api/vacancies payload into the running app.
 *
 * `config`, `stats`, `vacancy_ids`, `groups`, `companies`, `archivedGroups`
 * and `triageReviews` above are `const` bindings destructured once from
 * window.VACANCY_DATA at module load — every render function across the app
 * (catalog.js, companies.js, pipeline.js, today.js, stats.js, archive.js)
 * imports and reads those SAME array/object references at render time. A
 * refresh can't reassign them (const, and every importer would still hold
 * the stale reference), so this mutates their CONTENTS in place instead —
 * the next render() call sees fresh data through the same bindings.
 *
 * UI state (active tab, filters, sort, scroll, search text) lives in the
 * separate `state` object and in DOM elements untouched by this function, so
 * it survives automatically; callers just call scheduleRender() after.
 *
 * Known gap: presentation config baked once at module load (the i18n string
 * table / pack images in i18n.js) does not hot-reload — those still need a
 * manual refresh. In practice they only
 * change if the user edits dashboard settings and reruns the pipeline while
 * the tab is open, which is rare enough to leave as a documented limitation.
 * Same class of gap: `state.liveCompanies` (loaded on demand from
 * /api/companies by the Companies tab) is not refreshed here, and
 * getCompanies() prefers it once set — new companies from a polled snapshot
 * appear in that tab only after its next interaction reloads the live list.
 */
export function applySnapshot(payload) {
  if (!payload || typeof payload !== "object") return false;

  replaceObjectContents(config, payload.config || {});
  replaceObjectContents(stats, payload.stats || {});
  replaceArrayContents(vacancy_ids, payload.vacancy_ids || []);
  replaceArrayContents(groups, payload.groups || []);
  replaceArrayContents(companies, payload.companies || []);
  replaceArrayContents(archivedGroups, payload.archived_groups || []);
  replaceArrayContents(triageReviews, payload.triage_reviews || []);

  // No dedicated const export — today.js / boards.js / settings.js read these
  // straight off the global at render time, so keep window.VACANCY_DATA itself
  // current too (Boards catalogue, resolved Settings, Today's learning hint).
  window.VACANCY_DATA.enrichment_stats = payload.enrichment_stats;
  window.VACANCY_DATA.latency_metrics = payload.latency_metrics;
  // Keep the last-good baked values when a polled payload predates these keys
  // (old server, new client) — blanking a rendered section is worse than
  // showing slightly stale read-only catalogue/settings data.
  if (payload.boards_catalog !== undefined)
    window.VACANCY_DATA.boards_catalog = payload.boards_catalog;
  if (payload.settings !== undefined)
    window.VACANCY_DATA.settings = payload.settings;
  if (payload.learning !== undefined)
    window.VACANCY_DATA.learning = payload.learning;

  groupsById.clear();
  for (const g of groups) {
    g.member_ids = Array.isArray(g.member_ids) ? g.member_ids : [];
    // New group from a polled snapshot: seed from its baked status (base
    // layer), same rule as the initial seed above. Existing entries keep the
    // live status already merged in — the snapshot never clobbers a decision.
    if (!state.dbData[g.id])
      state.dbData[g.id] = { status: g.status || "unseen" };
    groupsById.set(g.id, g);
  }

  archivedGroupsById.clear();
  for (const g of archivedGroups) {
    g.member_ids = Array.isArray(g.member_ids) ? g.member_ids : [];
    archivedGroupsById.set(g.id, g);
  }

  companiesBySlug.clear();
  for (const c of companies) companiesBySlug.set(c.slug, c);

  return true;
}

export function getCompanyStatusCounts(ids) {
  const counts = { liked: 0, passed: 0, unseen: 0 };
  const todayStr = new Date().toISOString().slice(0, 10);
  for (const id of ids || []) {
    let status = (state.dbData[id] && state.dbData[id].status) || "unseen";
    // Expired liked vacancies count as passed
    if (STATUS_BASKET[status] === "liked") {
      const g = groupsById.get(id);
      if (g && g.deadline) {
        const dl = new Date(g.deadline);
        if (!isNaN(dl.getTime()) && dl < new Date(todayStr)) {
          status = "passed";
        }
      }
    }
    counts[status] = (counts[status] || 0) + 1;
  }
  return counts;
}

// ---------------------------------------------------------------------------
// State mutations
// ---------------------------------------------------------------------------

/**
 * Update vacancy status with optimistic flag.
 * Emits 'statusChanged' — subscribers handle save, toast, re-render.
 */
export function updateStatus(canonId, memberIds, newStatus) {
  if (!state.statusesLoaded) {
    console.warn("Ignoring status change \u2014 statuses not loaded yet");
    return;
  }
  const allIds = new Set(memberIds);
  if (canonId) allIds.add(canonId);
  const savedIds = [];
  allIds.forEach((mid) => {
    if (state.dbData[mid]) {
      state.dbData[mid].status = newStatus;
      state.dbData[mid]._optimistic = Date.now();
      savedIds.push(mid);
    }
  });
  emit("statusChanged", { ids: savedIds, status: newStatus });
}

/**
 * Merge remote statuses into dbData, respecting optimistic flag.
 * Entries with _optimistic < 5 seconds old are NOT overwritten.
 * Returns count of changed entries.
 */
export function mergeRemoteStatuses(remote, timestamps) {
  let changed = 0;
  const now = Date.now();
  for (const [id, status] of Object.entries(remote)) {
    if (state.dbData[id]) {
      // Protect optimistic updates from being overwritten by stale server data
      if (
        state.dbData[id]._optimistic &&
        now - state.dbData[id]._optimistic < 5000
      ) {
        continue;
      }
      state.dbData[id].status = status;
      delete state.dbData[id]._optimistic;
      if (timestamps[id]) {
        state.dbData[id].status_changed_at = timestamps[id];
      }
      changed++;
    }
  }
  return changed;
}
