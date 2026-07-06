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
  dashboardSource,
  on,
  emit,
  scheduleRender,
} from "./modules/state.js";
import {
  initUI,
  showToast,
  isVacancyExpired,
  escHtml,
} from "./modules/helpers.js";
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
  openCatalogRow,
} from "./modules/catalog.js";
import {
  initCompanies,
  renderCompanies,
  sortCompanyTable,
  toggleCompanySort,
  switchCompanySubTab,
  toggleMonitoringChip,
  reviewCompany,
  viewOrgInCatalog,
  openCompanyProfile,
  closeCompanyProfile,
  renderProfileForSlug,
} from "./modules/companies.js";
import { renderPipeline } from "./modules/pipeline.js";
import { renderToday, todayAction, openTodayRow } from "./modules/today.js";
import { initStats, renderStats } from "./modules/stats.js";
import { initArchive, renderArchive } from "./modules/archive.js";
import { initBoards, renderBoards, toggleBoard } from "./modules/boards.js";
import {
  initApplications,
  renderApplications,
  openApplicationRow,
} from "./modules/applications.js";
import { initSettings, renderSettings } from "./modules/settings.js";
import {
  sectionForMode,
  isVacancyView,
  isApplicationsView,
  syncStatusLabelKey,
  DEFAULT_VACANCY_VIEW,
  fallbackBannerState,
} from "./modules/nav.js";
import { parse, build } from "./modules/route.js";
import {
  renderVacancyDetail,
  vacancyLike,
  vacancyPass,
  vacancyResearch,
  vacancyNetwork,
  vacancyMoveToApply,
} from "./modules/vacancy.js";
import {
  filterPalette,
  flatKeys,
  routeForResult,
  resultsHtml,
  createSelection,
} from "./modules/palette.js";

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
renderFallbackBanner();

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
// Fallback banner (DHA-422) — a prominent, dismissible notice shown only when
// boot() loaded the baked public/data.js instead of live /api/vacancies (see
// nav.js's fallbackBannerState for why this reads `dashboardSource`, not the
// sidebar's sync-status word). `dashboardSource` and `config.last_updated` are
// both fixed for the life of the page (state.js: "immutable after init"), so
// this renders once at load — no need to re-run it on every "render" tick the
// way the sync footer does.
// ---------------------------------------------------------------------------

var fallbackBannerDismissed = false;

function renderFallbackBanner() {
  var el = document.getElementById("fallbackBanner");
  var textEl = document.getElementById("fallbackBannerText");
  if (!el || !textEl) return;
  var result = fallbackBannerState(
    dashboardSource,
    config && config.last_updated,
    Date.now(),
  );
  if (fallbackBannerDismissed || !result.show) {
    el.hidden = true;
    return;
  }
  el.className =
    "fallback-banner" +
    (result.level === "warning" ? " fallback-banner--warning" : "");
  var message =
    result.age === "unknown"
      ? T(
          "fallback_banner_unknown",
          "Offline snapshot (age unknown) — live API unavailable.",
        )
      : T(
          "fallback_banner_known",
          "Offline snapshot from {date} — live API unavailable.",
        ).replace("{date}", formattedUpdatedDate());
  if (result.level === "warning" && result.age === "known") {
    message +=
      " " + T("fallback_banner_stale_suffix", "Data may be out of date.");
  }
  textEl.textContent = message;
  el.hidden = false;
}

