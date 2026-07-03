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
  renderProfileForSlug,
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
  DEFAULT_VACANCY_VIEW,
} from "./modules/nav.js";
import { parse, build } from "./modules/route.js";
import {
  renderVacancyDetail,
  vacancyLike,
  vacancyPass,
  vacancyMoveToApply,
} from "./modules/vacancy.js";

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
  showToast(status, T);
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
  if (state.currentVacancyId) renderVacancyDetail(state.currentVacancyId);
});

on("switchToCatalog", ({ orgFilter }) => {
  switchMode("catalog");
  const sel = document.getElementById("catalogOrgFilter");
  if (sel) {
    sel.value = orgFilter;
    renderCatalog();
  }
});

// Lightweight repaint for every poll outcome (bootstrap.js's applyPollResult,
// U3 review fix) — sidebar footer + nav counts only, deliberately NOT the
// full "render" handler above. A poll firing every 60s regardless of whether
// data changed must never rebuild the section grids: that would blow away
// DOM-only state a snapshot replace never touches (an expanded catalog card,
// the triage board's SortableJS-managed DOM).
on("sync", () => {
  updateNavCounts();
  renderSyncFooter();
});

// ---------------------------------------------------------------------------
// Routing (U4) — deep-linkable detail pages + back/forward.
//
// Two detail surfaces overlay the section list views: the company profile
// (#companyProfile, ?company=<slug>) and the vacancy detail page
// (#vacancyDetail, ?vacancy=<group-id>). route.js is the single URL parser;
// this block is the thin DOM shell that reconciles the visible overlay/section
// to the URL. Opening a detail pushState()s (browser back returns); switching
// sections replaceState()s a bare URL (KTD1). Back/forward only toggles
// `.active` — it never re-renders a list, so the list DOM and whole-window
// scroll survive (the browser restores the position).
// ---------------------------------------------------------------------------

// Leaf mode → its section element id (each is a sibling under .container).
var LEAF_SECTION_ID = {
  today: "todaySection",
  catalog: "catalogSection",
  companies: "companiesSection",
  pipeline: "pipelineSection",
  stats: "statsSection",
  archive: "archiveSection",
  boards: "boardsSection",
  applications: "applicationsSection",
  settings: "settingsSection",
};

// Top-nav section → its sidebar button id (desktop sidebar + narrow icon rail
// share the same elements).
var NAV_BTNS = {
  today: "navToday",
  vacancies: "navVacancies",
  companies: "navCompanies",
  applications: "navApplications",
  boards: "navBoards",
  settings: "navSettings",
};

// Highlight one top-nav section (parent-active model, nav.js). Used by both
// switchMode and the router so a cold ?vacancy=/?company= deep link lights up
// the owning section (AE6).
function setNavActiveSection(section) {
  Object.keys(NAV_BTNS).forEach(function (sec) {
    var btn = document.getElementById(NAV_BTNS[sec]);
    if (btn) btn.classList.toggle("active", sec === section);
  });
}

function hideAllLeafSections() {
  Object.keys(LEAF_SECTION_ID).forEach(function (mode) {
    var el = document.getElementById(LEAF_SECTION_ID[mode]);
    if (el) el.classList.remove("active");
  });
}

// Reveal the current mode's leaf section by display toggle only — NO re-render,
// so returning from a detail keeps the list DOM and scroll intact (KTD1).
function showCurrentLeafSection() {
  var el = document.getElementById(LEAF_SECTION_ID[state.currentMode]);
  if (el) el.classList.add("active");
}

function clearCompanyOverlay() {
  state.currentProfileSlug = null;
  var el = document.getElementById("companyProfile");
  if (el) {
    el.classList.remove("active");
    el.innerHTML = "";
  }
  var statsPanel = document.getElementById("statsPanel");
  if (statsPanel) statsPanel.style.display = "";
}

function clearVacancyOverlay() {
  state.currentVacancyId = null;
  // Drop the auto-advance entry context too, so a later open that isn't a
  // Browse-list entry can't inherit a stale queue (F3).
  state.vacancyEntry = null;
  var el = document.getElementById("vacancyDetail");
  if (el) {
    el.classList.remove("active");
    el.innerHTML = "";
  }
}

