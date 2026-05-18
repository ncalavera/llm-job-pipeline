// =============================================================================
// api.js — Server communication (save/load), offline detection
// =============================================================================

import { state, API_BASE, emit, on, mergeRemoteStatuses } from "./state.js";

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
  el.textContent =
    "\u26A0 \u041E\u0444\u043B\u0430\u0439\u043D-\u0440\u0435\u0436\u0438\u043C \u2014 \u0438\u0437\u043C\u0435\u043D\u0435\u043D\u0438\u044F \u043D\u0435 \u0441\u043E\u0445\u0440\u0430\u043D\u044F\u044E\u0442\u0441\u044F";
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
          "\u2705 \u0421\u043E\u0445\u0440\u0430\u043D\u0435\u043D\u043E: " +
            new Date().toLocaleTimeString(),
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
// Auto-save on status changes
// ---------------------------------------------------------------------------

export function initApi() {
  on("statusChanged", ({ ids, status }) => {
    ids.forEach((id) => saveToServer(id, status));
  });
}
