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
  state,
  groups,
  companiesList,
  getGroupStatus,
  STATUS_BASKET,
  isGroupCompanyApproved,
  updateStatus,
} from "./state.js";
import {
  escHtml,
  isVacancyExpired,
  STALE_SOURCE_DAYS,
  sourceAgeDays,
  screenScoreBadge,
  safeUrl,
} from "./helpers.js";
import { T } from "./i18n.js";
import { selectTodayRoles } from "./derive.js";

const SOON_DAYS = 7; // a deadline this close is "decide now"
const NEW_HIGH_FIT = 70; // the rarest, loudest tier
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
  const ageDays = sourceAgeDays(g.last_seen);
  if (ageDays != null && ageDays >= STALE_SOURCE_DAYS) return false;
  return true;
}

function _vacUrl(g) {
  const loc = (g.locations || []).find((l) => l && l.url);
  return safeUrl((loc && loc.url) || g.org_url || "");
}

// One inline-action button per entry. `glyph` → icon button (title carries the
// label); otherwise a text button. Mirrors catalog.js thumb-btn wiring: the
// member ids are JSON-encoded into the attribute and the canonical id is passed
// raw (it's a hash). All copy comes from T() with English fallbacks.
function _actionBtns(g, actions) {
  if (!actions || !actions.length) return "";
  const mids = JSON.stringify(g.member_ids || []).replace(/"/g, "&quot;");
  const btns = actions
    .map(function (a) {
      const onclick =
        "onclick=\"event.stopPropagation();todayAction('" +
        g.id +
        "'," +
        mids +
        ",'" +
        a.action +
        "')\"";
      const cls = "today-act " + (a.cls || "");
      if (a.glyph) {
        return (
          '<button class="' +
          cls +
          '" ' +
          onclick +
          ' title="' +
          escHtml(a.label) +
          '">' +
          a.glyph +
          "</button>"
        );
      }
      return (
        '<button class="' +
        cls +
        '" ' +
        onclick +
        ">" +
        escHtml(a.label) +
        "</button>"
      );
    })
    .join("");
  return '<span class="today-actions">' + btns + "</span>";
}

function _row(g, extra, actions) {
  const url = _vacUrl(g);
  const head = escHtml(g.org || "") + " — " + escHtml(g.title || "");
  const link = url
    ? '<a href="' +
      escHtml(url) +
      '" target="_blank" rel="noopener">' +
      head +
      "</a>"
    : head;
  const score =
    g.llm_score != null ? " · 🎯 " + g.llm_score + screenScoreBadge(g) : "";
  const tail = extra
    ? ' · <span class="today-why">' + escHtml(extra) + "</span>"
    : "";
  return (
    '<li class="today-item" data-id="' +
    escHtml(g.id) +
    '"><span class="today-lead">' +
    link +
    score +
    tail +
    "</span>" +
    _actionBtns(g, actions) +
    "</li>"
  );
}

// Inline triage: flip a role's status through the same optimistic-update chain
// the catalog uses. statusChanged (app.js) re-renders the Today tab, so the row
// disappears once its new status no longer matches any list.
export function todayAction(canonId, memberIds, action) {
  const target =
    action === "like"
      ? "liked"
      : action === "pass"
        ? "passed"
        : action === "apply"
          ? "to_apply"
          : action === "applied"
            ? "applied"
            : "unseen";
  const row = document.querySelector('.today-item[data-id="' + canonId + '"]');
  if (row) {
    row.classList.add("dismissing");
    setTimeout(function () {
      updateStatus(canonId, memberIds, target);
    }, 200);
  } else {
    updateStatus(canonId, memberIds, target);
  }
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

// Companies still awaiting the user's approve/reject decision. Honors any live
// approval the user just made this session (state.companyStatuses, keyed by
// company_id) over the baked snapshot's review_status. A one-line nudge with a
// button that jumps straight to the Companies → Pending Review sub-tab.
function _pendingCompaniesBlock() {
  const pending = (companiesList || []).filter(function (c) {
    const live = state.companyStatuses && state.companyStatuses[c.company_id];
    const status = live || c.review_status;
    return status === "pending";
  });
  if (!pending.length) return "";
  const label = T("today_pending_companies", "Companies awaiting approval");
  const cta = T("today_pending_companies_cta", "Review");
  return (
    '<section class="today-block today-pending"><h3>' +
    escHtml(label) +
    ' <span class="today-count">' +
    pending.length +
    "</span></h3>" +
    '<p class="today-pending-cta"><button type="button" class="today-act act-apply" ' +
    "onclick=\"switchMode('companies');switchCompanySubTab('pending')\">" +
    escHtml(cta) +
    "</button></p></section>"
  );
}

// The learning cycle's "there are verdicts to fold in next run" hint, from the
// server-computed VACANCY_DATA.learning (deterministic, no LLM). Only shown when
// something is actually pending; the counts are the only thing baked (never the
// proposal text).
function _learningBlock() {
  const l = (window.VACANCY_DATA && window.VACANCY_DATA.learning) || null;
  if (!l || !l.pending) return "";
  const parts = [];
  if (l.verdicts) {
    parts.push(
      l.verdicts +
        " " +
        T("today_learning_pending", "verdicts to review on the next run"),
    );
  }
  if (l.proposals) {
    parts.push(
      l.proposals + " " + T("today_learning_proposals", "proposals ready"),
    );
  }
  if (!parts.length) return "";
  return (
    '<section class="today-block today-learning"><h3>' +
    escHtml(T("today_learning", "Learning cycle")) +
    "</h3>" +
    '<p class="today-learning-line">' +
    escHtml(parts.join(" · ")) +
    "</p></section>"
  );
}

export function renderToday() {
  const root = document.getElementById("todaySection");
  if (!root) return;
  _captureVisit();

  // Membership + urgency ordering are a pure derivation of (approved roles +
  // live statuses + today's date) — see derive.js. Today is deliberately NOT
  // score-floored: a role the user has liked/queued must surface regardless of
  // score. The lists react to likes/passes and today's expiry with no run.
  const {
    expiring: expiringRows,
    ready: readyRows,
    newHighFit: newRows,
  } = selectTodayRoles(groups, {
    isApproved: isGroupCompanyApproved,
    getStatus: getGroupStatus,
    basketMap: STATUS_BASKET,
    isLiveRole: _isLiveRole,
    daysUntil: _daysUntil,
    soonDays: SOON_DAYS,
    newHighFit: NEW_HIGH_FIT,
    prevVisit: _prevVisit,
  });

  const passAction = {
    action: "pass",
    label: T("today_act_pass", "Pass"),
    cls: "act-pass",
  };
  const expiringActions = [
    { action: "apply", label: T("today_act_apply", "Apply"), cls: "act-apply" },
    passAction,
  ];
  const readyActions = [
    {
      action: "applied",
      label: T("today_act_applied", "Mark applied"),
      cls: "act-apply",
    },
    passAction,
  ];
  const newActions = [
    {
      action: "like",
      label: T("today_act_like", "Like"),
      glyph: "👍",
      cls: "act-like",
    },
    { ...passAction, glyph: "👎" },
  ];

  const expiring = expiringRows.map((r) =>
    _row(
      r.g,
      r.kind === "protected"
        ? T("today_protected", "protected, decide")
        : T("today_deadline_in", "deadline in") + " " + r.daysLeft + "d",
      expiringActions,
    ),
  );
  const ready = readyRows.map((g) => _row(g, null, readyActions));
  const newHighFit = newRows.map((g) =>
    _row(g, "🎯 " + g.llm_score, newActions),
  );

  root.innerHTML =
    _pendingCompaniesBlock() +
    _learningBlock() +
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
