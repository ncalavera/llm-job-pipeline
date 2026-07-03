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
  qualityBand,
  jsAttr,
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

// Whole calendar days from today to dateStr (0 = today, negative = past).
// Exported + pure so the "deadline == today must count as 0, not -1" boundary
// (DHA-369 #3) is unit-tested with no DOM. Diffs two date-only instants (both
// anchored to UTC midnight) instead of dateStr's midnight vs Date.now()'s
// exact instant — the old mix returned -1 for "today" any time after 00:00
// UTC, which silently dropped a same-day deadline from the expiring list.
export function daysUntil(dateStr) {
  if (!dateStr) return null;
  const d = new Date(String(dateStr).slice(0, 10));
  if (isNaN(d.getTime())) return null;
  const todayD = new Date(new Date().toISOString().slice(0, 10));
  return Math.round((d.getTime() - todayD.getTime()) / 86400000);
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

// One inline-action button per entry. `glyph` → icon button (title carries the
// label); otherwise a text button. Mirrors catalog.js thumb-btn wiring: the
// member ids are JSON-encoded into the attribute; the canonical id is
// jsAttr-escaped for the same reason catalogRowHtml escapes it (R14) — ids are
// hash-shaped in practice, but the onclick string shouldn't trust that.
function _actionBtns(g, actions) {
  if (!actions || !actions.length) return "";
  const idAttr = jsAttr(g.id);
  const mids = JSON.stringify(g.member_ids || []).replace(/"/g, "&quot;");
  const btns = actions
    .map(function (a) {
      const onclick =
        "onclick=\"event.stopPropagation();todayAction('" +
        idAttr +
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

// Row anatomy (design-protocol.md #6): tinted score tile · title + org/why
// subline · quiet actions. Pure (no DOM read) so the escaping-regression suite
// can assert on it directly, same shape as catalogRowHtml/vacancyPageHtml.
// Clicking anywhere on the row opens the vacancy page (U6); the outbound
// posting link that used to live here moved there too (R6) — actions keep
// stopPropagation so they don't also trigger the navigation.
export function todayRowHtml(g, extra, actions) {
  const score = g.llm_score;
  const scoreCls =
    score == null ? "vac-score--none" : "q-" + qualityBand(score) + "-bg";
  const scoreTxt = score == null ? "—" : String(score);
  const subParts = [escHtml(g.org || "")];
  if (extra) subParts.push(escHtml(extra));
  return (
    '<div class="today-row" data-id="' +
    escHtml(g.id) +
    '" onclick="openTodayRow(\'' +
    jsAttr(g.id) +
    "')\">" +
    '<div class="today-row-score ' +
    scoreCls +
    '">' +
    escHtml(scoreTxt) +
    "</div>" +
    screenScoreBadge(g) +
    '<div class="today-row-body">' +
    '<div class="today-row-title">' +
    escHtml(g.title || "") +
    "</div>" +
    '<div class="today-row-sub">' +
    subParts.join(" · ") +
    "</div>" +
    "</div>" +
    _actionBtns(g, actions) +
    "</div>"
  );
}

// Open a row's vacancy detail page. Non-"browse" context: vacancyMoveToApply
// confirms in place instead of auto-advancing (F3) — Today has no queue to
// advance through. Exposed on window (app.js) for the row's onclick.
export function openTodayRow(id) {
  window.openVacancyRoute(id, { context: "today" });
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
  const row = document.querySelector('.today-row[data-id="' + canonId + '"]');
  if (row) {
    row.classList.add("dismissing");
    setTimeout(function () {
      updateStatus(canonId, memberIds, target);
    }, 200);
  } else {
    updateStatus(canonId, memberIds, target);
  }
}

// Section-label rhythm shared by the three main groups and every rail block
// (design-protocol.md #6): uppercase tracked title + quiet mono count. `count`
// is omitted (no digit shown) when null, for rail blocks that don't carry one.
function _sectionLabel(title, count) {
  const countHtml =
    count != null
      ? '<span class="today-section-count">' + count + "</span>"
      : "";
  return (
    '<div class="today-section-label"><span>' +
    escHtml(title) +
    "</span>" +
    countHtml +
    "</div>"
  );
}

// One of the three main groups: label + rows, or label + the (always-shown)
// empty note — a group never disappears, so its count stays visible even at
// zero (current behavior, preserved).
export function todayGroupHtml(title, items, emptyMsg) {
  const body = items.length
    ? '<div class="today-rows">' + items.join("") + "</div>"
    : '<p class="today-empty">' + escHtml(emptyMsg) + "</p>";
  return (
    '<div class="today-group">' +
    _sectionLabel(title, items.length) +
    body +
    "</div>"
  );
}

// Rail block wrapper: a section label (with an optional mono count, same
// helper the main groups use) followed by its body.
function _railBlock(label, count, bodyHtml) {
  return (
    '<div class="today-rail-block">' +
    _sectionLabel(label, count) +
    bodyHtml +
    "</div>"
  );
}

function _metricsPanel() {
  const m =
    (window.VACANCY_DATA && window.VACANCY_DATA.latency_metrics) || null;
  if (!m) return "";
  const stuckItems = (m.stuck || []).map(
    (s) =>
      '<div class="today-stuck-row"><span class="today-stuck-score q-' +
      qualityBand(s.llm_score) +
      '">' +
      escHtml(String(s.llm_score)) +
      '</span><div class="today-stuck-body"><div class="today-stuck-title">' +
      escHtml(s.org + " — " + s.title) +
      '</div><div class="today-stuck-meta">' +
      s.days_stuck +
      " " +
      escHtml(T("today_days_stuck", "d without movement")) +
      " (" +
      escHtml(s.status) +
      ")</div></div></div>",
  );
  const stuckBody = stuckItems.length
    ? '<div class="today-stuck-rows">' + stuckItems.join("") + "</div>"
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
  const hint =
    '<p class="today-sla-hint">' +
    escHtml(T("today_stuck_hint", "stuck (>=")) +
    String(m.sla_score) +
    ", >" +
    String(m.sla_days) +
    escHtml(T("today_stuck_hint_tail", " d):")) +
    "</p>";
  return _railBlock(
    T("today_sla", "Decision discipline"),
    m.stuck_count || 0,
    hint + stuckBody + leakLine,
  );
}

// Companies still awaiting the user's approve/reject decision. Honors any live
// approval the user just made this session (state.companyStatuses, keyed by
// company_id) over the baked snapshot's review_status. A one-line nudge with a
// cobalt text link that jumps straight to the Companies → Pending Review tab.
function _pendingCompaniesBlock() {
  const pending = (companiesList || []).filter(function (c) {
    const live = state.companyStatuses && state.companyStatuses[c.company_id];
    const status = live || c.review_status;
    return status === "pending";
  });
  if (!pending.length) return "";
  const label = T("today_pending_companies", "Companies awaiting approval");
  const cta = T("today_pending_companies_cta", "Review");
  return _railBlock(
    label,
    pending.length,
    '<button type="button" class="today-pending-cta" ' +
      "onclick=\"switchMode('companies');switchCompanySubTab('pending')\">" +
      escHtml(cta) +
      " →</button>",
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
      "<strong>" +
        escHtml(String(l.verdicts)) +
        "</strong> " +
        escHtml(
          T("today_learning_pending", "verdicts to review on the next run"),
        ),
    );
  }
  if (l.proposals) {
    parts.push(
      "<strong>" +
        escHtml(String(l.proposals)) +
        "</strong> " +
        escHtml(T("today_learning_proposals", "proposals ready")),
    );
  }
  if (!parts.length) return "";
  return _railBlock(
    T("today_learning", "Learning cycle"),
    null,
    '<p class="today-learning-line">' + parts.join(" · ") + "</p>",
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
    daysUntil,
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
    todayRowHtml(
      r.g,
      r.kind === "protected"
        ? T("today_protected", "protected, decide")
        : T("today_deadline_in", "deadline in") + " " + r.daysLeft + "d",
      expiringActions,
    ),
  );
  const ready = readyRows.map((g) => todayRowHtml(g, null, readyActions));
  // The score is already visible on the tile, so unlike the other two groups
  // this one has no extra "why" text to add — surfacing here at all IS the why.
  const newHighFit = newRows.map((g) => todayRowHtml(g, null, newActions));

  const main =
    todayGroupHtml(
      T("today_expiring", "Expiring, needs a decision"),
      expiring,
      T("today_none_action", "nothing needs action"),
    ) +
    todayGroupHtml(
      T("today_ready", "Ready to send"),
      ready,
      T("today_none_ready", "nothing ready to send"),
    ) +
    todayGroupHtml(
      T("today_new", "New 70+ since last visit"),
      newHighFit,
      T("today_none_new", "nothing new"),
    );
  const rail = _metricsPanel() + _pendingCompaniesBlock() + _learningBlock();

  root.innerHTML =
    '<div class="today-header">' +
    '<span class="today-title">' +
    escHtml(T("tab_today", "Today")) +
    "</span>" +
    '<span class="today-subtitle">' +
    escHtml(T("today_subtitle", "The few things that need a decision today.")) +
    "</span>" +
    "</div>" +
    '<div class="today-sheet">' +
    '<div class="today-main">' +
    main +
    "</div>" +
    '<div class="today-rail">' +
    rail +
    "</div>" +
    "</div>";
}
