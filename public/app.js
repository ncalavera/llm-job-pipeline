// =============================================================================
// app.js — Entry point: initialization, mode switching, event wiring
// =============================================================================

import {
  state,
  API_BASE,
  config,
  companiesBySlug,
  on,
  emit,
  scheduleRender,
} from "./modules/state.js";
import { initUI, showToast } from "./modules/helpers.js";
import {
  applyI18n,
  T,
  setLanguage,
  getLanguage,
  availableLanguages,
} from "./modules/i18n.js";
import {
  initApi,
  loadFromServer,
  loadCompanyStatuses,
  loadCompanies,
} from "./modules/api.js";
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
import { initBoards } from "./modules/boards.js";

// ---------------------------------------------------------------------------
// Initialize UI elements (toast, scroll-to-top)
// ---------------------------------------------------------------------------

initUI();

// ---------------------------------------------------------------------------
// Language + illustration pack — apply the baked strings and pack images to the
// static shell before anything renders.
// ---------------------------------------------------------------------------

applyI18n();
renderLanguageSwitch();

// Render an EN/RU toggle into the top nav. Hidden when only one language is
// baked. Clicking persists the choice and reloads so every view re-renders.
function renderLanguageSwitch() {
  var langs = availableLanguages();
  if (langs.length < 2) return;
  var host = document.querySelector(".top-nav-right");
  if (!host) return;

  var wrap = document.createElement("div");
  wrap.className = "lang-switch";
  var active = getLanguage();
  langs.forEach(function (lang) {
    var btn = document.createElement("button");
    btn.className = "lang-btn" + (lang === active ? " lang-btn--active" : "");
    btn.textContent = lang.toUpperCase();
    btn.onclick = function () {
      setLanguage(lang);
    };
    wrap.appendChild(btn);
  });
  host.insertBefore(wrap, host.firstChild);
}

// ---------------------------------------------------------------------------
// Dashboard style — "illustrated" (default) or "minimal".
// Drives CSS via body[data-dashboard-style]; baked into data.js by the generator.
// ---------------------------------------------------------------------------

(function applyDashboardStyle() {
  var style = (config && config.dashboard_style) || "illustrated";
  if (style !== "illustrated" && style !== "minimal") style = "illustrated";
  document.body.dataset.dashboardStyle = style;
})();

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
  const boardsSection = document.getElementById("boardsSection");

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
  var modeBoardsBtn = document.getElementById("modeBoards");
  if (modeBoardsBtn)
    modeBoardsBtn.classList.toggle("active", mode === "boards");

  catalogSection.classList.toggle("active", mode === "catalog");
  companiesSection.classList.toggle("active", mode === "companies");
  if (pipelineSection)
    pipelineSection.classList.toggle("active", mode === "pipeline");
  if (statsSection) statsSection.classList.toggle("active", mode === "stats");
  if (archiveSection)
    archiveSection.classList.toggle("active", mode === "archive");
  if (boardsSection)
    boardsSection.classList.toggle("active", mode === "boards");

  // Lazy-load images for the activated section
  var sectionMap = {
    catalog: catalogSection,
    companies: companiesSection,
    pipeline: pipelineSection,
    stats: statsSection,
    archive: archiveSection,
    boards: boardsSection,
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
  } else if (mode === "boards") {
    initBoards();
  }
}

// ---------------------------------------------------------------------------
// Dynamic hero date formatting
// ---------------------------------------------------------------------------

(function updateHeroDate() {
  var el = document.getElementById("heroDate");
  if (!el) return;
  var prefix = T("updated_prefix", "Updated:");
  // Date source: any text already in the span, else the baked config timestamp.
  var raw = el.textContent.replace(prefix, "").replace("Updated:", "").trim();
  if (!raw || raw === "\u2014") {
    raw = (config && config.last_updated) || "";
  }
  if (!raw) return;
  var locale = (config && config.language) === "ru" ? "ru-RU" : "en-US";
  try {
    var d = new Date(raw);
    if (!isNaN(d.getTime())) {
      el.textContent =
        prefix +
        " " +
        d.toLocaleDateString(locale, {
          day: "numeric",
          month: "long",
          year: "numeric",
        }) +
        ", " +
        d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
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
  // Hide loading screen with fade-out animation. The loader lives inside the
  // (hidden by default) catalog section, so its CSS animation may never run and
  // "animationend" may never fire — guard with a timeout that removes it
  // unconditionally, otherwise the "Sorting vacancies…" overlay stays stuck.
  var loadingEl = document.getElementById("catalogLoading");
  if (loadingEl) {
    loadingEl.classList.add("hidden");
    var removeLoader = function () {
      if (loadingEl && loadingEl.parentNode) loadingEl.remove();
    };
    loadingEl.addEventListener("animationend", removeLoader);
    setTimeout(removeLoader, 500);
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
  // Live company rows (list, counts, tier, monitoring) — re-render when they
  // arrive so the Companies tab never shows the stale snapshot list.
  on("companiesLoaded", function () {
    if (state.currentMode === "companies") renderCompanies();
  });
  loadFromServer();
  loadCompanyStatuses();
  loadCompanies();
  // Watchdog: force init after 5s if statusesLoaded never fires
  setTimeout(function () {
    if (!state.statusesLoaded) {
      console.warn("Status load timeout — forcing init");
      state.statusesLoaded = true;
      initDefault();
    }
  }, 5000);
}
