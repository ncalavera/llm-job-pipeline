// =============================================================================
// app.js — Entry point: initialization, mode switching, event wiring
// =============================================================================

import {
  state,
  API_BASE,
  config,
  companiesBySlug,
  groups,
  STATUS_BASKET,
  getGroupStatus,
  isGroupCompanyApproved,
  getCompanies,
  on,
  emit,
  scheduleRender,
} from "./modules/state.js";
import { initUI, showToast, isVacancyExpired } from "./modules/helpers.js";
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
import { VISIBLE_MIN_SCORE, basketCounts } from "./modules/derive.js";
import {
  initCatalog,
  updateBasketCounts,
  renderCatalog,
  switchBasket,
  toggleCatalogLoc,
  toggleCatalogSort,
  toggleCatalogShowAll,
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
import { renderToday, todayAction } from "./modules/today.js";
import { initStats, renderStats } from "./modules/stats.js";
import { initArchive, renderArchive } from "./modules/archive.js";
import { initBoards, renderBoards } from "./modules/boards.js";
import {
  initApplications,
  renderApplications,
} from "./modules/applications.js";
import { initSettings, renderSettings } from "./modules/settings.js";
import {
  sectionForMode,
  isVacancyView,
  isApplicationsView,
  syncStatusLabelKey,
} from "./modules/nav.js";

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
// Nav counts/sync footer don't depend on statuses/companies finishing their
// load (groups/companies are already available — state.js destructures them
// synchronously from VACANCY_DATA), so paint them once immediately. This also
// covers the cold-deep-link-to-a-company path in initDefault() below, which
// renders the profile directly without going through switchMode().
updateNavCounts();
renderSyncFooter();

// Render an EN/RU toggle into the sidebar footer. Hidden when only one
// language is baked. Clicking persists the choice and reloads so every view
// re-renders.
function renderLanguageSwitch() {
  var langs = availableLanguages();
  if (langs.length < 2) return;
  var host = document.getElementById("sidebarLangHost");
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
// Sidebar nav counts (U3) — quiet mono counts derived client-side from live
// state. The Vacancies count reuses derive.js's basketCounts with the exact
// same visibility options catalog.js's own "Unreviewed" badge uses, so the
// sidebar can never disagree with what Browse shows (badge==list, DHA-374).
// Today's and Triage's counts are deliberately NOT shown yet: computing them
// correctly needs private, stateful helpers inside today.js/pipeline.js
// (today.js's "since last visit" cursor, pipeline.js's board bucketing) that
// aren't exported — pipeline's derivation is U12's job. Showing an
// approximate number that could disagree with those screens' own lists would
// violate the same badge==list invariant this reuses.
// ---------------------------------------------------------------------------

function navVisOpts() {
  return {
    isApproved: isGroupCompanyApproved,
    getStatus: getGroupStatus,
    isExpired: isVacancyExpired,
    basketMap: STATUS_BASKET,
    minScore: state.catalogShowAll ? null : VISIBLE_MIN_SCORE,
  };
}

function updateNavCounts() {
  var vacEl = document.getElementById("navCountVacancies");
  if (vacEl) vacEl.textContent = basketCounts(groups, navVisOpts()).unseen;
  var compEl = document.getElementById("navCountCompanies");
  if (compEl) compEl.textContent = getCompanies().length;
}

// ---------------------------------------------------------------------------
// Sidebar sync-status footer (U3) — reads state.sync (advanced by
// bootstrap.js on every poll outcome, see nav.js's state machine) and renders
// a fixed, translated label plus the existing "last updated" date. The label
// is ALWAYS one of the four fixed words below — never raw exception detail.
// ---------------------------------------------------------------------------

// Locale-aware "last updated" date (R16) — previously the top-nav's
// #heroDate; now folds into the sync line ("Live · 3 July 2026"),
// matching the design mock's "Synced · Jul 3" footer pattern.
function formattedUpdatedDate() {
  var raw = (config && config.last_updated) || "";
  if (!raw) return "";
  var locale = (config && config.language) === "ru" ? "ru-RU" : "en-US";
  try {
    var d = new Date(raw);
    if (!isNaN(d.getTime())) {
      return d.toLocaleDateString(locale, {
        day: "numeric",
        month: "long",
        year: "numeric",
      });
    }
  } catch (e) {
    /* ignore parse errors */
  }
  return "";
}

// Called at top-level on load (before this module's own const bindings below
// would be safe to touch, per JS's temporal dead zone) and again on every
// "render" — kept function-scoped so there's no module-level const for that
// early call to trip over.
function renderSyncFooter() {
  var SYNC_FALLBACK = {
    checking: "Checking…",
    ok: "Live",
    stale: "Stale snapshot",
    error: "Sync error",
  };
  var dot = document.getElementById("syncDot");
  var label = document.getElementById("syncLabel");
  if (!dot || !label) return;
  var status = (state.sync && state.sync.status) || "checking";
  dot.className = "sync-dot " + status;
  label.className = "nav-label sync-label " + status;
  var word = T(
    syncStatusLabelKey(status),
    SYNC_FALLBACK[status] || SYNC_FALLBACK.checking,
  );
  var date =
    status === "ok" || status === "stale" ? formattedUpdatedDate() : "";
  label.textContent = date ? word + " · " + date : word;
}

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
  updateNavCounts();
  renderSyncFooter();
  renderCatalog();
  if (state.currentMode === "today") renderToday();
  if (state.currentMode === "companies") renderCompanies();
  if (state.currentMode === "pipeline") renderPipeline();
  if (state.currentMode === "stats") renderStats();
  if (state.currentMode === "applications") renderApplications();
  if (state.currentMode === "settings") renderSettings();
  if (state.currentMode === "boards") renderBoards();
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
  // Remember the Vacancies/Applications sub-view so re-opening the section
  // returns to it.
  if (isVacancyView(mode)) state.vacancyView = mode;
  if (isApplicationsView(mode)) state.applicationsView = mode;
  var section = sectionForMode(mode);

  // DOM section per leaf mode (each is a sibling under .container).
  var sectionMap = {
    today: document.getElementById("todaySection"),
    catalog: document.getElementById("catalogSection"),
    companies: document.getElementById("companiesSection"),
    pipeline: document.getElementById("pipelineSection"),
    stats: document.getElementById("statsSection"),
    archive: document.getElementById("archiveSection"),
    boards: document.getElementById("boardsSection"),
    applications: document.getElementById("applicationsSection"),
    settings: document.getElementById("settingsSection"),
  };
  Object.keys(sectionMap).forEach(function (leaf) {
    var el = sectionMap[leaf];
    if (el) el.classList.toggle("active", leaf === mode);
  });

  // Sidebar section active state follows the SECTION, not the leaf (so the
  // whole Vacancies/Applications hub stays highlighted across its sub-views).
  // Shared by the desktop sidebar and the narrow icon rail (same elements).
  var navBtns = {
    today: "navToday",
    vacancies: "navVacancies",
    companies: "navCompanies",
    applications: "navApplications",
    boards: "navBoards",
    settings: "navSettings",
  };
  Object.keys(navBtns).forEach(function (sec) {
    var btn = document.getElementById(navBtns[sec]);
    if (btn) btn.classList.toggle("active", sec === section);
  });

  // Sidebar sub-items are always expanded inline (Linear-style, protocol
  // section 4) — no visibility toggle, just the active state tracking the
  // leaf.
  var subBtns = {
    catalog: "navSubBrowse",
    stats: "navSubGeo",
    archive: "navSubArchive",
    applications: "navSubApplied",
    pipeline: "navSubTriage",
  };
  Object.keys(subBtns).forEach(function (leaf) {
    var btn = document.getElementById(subBtns[leaf]);
    if (btn) btn.classList.toggle("active", leaf === mode);
  });

  // Narrow-width (<900px) pill row: the sidebar collapses to an icon rail
  // there, so this becomes the way to reach a hub's sub-views. CSS hides both
  // rows entirely at desktop widths, where the sidebar's inline sub-items
  // above already cover this — only one row is ever "active" at a time.
  var subNav = document.getElementById("vacancySubNav");
  if (subNav) subNav.classList.toggle("active", section === "vacancies");
  var vsubBtns = {
    catalog: "vsubBrowse",
    stats: "vsubGeo",
    archive: "vsubArchive",
  };
  Object.keys(vsubBtns).forEach(function (leaf) {
    var btn = document.getElementById(vsubBtns[leaf]);
    if (btn) btn.classList.toggle("active", leaf === mode);
  });

  var appsSubNav = document.getElementById("applicationsSubNav");
  if (appsSubNav)
    appsSubNav.classList.toggle("active", section === "applications");
  var asubBtns = {
    applications: "asubApplications",
    pipeline: "asubTriage",
  };
  Object.keys(asubBtns).forEach(function (leaf) {
    var btn = document.getElementById(asubBtns[leaf]);
    if (btn) btn.classList.toggle("active", leaf === mode);
  });

  updateNavCounts();

  // Lazy-load images for the activated section.
  var activeSection = sectionMap[mode];
  if (activeSection) {
    activeSection.querySelectorAll("img[data-src]").forEach(function (img) {
      img.src = img.dataset.src;
      img.removeAttribute("data-src");
    });
  }

  if (mode === "today") {
    renderToday();
  } else if (mode === "catalog") {
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
  } else if (mode === "applications") {
    initApplications();
  } else if (mode === "settings") {
    initSettings();
  }
}

// The Vacancies top-nav button opens the remembered sub-view (default Browse).
function switchVacancies() {
  switchMode(state.vacancyView || "catalog");
}

// The Applications top-nav button opens the remembered sub-view (default
// Applications).
function switchApplications() {
  switchMode(state.applicationsView || "applications");
}

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
window.switchVacancies = switchVacancies;
window.switchApplications = switchApplications;
window.switchBasket = switchBasket;
window.toggleCatalogLoc = toggleCatalogLoc;
window.toggleCatalogSort = toggleCatalogSort;
window.toggleCatalogShowAll = toggleCatalogShowAll;
window.catalogThumbAction = catalogThumbAction;
window.todayAction = todayAction;
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
window.renderToday = renderToday;
window.renderArchive = renderArchive;
window.renderApplications = renderApplications;
window.renderSettings = renderSettings;

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
    if (state.currentMode === "today") renderToday();
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
