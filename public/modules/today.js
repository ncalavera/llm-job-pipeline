// =============================================================================
// today.js — the "Today" cockpit: the few things that need a decision now.
// =============================================================================
//
// DHA-410 rework: six ordered, hide-when-empty action blocks answer "what do I
// do right now" without scrolling. Order is fixed; a block with zero rows does
// not render at all (no empty skeletons). Populations derive in derive.js from
// (approved roles + live statuses + today's date) — the badge on each block
// equals the length of the list it labels by construction (guardrail #9).
//
//   1. Committed      — status to_apply → mark applied (lapsed rows flagged
//                       overdue, never dropped — the user committed to them)
//   2. Awaiting reply — status applied (read-only; awaiting a reply)
//   3. Liked, undecided — status liked → queue / pass
//   4. Closing soon   — protected status='expiring' roles lead (the pipeline
//                       kept them alive for a decision; flagged source-gone /
//                       deadline-passed), then unseen score ≥60 deadline ≤7d
//                       → like / pass; + one link-out line counting the
//                       weak/unscored deadline rows the score gate hides
//      Don't let good ones rot — unseen, score ≥60, undecided >7d (hidden when
//                                empty; costs nothing given the derive shape)
//   5. Working        — to_research / to_network (read-only in-flight work)
//   6. Approve intake — pending candidate companies → approve / pass
//
// All display copy resolves through T(); the English fallbacks here keep the
// public shell Cyrillic-free.

