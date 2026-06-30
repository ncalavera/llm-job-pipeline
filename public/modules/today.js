// =============================================================================
// today.js — the "Today" cockpit: the few things that need a decision today.
// =============================================================================
//
// Three action lists + the decision-SLA health panel:
//   1. Expiring, needs a decision — status='expiring' + active roles with a
//      deadline within SOON_DAYS.
//   2. Ready to send — status='to_apply' (KTD7: v1 keys on status only).
//   3. New 70+ — roles scoring >= NEW_HIGH_FIT first seen since the last visit
//      (tracked in localStorage; advances on load).
// The SLA panel (stuck + weekly leakage) comes from the server-computed
// VACANCY_DATA.latency_metrics. All display copy resolves through T(); the
// English fallbacks here keep the public shell Cyrillic-free.

import {
  groups,
  getGroupStatus,
  STATUS_BASKET,
  isGroupCompanyApproved,
} from "./state.js";
import { escHtml, isVacancyExpired } from "./helpers.js";
import { T } from "./i18n.js";

const SOON_DAYS = 7; // a deadline this close is "decide now"
const NEW_HIGH_FIT = 70; // the rarest, loudest tier
// A role not confirmed by its source for this long is probably closed (mirrors
// STALE_SOURCE_DAYS in catalog.js / scripts/config.py).
const STALE_SOURCE_DAYS = 14;
const LAST_VISIT_KEY = "today_last_visit";

// Captured ONCE per page load: the previous visit timestamp. We read it before
// advancing it, so the "new since last visit" list stays stable while the user
// is on the tab this session, and clears on the next page load.
let _prevVisit = null;
let _visitCaptured = false;

function _captureVisit() {
  if (_visitCaptured) return;
  try {
    _prevVisit = window.localStorage.getItem(LAST_VISIT_KEY);
    window.localStorage.setItem(LAST_VISIT_KEY, new Date().toISOString());
  } catch (_) {
    _prevVisit = null; // private mode / no storage → treat everything as seen
  }
  _visitCaptured = true;
}

function _daysUntil(dateStr) {
  if (!dateStr) return null;
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return null;
  return Math.floor((d.getTime() - Date.now()) / 86400000);
}

// A role belongs in Today only while it's still active and open. Returns false
// for decided-out roles (archived/passed/skipped), lapsed deadlines, and stale
// sources. status==='expiring' is intentionally protected — kept visible past
// its deadline/source lapse awaiting a decision — so it's exempt from the
// deadline + staleness checks, but still drops if it was decided out.
function _isLiveRole(g) {
  const status = getGroupStatus(g);
  if (status === "archived" || status === "passed" || status === "skipped") {
    return false;
  }
  if (status === "expiring") return true;
  if (isVacancyExpired(g)) return false;
  if (g.last_seen) {
    const seen = new Date(g.last_seen);
    if (!isNaN(seen.getTime())) {
      const ageDays = Math.floor((Date.now() - seen.getTime()) / 86400000);
      if (ageDays >= STALE_SOURCE_DAYS) return false;
    }
  }
  return true;
}

function _vacUrl(g) {
  const loc = (g.locations || []).find((l) => l && l.url);
  return (loc && loc.url) || g.org_url || "";
}

function _row(g, extra) {
  const url = _vacUrl(g);
  const head = escHtml(g.org || "") + " — " + escHtml(g.title || "");
  const link = url
    ? '<a href="' +
      escHtml(url) +
      '" target="_blank" rel="noopener">' +
      head +
      "</a>"
    : head;
  const score = g.llm_score != null ? " · 🎯 " + g.llm_score : "";
  const tail = extra
    ? ' · <span class="today-why">' + escHtml(extra) + "</span>"
    : "";
  return '<li class="today-item">' + link + score + tail + "</li>";
}

function _list(title, items, emptyMsg) {
  const body = items.length
    ? '<ul class="today-list">' + items.join("") + "</ul>"
    : '<p class="today-empty">' + escHtml(emptyMsg) + "</p>";
  return (
    '<section class="today-block"><h3>' +
    escHtml(title) +
    ' <span class="today-count">' +
    items.length +
    "</span></h3>" +
    body +
    "</section>"
  );
}

function _metricsPanel() {
  const m =
    (window.VACANCY_DATA && window.VACANCY_DATA.latency_metrics) || null;
  if (!m) return "";
  const stuckItems = (m.stuck || []).map(
    (s) =>
      '<li class="today-item">' +
      escHtml(s.org + " — " + s.title) +
      " · 🎯 " +
      s.llm_score +
      ' · <span class="today-why">' +
      s.days_stuck +
      " " +
      T("today_days_stuck", "d without movement") +
      " (" +
      escHtml(s.status) +
      ")</span></li>",
  );
  const stuckBody = stuckItems.length
    ? '<ul class="today-list">' + stuckItems.join("") + "</ul>"
    : '<p class="today-empty">' +
      escHtml(T("today_nothing_stuck", "nothing stuck")) +
      "</p>";
  const leak = m.leakage_count || 0;
  const leakLine =
    '<p class="today-leakage' +
    (leak > 0 ? " warn" : "") +
    '">' +
    escHtml(T("today_leakage", "leaked to archive/passed this week, roles")) +
    " " +
    String(m.sla_score) +
    "+: <strong>" +
    leak +
    "</strong></p>";
  return (
    '<section class="today-block today-sla"><h3>' +
    escHtml(T("today_sla", "Decision discipline")) +
    ' <span class="today-count">' +
    (m.stuck_count || 0) +
    "</span></h3>" +
    '<p class="today-sla-hint">' +
    escHtml(T("today_stuck_hint", "stuck (>=")) +
    String(m.sla_score) +
    ", >" +
    String(m.sla_days) +
    escHtml(T("today_stuck_hint_tail", " d):")) +
    "</p>" +
    stuckBody +
    leakLine +
    "</section>"
  );
}

export function renderToday() {
  const root = document.getElementById("todaySection");
  if (!root) return;
  _captureVisit();

  const visible = groups.filter((g) => isGroupCompanyApproved(g));

  const expiring = [];
  const ready = [];
  const newHighFit = [];

  for (const g of visible) {
    const status = getGroupStatus(g);
    const basket = STATUS_BASKET[status] || "unseen";

    if (status === "expiring") {
      expiring.push(_row(g, T("today_protected", "protected, decide")));
    } else if (basket === "liked" && _isLiveRole(g)) {
      const dleft = _daysUntil(g.deadline);
      if (dleft != null && dleft >= 0 && dleft <= SOON_DAYS) {
        expiring.push(
          _row(g, T("today_deadline_in", "deadline in") + " " + dleft + "d"),
        );
      }
    }

    if (status === "to_apply" && _isLiveRole(g)) {
      ready.push(_row(g));
    }

    if (
      status === "unseen" &&
      g.llm_score != null &&
      g.llm_score >= NEW_HIGH_FIT &&
      g.first_seen &&
      (!_prevVisit || g.first_seen > _prevVisit.slice(0, 10)) &&
      _isLiveRole(g)
    ) {
      newHighFit.push(_row(g, "🎯 " + g.llm_score));
    }
  }

  root.innerHTML =
    _list(
      T("today_expiring", "Expiring, needs a decision"),
      expiring,
      T("today_none_action", "nothing needs action"),
    ) +
    _list(
      T("today_ready", "Ready to send"),
      ready,
      T("today_none_ready", "nothing ready to send"),
    ) +
    _list(
      T("today_new", "New 70+ since last visit"),
      newHighFit,
      T("today_none_new", "nothing new"),
    ) +
    _metricsPanel();
}