var fallbackBannerDismissBtn = document.getElementById("fallbackBannerDismiss");
if (fallbackBannerDismissBtn) {
  fallbackBannerDismissBtn.setAttribute(
    "aria-label",
    T("fallback_banner_dismiss", "Dismiss"),
  );
  fallbackBannerDismissBtn.addEventListener("click", function () {
    fallbackBannerDismissed = true;
    renderFallbackBanner();
  });
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

on("switchToCatalog", ({ orgFilter, locSearch }) => {
  switchMode("catalog");
  // Two independent entry points share this seam: the company profile's
  // "view roles in Browse" passes orgFilter; the Geo table's row click passes
  // locSearch (a city/country term dropped into Browse's search box, which
  // already matches location text). Each key is applied only when present so
  // one caller never clobbers the other's field.
  if (orgFilter !== undefined) {
    const sel = document.getElementById("catalogOrgFilter");
    if (sel) sel.value = orgFilter;
  }
  if (locSearch !== undefined) {
    const search = document.getElementById("catalogSearch");
    if (search) search.value = locSearch;
  }
  renderCatalog();
});

// Lightweight repaint for every poll outcome (bootstrap.js's applyPollResult,
// U3 review fix) — sidebar footer + nav counts only, deliberately NOT the
// full "render" handler above. A poll firing every 60s regardless of whether
// data changed must never rebuild the section grids: that would blow away
// DOM-only state a snapshot replace never touches (the triage board's
// SortableJS-managed DOM).
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
    '<div style="margin-top:16px"><button class="nf-back-btn" onclick="' +
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
  // leaf. Applications has none (post-ship fast fix collapsed them).
  var subBtns = {
    catalog: "navSubBrowse",
    stats: "navSubGeo",
    archive: "navSubArchive",
  };
  Object.keys(subBtns).forEach(function (leaf) {
    var btn = document.getElementById(subBtns[leaf]);
    if (btn) btn.classList.toggle("active", leaf === mode);
  });

  // Narrow-width (<900px) pill row: the sidebar collapses to an icon rail
  // there, so this becomes the way to reach Vacancies' sub-views (Applications'
  // own pill row was removed the same way as its inline sub-items above).
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

// The Applications top-nav button opens Triage directly (post-ship fast fix —
// its sub-nav affordances are gone, so there's no remembered sub-view to
// return to). Applied stays reachable via whatever already routes to it
// directly (palette, nav parent mapping) — see nav.js.
function switchApplications() {
  switchMode("pipeline");
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
window.openTodayRow = openTodayRow;
window.openCatalogRow = openCatalogRow;
window.openApplicationRow = openApplicationRow;
window.renderCatalog = renderCatalog;
window.renderCompanies = renderCompanies;
window.initCompanies = initCompanies;
window.sortCompanyTable = sortCompanyTable;
window.toggleCompanySort = toggleCompanySort;
window.switchCompanySubTab = switchCompanySubTab;
window.toggleMonitoringChip = toggleMonitoringChip;
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
window.vacancyResearch = vacancyResearch;
window.vacancyNetwork = vacancyNetwork;
window.vacancyMoveToApply = vacancyMoveToApply;
window.renderPipeline = renderPipeline;
window.renderToday = renderToday;
window.renderArchive = renderArchive;
window.renderApplications = renderApplications;
// Boards section (inline onclick on each board's enabled toggle).
window.toggleBoard = toggleBoard;
window.renderSettings = renderSettings;

// ---------------------------------------------------------------------------
// ⌘K command palette (U17, DHA-401) — thin DOM shell over palette.js's pure
// filter/selection/markup. The overlay is built once and appended to <body>
// (like initUI's toast), so no markup lands in index.html.
//
// Data binding (KTD7): results recompute from the LIVE `groups`/getCompanies()
// on every keystroke and on every "render" (a 60s poll's applySnapshot fires
// one) — never a cached snapshot. The highlight is keyed by result id/slug
// (palette.js's createSelection, mirroring keys.js), so a poll that reorders
// results under a pending Enter cannot change which result opens.
//
// Focus (a11y): the palette is a combobox — focus stays on the input the whole
// time it is open (a genuine trap: Tab/Shift+Tab never leave the dialog, they
// walk the result highlight instead), and the active option is announced via
// aria-activedescendant on the listbox. Esc / backdrop-click close and return
// focus to the element that opened it.
// ---------------------------------------------------------------------------

var _paletteOverlay = null;
var _paletteDialog = null;
var _paletteInput = null;
var _paletteResults = null;
var _paletteOpen = false;
var _paletteTrigger = null; // element focus returns to on close
var _paletteFlat = []; // current flat results (index → result), for choose/hover
var _restoringFocus = false; // guards the sidebar-search focus handler on close
var _paletteSelection = createSelection();

function buildPaletteDom() {
  if (_paletteOverlay) return;
  _paletteOverlay = document.createElement("div");
  _paletteOverlay.className = "palette-overlay";
  _paletteOverlay.id = "commandPalette";

  _paletteDialog = document.createElement("div");
  _paletteDialog.className = "palette-dialog";
  _paletteDialog.setAttribute("role", "dialog");
  _paletteDialog.setAttribute("aria-modal", "true");
  _paletteDialog.setAttribute("aria-label", T("nav_search", "Search"));

  var inputWrap = document.createElement("div");
  inputWrap.className = "palette-input-wrap";
  _paletteInput = document.createElement("input");
  _paletteInput.type = "text";
  _paletteInput.className = "palette-input";
  _paletteInput.id = "paletteInput";
  _paletteInput.setAttribute("role", "combobox");
  _paletteInput.setAttribute("aria-expanded", "false");
  _paletteInput.setAttribute("aria-controls", "paletteResults");
  _paletteInput.setAttribute("aria-autocomplete", "list");
  _paletteInput.setAttribute("autocomplete", "off");
  _paletteInput.setAttribute("spellcheck", "false");
  _paletteInput.setAttribute(
    "aria-label",
    T("palette_placeholder", "Search vacancies and companies…"),
  );
  _paletteInput.placeholder = T(
    "palette_placeholder",
    "Search vacancies and companies…",
  );
  inputWrap.appendChild(_paletteInput);

  _paletteResults = document.createElement("div");
  _paletteResults.className = "palette-results";
  _paletteResults.id = "paletteResults";
  _paletteResults.setAttribute("role", "listbox");
  _paletteResults.setAttribute("aria-label", T("nav_search", "Search"));

  _paletteDialog.appendChild(inputWrap);
  _paletteDialog.appendChild(_paletteResults);
  _paletteOverlay.appendChild(_paletteDialog);
  document.body.appendChild(_paletteOverlay);

  _paletteInput.addEventListener("input", function () {
    renderPaletteResults(true); // a new query resets the highlight to the top
  });
  _paletteDialog.addEventListener("keydown", paletteKeydown);
  // Pin focus to the input: a mousedown on any non-input chrome (padding, group
  // labels, an option, the empty state) would otherwise blur the input, after
  // which Esc (the keydown listener is on the dialog, and focus jumps to body)
  // and the Tab trap both stop working. Prevent that default focus shift; an
  // option's click still fires (click is separate from mousedown), so choosing
  // a result is unaffected.
  _paletteDialog.addEventListener("mousedown", function (e) {
    if (e.target !== _paletteInput) e.preventDefault();
  });
  // Backdrop click (outside the dialog) closes.
  _paletteOverlay.addEventListener("click", function (e) {
    if (e.target === _paletteOverlay) closePalette();
  });
}

// Adapt the LIVE payload into palette.js's input shape at call time (never a
// cached snapshot — KTD7). Vacancies carry the group's fit score; companies the
// alignment (fit) score. Both fall back cleanly when a score is absent.
function paletteData() {
  return {
    vacancies: groups.map(function (g) {
      return {
        id: g.id,
        title: g.title,
        org: g.company_name || g.org,
        score: g.llm_score,
      };
    }),
    companies: getCompanies().map(function (c) {
      return { slug: c.slug, name: c.name, score: c.alignment_score };
    }),
  };
}

// Re-run the filter from live data + the current query and repaint. `reset`
// true (a keystroke) highlights the top match; false (a data refresh) reconciles
// the existing highlight by id (KTD7).
function renderPaletteResults(reset) {
  if (!_paletteOpen) return;
  var query = _paletteInput.value || "";
  var fr = filterPalette(query, paletteData());
  _paletteFlat = fr.flat;
  var keys = flatKeys(fr);
  if (reset) _paletteSelection.reset(keys);
  else _paletteSelection.reconcile(keys);

  if (!query.trim()) {
    _paletteResults.innerHTML =
      '<div class="palette-empty">' +
      escHtml(T("palette_hint", "Type to search vacancies and companies.")) +
      "</div>";
  } else if (fr.flat.length === 0) {
    _paletteResults.innerHTML =
      '<div class="palette-empty">' +
      escHtml(T("palette_no_results", "No matches.")) +
      "</div>";
  } else {
    _paletteResults.innerHTML = resultsHtml(fr, _paletteSelection.index, {
      labelVacancies: T("palette_group_vacancies", "Vacancies"),
      labelCompanies: T("palette_group_companies", "Companies"),
    });
  }
  applyPaletteActive();
}

// Repaint the active option without rebuilding the list (arrow / hover moves):
// toggle the selected class + aria-selected by index and point the input's
// aria-activedescendant at it, scrolling it into view.
function applyPaletteActive() {
  if (!_paletteResults) return;
  var activeIdx = _paletteSelection.index;
  _paletteResults.querySelectorAll(".palette-option").forEach(function (el) {
    var idx = Number(el.dataset.idx);
    var on = idx === activeIdx;
    el.classList.toggle("palette-option--active", on);
    el.setAttribute("aria-selected", on ? "true" : "false");
    if (on && typeof el.scrollIntoView === "function")
      el.scrollIntoView({ block: "nearest" });
  });
  if (activeIdx >= 0)
    _paletteInput.setAttribute(
      "aria-activedescendant",
      "palette-opt-" + activeIdx,
    );
  else _paletteInput.removeAttribute("aria-activedescendant");
}

function paletteKeydown(e) {
  if (e.isComposing) return;
  var key = e.key;
  if (key === "Escape") {
    e.preventDefault();
    closePalette();
    return;
  }
  if (key === "ArrowDown" || (key === "Tab" && !e.shiftKey)) {
    e.preventDefault();
    _paletteSelection.move(1, flatKeys({ flat: _paletteFlat }));
    applyPaletteActive();
    return;
  }
  if (key === "ArrowUp" || (key === "Tab" && e.shiftKey)) {
    e.preventDefault();
    _paletteSelection.move(-1, flatKeys({ flat: _paletteFlat }));
    applyPaletteActive();
    return;
  }
  if (key === "Enter") {
    e.preventDefault();
    var idx = _paletteSelection.index;
    if (idx >= 0) paletteChoose(idx);
    return;
  }
}

// Hide the overlay + reset palette state WITHOUT touching focus. Shared by the
// close path (which then returns focus to the trigger) and choose (which
// navigates away, so focus follows the detail page).
function hidePaletteOverlay() {
  _paletteOpen = false;
  _paletteFlat = [];
  _paletteSelection.clear();
  if (_paletteOverlay) _paletteOverlay.classList.remove("active");
  if (_paletteInput) {
    _paletteInput.setAttribute("aria-expanded", "false");
    _paletteInput.removeAttribute("aria-activedescendant");
  }
  if (_paletteResults) _paletteResults.innerHTML = "";
}

function openPalette(trigger) {
  buildPaletteDom();
  if (_paletteOpen) {
    _paletteInput.focus();
    return;
  }
  _paletteOpen = true;
  _paletteTrigger = trigger || document.activeElement || document.body;
  _paletteOverlay.classList.add("active");
  _paletteInput.value = "";
  _paletteInput.setAttribute("aria-expanded", "true");
  renderPaletteResults(true); // empty query → the "type to search" hint
  _paletteInput.focus();
}

function closePalette() {
  if (!_paletteOpen) return;
  var trigger = _paletteTrigger;
  _paletteTrigger = null;
  hidePaletteOverlay();
  // Return focus to whatever opened the palette. Guard the sidebar-search focus
  // handler so this programmatic focus can't immediately reopen it.
  if (trigger && typeof trigger.focus === "function") {
    _restoringFocus = true;
    try {
      trigger.focus();
    } finally {
      _restoringFocus = false;
    }
  }
}

// Open the highlighted (or clicked) result's detail page and dismiss the
// palette. Routes via the SAME window entries a row click uses — with the
// "palette" context so the vacancy page confirms in place (no Browse-queue
// auto-advance, F3). Navigation takes over focus, so we don't restore it.
function paletteChoose(idx) {
  var route = routeForResult(_paletteFlat[idx]);
  if (!route) return;
  hidePaletteOverlay();
  _paletteTrigger = null;
  if (route.kind === "vacancy")
    window.openVacancyRoute(route.id, { context: "palette" });
  else window.openCompanyProfile(route.slug);
}

// Mouse hover moves the highlight to the pointed-at option (id-keyed set).
function paletteHover(idx) {
  var r = _paletteFlat[idx];
  if (!r) return;
  _paletteSelection.set(r.key, flatKeys({ flat: _paletteFlat }));
  applyPaletteActive();
}

window.paletteChoose = paletteChoose;
window.paletteHover = paletteHover;

// Global ⌘K / Ctrl+K opens the palette. U15's browse keydown ignores meta/ctrl
// combos (catalog.js), so this cannot double-fire with j/k/l/x triage.
document.addEventListener("keydown", function (e) {
  if (
    (e.metaKey || e.ctrlKey) &&
    !e.altKey &&
    (e.key === "k" || e.key === "K")
  ) {
    e.preventDefault();
    openPalette(document.activeElement);
  }
});

// The sidebar search input is now a real palette trigger. It stays `readonly`
// (kept in index.html): a readonly input still receives focus/click, so it can
// open the palette, and readonly stops the dead-typing state after Esc-close
// returns focus to it. Focusing or clicking it opens the palette and forwards
// focus to the palette input; the _restoringFocus guard stops the close-time
// focus-return from reopening it.
(function wireSidebarSearch() {
  var input = document.getElementById("sidebarSearch");
  if (!input) return;
  input.setAttribute("role", "button");
  input.setAttribute("aria-haspopup", "dialog");
  input.setAttribute("aria-keyshortcuts", "Meta+K Control+K");
  var open = function (e) {
    if (_restoringFocus) return;
    if (e) e.preventDefault();
    openPalette(input);
  };
  input.addEventListener("focus", open);
  input.addEventListener("click", open);
})();

// KTD7: a mid-open data refresh (applySnapshot → scheduleRender → "render")
// recomputes the palette from the fresh data and reconciles the highlight by
// id, so it survives the swap. Registered after the main "render" handler; both
// fire, order-independent.
on("render", function () {
  if (_paletteOpen) renderPaletteResults(false);
});

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