// Drop any open detail overlay and strip its URL param. Called at the top of
// switchMode: a section switch always returns to a list. replaceState (not
// push) so the section itself adds no history entry; only touch the URL when an
// overlay was actually showing, matching the previous profile-close behavior.
function closeDetailOverlays() {
  var cp = document.getElementById("companyProfile");
  var vd = document.getElementById("vacancyDetail");
  var wasOpen =
    (cp && cp.classList.contains("active")) ||
    (vd && vd.classList.contains("active"));
  clearCompanyOverlay();
  clearVacancyOverlay();
  if (wasOpen) {
    var url = new URL(window.location);
    url.searchParams.delete("company");
    url.searchParams.delete("vacancy");
    history.replaceState({}, "", url);
  }
}

// Fixed, parameter-free not-found / placeholder panel. The raw URL value is
// NEVER interpolated (security + AE6 constraint) — copy is i18n-only, and the
// back button routes to the owning section's default entry.
function notFoundPanelHtml(section) {
  var backOnclick =
    section === "companies" ? "switchMode('companies')" : "switchVacancies()";
  return (
    '<div class="catalog-empty">' +
    '<div class="catalog-empty-icon">🔍</div>' +
    "<strong>" +
    T("route_not_found", "Not found") +
    "</strong>" +
    '<div class="catalog-empty-hint">' +
    T("route_not_found_hint", "This page doesn’t exist or hasn’t loaded.") +
    "</div>" +
    '<div style="margin-top:16px"><button class="cp-back-btn" onclick="' +
    backOnclick +
    '">' +
    T("route_back", "Go back") +
    "</button></div>" +
    "</div>"
  );
}

// The vacancy detail body is rendered by public/modules/vacancy.js (U6),
// imported above; U4's fixed placeholder is retired. A missing/gone id renders
// vacancy.js's own fixed not-found panel, so a valid deep link never flashes
// "not found".

// Show the vacancy detail overlay (used by openVacancyRoute, popstate, and the
// cold-deep-link path). Hides the company overlay + all leaf sections first.
// Clears any entry context so a popstate/cold load defaults to no auto-advance
// (F3); openVacancyRoute re-sets it after this call for Browse-list entries.
function showVacancyDetail(id) {
  state.currentVacancyId = id;
  state.vacancyEntry = null;
  var cp = document.getElementById("companyProfile");
  if (cp) {
    cp.classList.remove("active");
    cp.innerHTML = "";
  }
  state.currentProfileSlug = null;
  hideAllLeafSections();
  renderVacancyDetail(id);
  var host = document.getElementById("vacancyDetail");
  if (host) host.classList.add("active");
}

// Open a vacancy detail page from a row/action (pushState so back returns to
// the originating list). Exposed on window for U6's rows + actions to call.
// `opts.context` records HOW it was entered: "browse" (with `opts.queue`, the
// ordered unreviewed id list) enables "Move to apply" auto-advance (F3); any
// other entry omits opts and confirms in place. `opts.replace` swaps the
// current history entry instead of pushing a new one — the auto-advance chain
// uses it so walking a whole queue stays ONE history entry and Back from any
// hop returns straight to the originating list (F1), not step-by-step.
function openVacancyRoute(id, opts) {
  if (!id) return;
  var url = new URL(window.location);
  url.search = build({ screen: "vacancy", id: id });
  // inApp marks that a list entry sits beneath this one, so closeDetail can
  // step back (history.back) instead of pushing a bare entry that browser Back
  // would then reopen the detail from.
  var histState = { vacancy: id, inApp: true };
  if (opts && opts.replace) {
    history.replaceState(histState, "", url);
  } else {
    history.pushState(histState, "", url);
  }
  showVacancyDetail(id);
  state.vacancyEntry =
    opts && opts.context
      ? { context: opts.context, queue: (opts.queue || []).slice() }
      : null;
  setNavActiveSection(sectionForMode("vacancy"));
}

// Close the vacancy detail (U6's back button). If this entry was pushed in-app
// (a list sits beneath), step back so popstate restores it and no forward entry
// lingers to reopen the detail. On a cold deep link (no in-app entry beneath)
// there is nothing to go back to, so replace the URL with a bare one in place.
function closeDetail() {
  if (history.state && history.state.inApp) {
    history.back();
    return;
  }
  var url = new URL(window.location);
  url.searchParams.delete("vacancy");
  url.searchParams.delete("company");
  history.pushState({}, "", url);
  clearCompanyOverlay();
  clearVacancyOverlay();
  showCurrentLeafSection();
  setNavActiveSection(sectionForMode(state.currentMode));
}

