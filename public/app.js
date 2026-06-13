// =============================================================================
// app.js — Entry point: initialization, mode switching, event wiring
// =============================================================================

import {
  state,
  API_BASE,
  companiesBySlug,
  on,
  emit,
  scheduleRender,
} from "./modules/state.js";
import { initUI, showToast } from "./modules/helpers.js";
import { initApi, loadFromServer, loadCompanyStatuses } from "./modules/api.js";
import {
  initCatalog,
  updateBasketCounts,
  renderCatalog,
  switchBasket,
  toggleCatalogLoc,
  toggleCatalogSort,
  catalogThumbAction,
  toggleCatalogExpand,
} from "./modules/catalog.js";
import {
  initCompanies,
  renderCompanies,
  sortCompanyTable,
  toggleCompanySort,
  switchCompanySubTab,
  toggleCompanyCardFilter,
  toggleMonitoringChip,
  companyVacancyAction,
  reviewCompany,
  viewOrgInCatalog,
  openCompanyProfile,
  closeCompanyProfile,
  getCompanySlugFromUrl,
  renderProfileForSlug,
  hideProfile,
} from "./modules/companies.js";
import { renderPipeline } from "./modules/pipeline.js";
import { initStats, renderStats } from "./modules/stats.js";
import { initArchive, renderArchive } from "./modules/archive.js";

// ---------------------------------------------------------------------------
// Initialize UI elements (toast, scroll-to-top)
// ---------------------------------------------------------------------------

initUI();

// ---------------------------------------------------------------------------
// Event wiring — statusChanged triggers save, toast, re-render
// ---------------------------------------------------------------------------

on("statusChanged", ({ status }) => {
  showToast(status);
  updateBasketCounts();
  scheduleRender();
});

on("render", () => {
  updateBasketCounts();
  renderCatalog();
  if (state.currentMode === "companies") renderCompanies();
  if (state.currentMode === "pipeline") renderPipeline();
  if (state.currentMode === "stats") renderStats();
  if (state.currentProfileSlug) renderProfileForSlug(state.currentProfileSlug);
});

on("switchToCatalog", ({ orgFilter }) => {
  switchMode("catalog");
  const sel = document.getElementById("catalogOrgFilter");
  if (sel) {
    sel.value = orgFilter;
    renderCatalog();
  }
});

// ---------------------------------------------------------------------------
// Mode switching
// ---------------------------------------------------------------------------

function switchMode(mode) {
  // Close profile page if open
  if (state.currentProfileSlug) {
    state.currentProfileSlug = null;
    var profileEl = document.getElementById("companyProfile");
    if (profileEl) {
      profileEl.classList.remove("active");
      profileEl.innerHTML = "";
    }
    var statsPanel = document.getElementById("statsPanel");
    if (statsPanel) statsPanel.style.display = "";
    var url = new URL(window.location);
    url.searchParams.delete("company");
    history.replaceState({}, "", url);
  }

  state.currentMode = mode;
  const catalogSection = document.getElementById("catalogSection");
  const companiesSection = document.getElementById("companiesSection");
  const pipelineSection = document.getElementById("pipelineSection");
  const statsSection = document.getElementById("statsSection");
  const archiveSection = document.getElementById("archiveSection");

  document
    .getElementById("modeCatalog")
    .classList.toggle("active", mode === "catalog");
  document
    .getElementById("modeCompanies")
    .classList.toggle("active", mode === "companies");
  document
    .getElementById("modePipeline")
    .classList.toggle("active", mode === "pipeline");
  var modeStatsBtn = document.getElementById("modeStats");
  if (modeStatsBtn) modeStatsBtn.classList.toggle("active", mode === "stats");
  var modeArchiveBtn = document.getElementById("modeArchive");
  if (modeArchiveBtn)
    modeArchiveBtn.classList.toggle("active", mode === "archive");

  catalogSection.classList.toggle("active", mode === "catalog");
  companiesSection.classList.toggle("active", mode === "companies");
  if (pipelineSection)
    pipelineSection.classList.toggle("active", mode === "pipeline");
  if (statsSection) statsSection.classList.toggle("active", mode === "stats");
  if (archiveSection)
    archiveSection.classList.toggle("active", mode === "archive");

  // Lazy-load images for the activated section
  var sectionMap = {
    catalog: catalogSection,
    companies: companiesSection,
    pipeline: pipelineSection,
    stats: statsSection,
    archive: archiveSection,
  };
  var section = sectionMap[mode];
  if (section) {
    section.querySelectorAll("img[data-src]").forEach(function (img) {
      img.src = img.dataset.src;
      img.removeAttribute("data-src");
    });
  }

  if (mode === "catalog") {
    initCatalog();
  } else if (mode === "companies") {
    initCompanies();
  } else if (mode === "pipeline") {
    renderPipeline();
  } else if (mode === "stats") {
    initStats();
  } else if (mode === "archive") {
    initArchive();
  }
}

