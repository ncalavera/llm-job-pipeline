// =============================================================================
// api.js — Server communication (save/load), offline detection
// =============================================================================

import {
  state,
  API_BASE,
  emit,
  on,
  mergeRemoteStatuses,
  companiesList,
} from "./state.js";

// ---------------------------------------------------------------------------
// Sync status indicators
// ---------------------------------------------------------------------------

let syncHideTimer = null;

export function showSyncStatus(text, isError) {
  const el = document.getElementById("syncIndicator");
  if (!el) return;
  el.textContent = text;
  el.classList.remove("fade-out");
  if (syncHideTimer) clearTimeout(syncHideTimer);
  syncHideTimer = setTimeout(
    function () {
      el.classList.add("fade-out");
    },
    isError ? 8000 : 3000,
  );
}

export function showOfflineBanner() {
  const el = document.getElementById("syncIndicator");
  if (!el) return;
  el.textContent = "\u26A0 Offline \u2014 changes are not being saved";
  el.classList.remove("fade-out");
  el.classList.add("sync-offline");
  if (syncHideTimer) clearTimeout(syncHideTimer);
}

export function hideOfflineBanner() {
  const el = document.getElementById("syncIndicator");
  if (!el) return;
  el.classList.remove("sync-offline");
}

// ---------------------------------------------------------------------------
// Save to server
// ---------------------------------------------------------------------------

export function saveToServer(id, status) {
  if (!API_BASE) return;
  fetch(API_BASE + "/api/save", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, status }),
  })
    .then(function (r) {
      if (r.ok) {
        if (!state.apiHealthy) {
          state.apiHealthy = true;
          hideOfflineBanner();
        }
        showSyncStatus(
          "\u2705 Saved: " + new Date().toLocaleTimeString(),
          false,
        );
      } else {
        console.error("Save API error:", r.status, r.statusText);
        state.apiHealthy = false;
        showOfflineBanner();
      }
    })
    .catch(function (e) {
      console.error("Save network error:", e);
      state.apiHealthy = false;
      showOfflineBanner();
    });
}

// ---------------------------------------------------------------------------
// Load from server (respects optimistic flag)
// ---------------------------------------------------------------------------

export function loadFromServer() {
  if (!API_BASE) return;
  fetch(API_BASE + "/api/statuses", { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((payload) => {
      const remote = payload && payload.statuses ? payload.statuses : payload;
      const timestamps =
        payload && payload.timestamps ? payload.timestamps : {};
      const changed = mergeRemoteStatuses(remote, timestamps);
      state.statusesLoaded = true;
      if (changed > 0) {
        console.log("Loaded " + changed + " statuses from Supabase");
      }
      emit("statusesLoaded");
    })
    .catch((e) => {
      state.statusesLoaded = true; // allow interaction even if API fails
      state.apiHealthy = false;
      showOfflineBanner();
      emit("statusesLoaded");
      console.warn("Status sync failed:", e);
    });
}

// ---------------------------------------------------------------------------
// Company review (approve/reject)
// ---------------------------------------------------------------------------

export function saveCompanyReview(companyId, action) {
  if (!API_BASE) return Promise.resolve(false);
  return fetch(API_BASE + "/api/company-review", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ company_id: companyId, action: action }),
  })
    .then(function (r) {
      if (r.ok) {
        if (!state.apiHealthy) {
          state.apiHealthy = true;
          hideOfflineBanner();
        }
        var label = action === "approve" ? "Approved" : "Rejected";
        showSyncStatus(
          "\u2705 " + label + ": " + new Date().toLocaleTimeString(),
          false,
        );
        return true;
      } else {
        console.error("Company review API error:", r.status, r.statusText);
        state.apiHealthy = false;
        showOfflineBanner();
        return false;
      }
    })
    .catch(function (e) {
      console.error("Company review network error:", e);
      state.apiHealthy = false;
      showOfflineBanner();
      return false;
    });
}

// ---------------------------------------------------------------------------
// Load company statuses from server
// ---------------------------------------------------------------------------

export function loadCompanyStatuses() {
  if (!API_BASE) return;
  fetch(API_BASE + "/api/company-statuses", { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((payload) => {
      var remote = payload && payload.statuses ? payload.statuses : {};
      state.companyStatuses = remote;
      state.companyStatusesLoaded = true;
      emit("companyStatusesLoaded");
    })
    .catch((e) => {
      state.companyStatusesLoaded = true;
      emit("companyStatusesLoaded");
      console.warn("Company status sync failed:", e);
    });
}

// ---------------------------------------------------------------------------
// Load live company rows from server (Companies tab — always current)
// ---------------------------------------------------------------------------

// Snapshot-only deep-profile fields not returned by /api/companies; merged onto
// the live rows by company_id so the profile page keeps working.
const DEEP_PROFILE_FIELDS = [
  "product",
  "website",
  "careers_url",
  "description",
  "executive_summary",
  "md_content",
  "has_deep_analysis",
  "is_enriched",
  "founded_year",
  "employee_count",
  "funding_status",
  "hq_location",
  "sector",
  "logo_url",
  "alignment_label",
  "fit_strengths",
  "fit_risks",
  "fit_approach",
  "experience_reasoning",
  "mission_verdict",
  "experience_match",
  "personal_interest",
  "notes",
  "glassdoor_rating",
  "linkedin_employees",
  "recent_news",
  "org_color",
  "region_breakdown",
  "status_reason",
];

export function loadCompanies() {
  if (!API_BASE) return;
  fetch(API_BASE + "/api/companies", { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((payload) => {
      var live =
        payload && Array.isArray(payload.companies) ? payload.companies : null;
      if (!live) return;
      // Merge snapshot deep-profile fields (by company_id) onto the live rows.
      var snapById = {};
      for (var i = 0; i < companiesList.length; i++) {
        if (companiesList[i].company_id)
          snapById[companiesList[i].company_id] = companiesList[i];
      }
      for (var j = 0; j < live.length; j++) {
        var snap = snapById[live[j].company_id];
        if (!snap) continue;
        for (var k = 0; k < DEEP_PROFILE_FIELDS.length; k++) {
          var f = DEEP_PROFILE_FIELDS[k];
          if (live[j][f] === undefined) live[j][f] = snap[f];
        }
        // The profile's vacancy section renders snapshot vacancy cards by id;
        // keep the snapshot ids for it (the table uses live counts, not ids).
        if (Array.isArray(snap.vacancy_ids))
          live[j].vacancy_ids = snap.vacancy_ids;
      }
      state.liveCompanies = live;
      emit("companiesLoaded");
    })
    .catch((e) => {
      console.warn("Live company load failed (using snapshot):", e);
    });
}

// ---------------------------------------------------------------------------
// Auto-save on status changes
// ---------------------------------------------------------------------------

export function initApi() {
  on("statusChanged", ({ ids, status }) => {
    ids.forEach((id) => saveToServer(id, status));
  });
}