// Reconcile the visible overlay/section to the current URL. Drives popstate.
function applyRouteFromUrl() {
  var route = parse(window.location.search);
  if (route.screen === "vacancy") {
    showVacancyDetail(route.id);
    setNavActiveSection(sectionForMode("vacancy"));
  } else if (route.screen === "company" && companiesBySlug.has(route.id)) {
    clearVacancyOverlay();
    renderProfileForSlug(route.id);
    setNavActiveSection(sectionForMode("company"));
  } else if (route.screen === "company") {
    // Unknown slug — fixed, parameter-free not-found in the company overlay.
    clearVacancyOverlay();
    state.currentProfileSlug = null;
    hideAllLeafSections();
    var cp = document.getElementById("companyProfile");
    if (cp) {
      cp.innerHTML = notFoundPanelHtml("companies");
      cp.classList.add("active");
    }
    setNavActiveSection(sectionForMode("company"));
  } else {
    // Bare URL — drop any overlay, reveal the current leaf list (no re-render).
    clearCompanyOverlay();
    clearVacancyOverlay();
    showCurrentLeafSection();
    setNavActiveSection(sectionForMode(state.currentMode));
  }
}

// ---------------------------------------------------------------------------
// Mode switching
// ---------------------------------------------------------------------------

function switchMode(mode) {
  // A section switch always drops any open detail overlay (company profile or
  // vacancy detail / not-found) and returns to a list.
  closeDetailOverlays();

  state.currentMode = mode;
  // Remember the Vacancies/Applications sub-view so re-opening the section
  // returns to it.
  if (isVacancyView(mode)) state.vacancyView = mode;
  if (isApplicationsView(mode)) state.applicationsView = mode;
  var section = sectionForMode(mode);

  // DOM section per leaf mode (each is a sibling under .container).
  Object.keys(LEAF_SECTION_ID).forEach(function (leaf) {
    var el = document.getElementById(LEAF_SECTION_ID[leaf]);
    if (el) el.classList.toggle("active", leaf === mode);
  });

  // Sidebar section active state follows the SECTION, not the leaf (so the
  // whole Vacancies/Applications hub stays highlighted across its sub-views).
  // Shared by the desktop sidebar and the narrow icon rail (same elements).
  setNavActiveSection(section);

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
  var activeSection = document.getElementById(LEAF_SECTION_ID[mode]);
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
// Browser back/forward — reconcile both detail surfaces to the URL (U4).
// ---------------------------------------------------------------------------

window.addEventListener("popstate", applyRouteFromUrl);

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
// Vacancy detail routing (U4) — U6's rows/actions call openVacancyRoute(id);
// its back button calls closeDetail().
window.openVacancyRoute = openVacancyRoute;
window.closeDetail = closeDetail;
// Vacancy detail page actions (U6) — inline onclick on the page's buttons.
window.vacancyLike = vacancyLike;
window.vacancyPass = vacancyPass;
window.vacancyMoveToApply = vacancyMoveToApply;
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

  // Cold deep link — resolve the URL through the same route parser popstate
  // uses. For a detail route, render the underlying list first (so back/close
  // lands on a real list and the sidebar highlights the owning section — AE6),
  // then overlay the detail.
  var route = parse(window.location.search);
  if (route.screen === "vacancy") {
    state.currentMode = state.vacancyView || DEFAULT_VACANCY_VIEW;
    switchMode(state.currentMode);
    showVacancyDetail(route.id);
    setNavActiveSection(sectionForMode("vacancy"));
  } else if (route.screen === "company" && companiesBySlug.has(route.id)) {
    state.currentMode = "companies";
    renderProfileForSlug(route.id);
    setNavActiveSection(sectionForMode("company"));
  } else if (route.screen === "company") {
    // Unknown slug — fixed not-found over the Companies list.
    state.currentMode = "companies";
    switchMode("companies");
    hideAllLeafSections();
    var cp = document.getElementById("companyProfile");
    if (cp) {
      cp.innerHTML = notFoundPanelHtml("companies");
      cp.classList.add("active");
    }
    setNavActiveSection(sectionForMode("company"));
  } else {
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