// ---------------------------------------------------------------------------
// Dynamic hero date formatting
// ---------------------------------------------------------------------------

(function updateHeroDate() {
  var el = document.getElementById("heroDate");
  if (!el) return;
  var raw = el.textContent.replace("Updated: ", "").trim();
  if (!raw || raw === "\u2014") return;
  try {
    var d = new Date(raw);
    if (!isNaN(d.getTime())) {
      el.textContent =
        "Updated: " +
        d.toLocaleDateString("en-US", {
          day: "numeric",
          month: "long",
          year: "numeric",
        }) +
        ", " +
        d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
    }
  } catch (e) {
    /* ignore parse errors */
  }
})();

// ---------------------------------------------------------------------------
// Browser back/forward for company profile
// ---------------------------------------------------------------------------

window.addEventListener("popstate", function () {
  var slug = getCompanySlugFromUrl();
  if (slug) {
    renderProfileForSlug(slug);
  } else if (state.currentProfileSlug) {
    hideProfile();
  }
});

// ---------------------------------------------------------------------------
// Expose functions to window for inline onclick (temporary — step 4 removes)
// ---------------------------------------------------------------------------

window.switchMode = switchMode;
window.switchBasket = switchBasket;
window.toggleCatalogLoc = toggleCatalogLoc;
window.toggleCatalogSort = toggleCatalogSort;
window.catalogThumbAction = catalogThumbAction;
window.toggleCatalogExpand = toggleCatalogExpand;
window.renderCatalog = renderCatalog;
window.renderCompanies = renderCompanies;
window.initCompanies = initCompanies;
window.sortCompanyTable = sortCompanyTable;
window.toggleCompanySort = toggleCompanySort;
window.switchCompanySubTab = switchCompanySubTab;
window.toggleCompanyCardFilter = toggleCompanyCardFilter;
window.toggleMonitoringChip = toggleMonitoringChip;
window.companyVacancyAction = companyVacancyAction;
window.reviewCompany = reviewCompany;
window.viewOrgInCatalog = viewOrgInCatalog;
window.openCompanyProfile = openCompanyProfile;
window.closeCompanyProfile = closeCompanyProfile;
window.renderPipeline = renderPipeline;
window.renderArchive = renderArchive;

// ---------------------------------------------------------------------------
// Init sequence
// ---------------------------------------------------------------------------

initApi();

function initDefault() {
  // Hide loading screen with fade-out animation
  var loadingEl = document.getElementById("catalogLoading");
  if (loadingEl) {
    loadingEl.classList.add("hidden");
    loadingEl.addEventListener("animationend", function () {
      loadingEl.remove();
    });
  }

  var initSlug = getCompanySlugFromUrl();
  if (initSlug && companiesBySlug.has(initSlug)) {
    state.currentMode = "companies";
    renderProfileForSlug(initSlug);
  } else {
    // Default mode: companies (company-first pipeline)
    switchMode(state.currentMode);
  }
}

if (!API_BASE) {
  // Local file mode — no API, render immediately with all-unseen
  state.statusesLoaded = true;
  state.companyStatusesLoaded = true;
  initDefault();
} else {
  // Subscribe BEFORE fetch to prevent race condition
  on("statusesLoaded", initDefault);
  on("companyStatusesLoaded", function () {
    if (state.currentMode === "companies") renderCompanies();
    // Re-render catalog with live company approval data
    updateBasketCounts();
    if (state.currentMode === "catalog") renderCatalog();
    if (state.currentMode === "stats") renderStats();
  });
  loadFromServer();
  loadCompanyStatuses();
  // Watchdog: force init after 5s if statusesLoaded never fires
  setTimeout(function () {
    if (!state.statusesLoaded) {
      console.warn("Status load timeout — forcing init");
      state.statusesLoaded = true;
      initDefault();
    }
  }, 5000);
}
