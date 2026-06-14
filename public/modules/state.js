// =============================================================================
// state.js — Centralized state, pub/sub events, constants
// =============================================================================

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

// O(1) lookup maps
export const groupsById = new Map(groups.map((g) => [g.id, g]));
export const companiesBySlug = new Map(companies.map((c) => [c.slug, c]));
export const companiesList = Array.isArray(companies) ? companies : [];

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
  passed: 6,
  unseen: 7,
};

export const STATUS_BASKET = {
  liked: "liked",
  to_apply: "liked",
  to_research: "liked",
  to_network: "liked",
  applied: "liked",
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
  { key: "skipped", label: "Skipped", color: "var(--muted)" },
];

export const CHIP_TO_COL = {
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
  catalogSortDesc: true,
  companySortCol: "liked",
  companySortAsc: false,
  statsSortCol: "count",
  statsSortAsc: false,
  currentMode: "companies",
  companyStatuses: {},
  companyStatusesLoaded: false,
  companySubTab: "approved",
  companyCardFilter: null,
  companyMonitorFilters: new Set(),
  currentProfileSlug: null,
};

// Initialize dbData — all vacancies start as "unseen", real statuses come from API
for (const g of groups) {
  g.member_ids = Array.isArray(g.member_ids) ? g.member_ids : [];
  state.dbData[g.id] = { status: "unseen" };
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