import {
  state,
  groups,
  companiesList,
  getGroupStatus,
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
// Clicking anywhere on the row opens the vacancy page; actions keep
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
    '" role="button" tabindex="0" onclick="openTodayRow(\'' +
    jsAttr(g.id) +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openTodayRow('" +
    jsAttr(g.id) +
    "')}\">" +
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

// A pending-company intake row (block 6). Shares the .today-row anatomy so the
// six blocks read as one template (guardrail #10). The tile carries the tier
// letter (neutral, not a quality score); approve/pass call the existing global
// reviewCompany channel — no new endpoint, optimistic + revert-on-failure. The
// row opens the company profile on click; the buttons stopPropagation.
export function todayCompanyRowHtml(c, writable) {
  const tier = (c.calculated_tier || "").toUpperCase();
  const tileTxt = tier || "—";
  const slug = jsAttr(c.slug || "");
  const cid = jsAttr(c.company_id || "");
  let actions = "";
  if (writable) {
    const approve =
      '<button class="today-act act-apply" onclick="event.stopPropagation();' +
      "reviewCompany('" +
      cid +
      "','approve')\">" +
      escHtml(T("today_act_approve", "Approve")) +
      "</button>";
    const pass =
      '<button class="today-act act-pass" onclick="event.stopPropagation();' +
      "reviewCompany('" +
      cid +
      "','reject')\">" +
      escHtml(T("today_act_pass", "Pass")) +
      "</button>";
    actions = '<span class="today-actions">' + approve + pass + "</span>";
  }
  return (
    '<div class="today-row today-row--company" data-cid="' +
    escHtml(c.company_id || "") +
    '" role="button" tabindex="0" onclick="openCompanyProfile(\'' +
    slug +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openCompanyProfile('" +
    slug +
    "')}\">" +
    '<div class="today-row-score vac-score--none">' +
    escHtml(tileTxt) +
    "</div>" +
    '<div class="today-row-body">' +
    '<div class="today-row-title">' +
    escHtml(c.name || "") +
    "</div>" +
    '<div class="today-row-sub">' +
    escHtml(T("today_intake_sub", "candidate awaiting review")) +
    "</div>" +
    "</div>" +
    actions +
    "</div>"
  );
}

// Open a row's vacancy detail page. Non-"browse" context: vacancyMoveToApply
// confirms in place instead of auto-advancing — Today has no queue to advance
// through. Exposed on window (app.js) for the row's onclick.
export function openTodayRow(id) {
  window.openVacancyRoute(id, { context: "today" });
}

// Inline triage: flip a role's status through the same optimistic-update chain
// the catalog uses. statusChanged (app.js) re-renders the Today tab, so the row
// disappears once its new status no longer matches any block.
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

// Section-label rhythm shared by every block (design-protocol.md #6):
// uppercase tracked title + quiet mono count.
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

// One block: label + rows. Hidden entirely when it has no rows (DHA-410: no
// empty skeletons) — returns "" so the caller can concat blocks blindly. The
// count shown always equals items.length (badge == list).
export function todayGroupHtml(title, items) {
  if (!items.length) return "";
  return (
    '<div class="today-group">' +
    _sectionLabel(title, items.length) +
    '<div class="today-rows">' +
    items.join("") +
    "</div>" +
    "</div>"
  );
}

// The honest link-out line below Closing soon: names the weak/unscored roles
// with a near deadline the score gate hides. There is no deadline-filtered
// Browse view (DHA-428), so this stays a plain count rather than a link — a
// bare Browse jump couldn't isolate these rows among everything else it shows.
export function closingHiddenLine(n) {
  if (!n) return "";
  const parts = [
    T("today_closing_hidden_pre", ""),
    String(n),
    T("today_closing_hidden_post", "more deadlines at weak or unscored roles"),
  ].filter(Boolean);
  return '<p class="today-linkout">' + escHtml(parts.join(" ")) + "</p>";
}

// Companies still awaiting the user's approve/reject decision. Honors any live
// approval made this session (state.companyStatuses, keyed by company_id) over
// the baked snapshot's review_status, so an approved company leaves the block
// immediately. Rendered as inline intake rows (block 6).
function _pendingCompanies() {
  return (companiesList || []).filter(function (c) {
    const live = state.companyStatuses && state.companyStatuses[c.company_id];
    const status = live || c.review_status;
    return status === "pending";
  });
}

// The Today deadline subline, or null when a role has no live future deadline.
function _deadlineSub(g) {
  const d = daysUntil(g.deadline);
  return d != null && d >= 0
    ? T("today_deadline_in", "deadline in") + " " + d + "d"
    : null;
}

export function renderToday() {
  const root = document.getElementById("todaySection");
  if (!root) return;

  // Membership + ordering are a pure derivation of (approved roles + live
  // statuses + today's date) — see derive.js selectTodayRoles. Today is
  // deliberately NOT score-floored: a role the user liked/queued surfaces
  // regardless of score. The blocks react to likes/passes and today's expiry
  // with no run.
  const {
    committed,
    awaiting,
    liked,
    closingSoon,
    closingSoonHidden,
    dontRot,
    working,
  } = selectTodayRoles(groups, {
    isApproved: isGroupCompanyApproved,
    getStatus: getGroupStatus,
    isLiveRole: _isLiveRole,
    daysUntil,
    soonDays: SOON_DAYS,
  });

  // Write affordances render only when the live overlay has loaded; static /
  // fallback mode (file://) shows read-only rows (KTD7).
  const writable = state.statusesLoaded;
  const companyWritable = state.companyStatusesLoaded;

  const passAction = {
    action: "pass",
    label: T("today_act_pass", "Pass"),
    cls: "act-pass",
  };
  const committedActions = writable
    ? [
        {
          action: "applied",
          label: T("today_act_applied", "Mark applied"),
          cls: "act-apply",
        },
        passAction,
      ]
    : [];
  const likedActions = writable
    ? [
        {
          action: "apply",
          label: T("today_act_queue", "Queue"),
          cls: "act-apply",
        },
        passAction,
      ]
    : [];
  const decideActions = writable
    ? [
        { action: "like", label: T("today_act_like", "Like"), cls: "act-like" },
        passAction,
      ]
    : [];

  // Block 1 — Committed (to_apply). Lapsed entries (deadline passed or source
  // gone stale) kept, flagged overdue — the user committed to them.
  const committedRows = committed.map((r) =>
    todayRowHtml(
      r.g,
      r.overdue ? T("today_overdue", "overdue") : _deadlineSub(r.g),
      committedActions,
    ),
  );
  // Block 2 — Awaiting reply (applied). Read-only: already sent, nothing to do.
  const awaitingRows = awaiting.map((g) => todayRowHtml(g, null, []));
  // Block 3 — Liked, undecided.
  const likedRows = liked.map((g) =>
    todayRowHtml(g, _deadlineSub(g), likedActions),
  );
  // Block 4 — Closing soon. Protected 'expiring' rows lead with a why-flag
  // (deadline passed vs source gone) and Queue/Pass — liking a role that is
  // already past deadline would lapse it straight to Passed, so queueing to
  // Committed is the affirmative action here. Plain unseen rows keep the
  // deadline chip and Like/Pass.
  const closingRows = closingSoon.map((r) => {
    if (r.expiring) {
      const d = daysUntil(r.g.deadline);
      const why =
        d != null && d < 0
          ? T("today_deadline_passed", "deadline passed")
          : T("today_source_gone", "source gone");
      return todayRowHtml(r.g, why, likedActions);
    }
    return todayRowHtml(r.g, _deadlineSub(r.g), decideActions);
  });
  // Don't let good ones rot — high-fit, undecided, no near deadline.
  const rotRows = dontRot.map((g) => todayRowHtml(g, null, decideActions));
  // Block 5 — Working (to_research / to_network). Read-only in-flight work.
  const workingRows = working.map((g) =>
    todayRowHtml(
      g,
      getGroupStatus(g) === "to_research"
        ? T("today_research", "researching")
        : T("today_networking", "networking"),
      [],
    ),
  );
  // Block 6 — Approve intake (pending candidate companies).
  const intakeRows = _pendingCompanies().map((c) =>
    todayCompanyRowHtml(c, companyWritable),
  );

  // The hidden-count line renders only under a populated Closing-soon block —
  // an orphan count with no header above it reads as noise. The one exception:
  // when the whole board is empty the count folds in under the all-clear line,
  // so the completion state stays honest about what the gate is hiding.
  const closingBlock = closingRows.length
    ? todayGroupHtml(T("today_closing", "Closing soon"), closingRows) +
      closingHiddenLine(closingSoonHidden)
    : "";

  const inbox =
    todayGroupHtml(T("today_committed", "Committed — send it"), committedRows) +
    todayGroupHtml(T("today_awaiting", "Awaiting reply"), awaitingRows) +
    todayGroupHtml(T("today_liked", "Liked — decide"), likedRows) +
    closingBlock +
    todayGroupHtml(T("today_dont_rot", "Don't let good ones rot"), rotRows) +
    todayGroupHtml(T("today_working", "In progress"), workingRows) +
    todayGroupHtml(T("today_intake", "Approve intake"), intakeRows);

  // Peak-End: an empty Today is a win, not a void — say so instead of rendering
  // seven absent blocks and silence.
  const main = inbox.trim()
    ? inbox
    : '<p class="today-allclear">' +
      escHtml(
        T("today_all_clear", "All clear — nothing needs a decision now."),
      ) +
      "</p>" +
      closingHiddenLine(closingSoonHidden);

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
    "</div>";
}
