// =============================================================================
// screen.js — the Screen view: the bulk screening inbox (DHA-603).
//
// The nightly run prepares facts per role (`screening`, `screening_state` on
// each group). This view derives three lists and a fixed set of groups from
// those facts in the browser (derive.js), shows one row per role with the
// verbatim quote behind each requirement, and lets the user Keep or Put aside
// many rows in one action, with Undo.
//
// Bulk writes bypass `updateStatus`: that emits 'statusChanged', whose api.js
// subscriber fires an un-awaited /api/save per id. Here every row's save is
// awaited so a failed row can revert, and only saved rows enter the Undo
// record (KTD4). The write path is injectable (`io`) so it unit-tests without
// the DOM or the network.
// =============================================================================

import {
  state,
  config,
  API_BASE,
  groups,
  groupsById,
  getCompanies,
  getGroupStatus,
  setStatusLocal,
  scheduleRender,
} from "./state.js";
import { loadFromServer } from "./api.js";
import {
  escHtml,
  safeUrl,
  jsAttr,
  formatDeadlineHtml,
  relativeTime,
  qualityBand,
  tierClass,
  showToastText,
  resolveVacancyCompany,
} from "./helpers.js";
import { sourceLabel } from "./vacancy.js";
import { T } from "./i18n.js";
import { reviewBatches, batchConcern } from "./screen-batches.js";
import {
  screenLists,
  screenGroups,
  screenRequirements,
  screenMatches,
  screenMatchRequirements,
  screenDateFacts,
} from "./derive.js";

// ---------------------------------------------------------------------------
// View state (module-local). Selection survives render ticks; any list or
// group change clears it (R10).
// ---------------------------------------------------------------------------

export const view = {
  batch: null,
  reason: "",
  feedback: null,
  feedbackState: "",
  notes: null,
  notesError: false,
  list: "toScreen", // "toScreen" | "kept" | "putAside"
  group: "all", // one of SCREEN_GROUP_KEYS; filters the To screen list only
  filters: {},
  filterOpen: new Set(["screen_enjoy"]),
  page: 0,
  selected: new Set(),
  open: new Set(), // rows whose evidence disclosure is open
  busy: false,
  notice: "",
};

// Bulk operations, newest last: { status, rows: [{ id, member_ids, previous }] }.
// `previous` maps every member id to the status it had before the action.
let history = [];
let pendingDecision = null;
try {
  const saved = JSON.parse(localStorage.getItem("screen-decisions") || "null");
  if (Array.isArray(saved?.history)) history = saved.history;
  pendingDecision = saved?.pending || null;
} catch { /* Browser storage may be unavailable. */ }
function persistDecisions() {
  try {
    localStorage.setItem("screen-decisions", JSON.stringify({ history, pending: pendingDecision }));
  } catch { /* In-memory retry still works. */ }
}

export function setList(name) {
  if (view.list === name) return;
  view.list = name;
  view.page = 0;
  view.reason = "";
  view.selected.clear();
}

export function setGroup(key) {
  if (view.group === key) return;
  view.group = key;
  view.selected.clear();
}

export function toggleSelected(id) {
  if (view.selected.has(id)) view.selected.delete(id);
  else view.selected.add(id);
}

/** Select every visible id, or clear when all of them are already selected. */
export function toggleSelectAll(visibleIds) {
  const all =
    visibleIds.length && visibleIds.every((id) => view.selected.has(id));
  view.selected = all ? new Set() : new Set(visibleIds);
}

// ---------------------------------------------------------------------------
// Derivation: lists, groups, the visible id list
// ---------------------------------------------------------------------------

export const PAGE_SIZE = 20;

export function setFilter(key, value) {
  view.filters[key] = value;
  view.group = "all";
  view.page = 0;
  view.selected.clear();
}

export function setPage(page) {
  view.page = Math.max(0, page);
  view.selected.clear();
}

export function screenModel(
  roles,
  getStatus,
  today = new Date().toISOString().slice(0, 10),
) {
  const lists = screenLists(roles, getStatus, config.screening_prompt_fingerprint);
  const cohort = roles.filter((g) => lists[view.list].has(g.id));
  const groupSets = screenGroups(cohort);
  const matching = cohort.filter(
    (g) =>
      (view.group === "all" || groupSets[view.group]?.has(g.id)) &&
      screenMatches(g, view.filters, today),
  );
  const matchingIds = matching.map((g) => g.id);
  const pages = Math.max(1, Math.ceil(matching.length / PAGE_SIZE));
  view.page = Math.min(view.page, pages - 1);
  const visibleIds = matchingIds.slice(
    view.page * PAGE_SIZE,
    (view.page + 1) * PAGE_SIZE,
  );
  for (const id of view.selected)
    if (!visibleIds.includes(id)) view.selected.delete(id);
  return {
    lists,
    groupSets,
    matchingIds,
    visibleIds,
    pages,
    unclassified: cohort.filter((g) => !g.screening?.work_profile).length,
  };
}

function canDecide(g) {
  return !!g && ["unseen", "liked", "passed", "skipped", "expiring"].includes(getGroupStatus(g));
}
let reviewedIds = new Set();
try { reviewedIds = new Set(JSON.parse(localStorage.getItem("inbox-review-ids") || "[]")); } catch { /* First visit or blocked storage. */ }

function inboxFiltersHtml() {
  const select = (key, label, values) => '<label>' + escHtml(label) + '<select data-filter="' + key + '"><option value="">' +
    escHtml(T("screen_all_values", "Any")) + '</option>' + values.map(([value, text]) => '<option value="' + escHtml(value) + '"' +
    (view.filters[key] === value ? ' selected' : '') + '>' + escHtml(text) + '</option>').join('') + '</select></label>';
  const values = key => [...new Set(groups.map(g => key === "added" ? String(g.first_seen || "").slice(0,10) : g.source_board).filter(Boolean))].sort().reverse().map(v => [v,v]);
  return '<div class="inbox-filterbar">' +
    '<label>' + escHtml(T("inbox_search", "Title or company")) + '<input data-filter="search" value="' + escHtml(view.filters.search || "") + '"></label>' +
    '<label>' + escHtml(T("inbox_place", "Job location")) + '<input data-filter="place" value="' + escHtml(view.filters.place || "") + '"></label>' +
    select("added", T("vac_first_seen", "First seen"), values("added")) +
    '<details><summary>' + escHtml(T("inbox_more_filters", "More filters")) + '</summary><div class="inbox-filterbar">' +
    select("source",T("vac_source","Source"),values("source")) +
    select("workMode",T("inbox_work_mode","Work mode"),["remote","hybrid","onsite","unknown"].map(v => [v,T("inbox_"+v,v)])) +
    select("technical",T("screen_technical","Technical requirements"),["coordination","practical","specialist","unknown"].map(v => [v,T(v === "specialist" ? "screen_technical_specialist" : "screen_"+v,v)])) +
    select("kind",T("inbox_requirement","Requirement"),["authorisation","language","education","experience","skill"].map(v => [v,T("inbox_"+v,v)])) +
    '</div></details><button class="scr-btn" id="inboxClear">' + escHtml(T("screen_clear_filters","Clear filters")) + '</button></div>' +
    '<div class="inbox-review-checkpoint"><button class="scr-btn" id="inboxNew" aria-pressed="' + !!view.newOnly + '">' +
    escHtml(T("inbox_since_review","New since last review on this device")) + '</button><button class="scr-btn" id="inboxFinish">' +
    escHtml(T("inbox_finish","Finish review")) + '</button></div>';
}

export const REVIEW_SIZE = 6;

export function reviewModel(roles, getStatus) {
  const lists = screenLists(roles, getStatus, config.screening_prompt_fingerprint);
  const cohort = roles.filter(g => lists[view.list].has(g.id) &&
    (!view.newOnly || !reviewedIds.has(g.id)) && screenMatches(g, view.filters));
  const batches = reviewBatches(cohort, () => "unseen");
  if (view.batch && !batches.some((b) => b.key === view.batch)) view.batch = null;
  const batch = batches.find((b) => b.key === view.batch);
  const matching = cohort.filter((g) =>
    !batch || batch.roles.some(r => r.id === g.id)
  ).sort((a, b) => String(b.first_seen || "").localeCompare(String(a.first_seen || "")) || String(a.title || "").localeCompare(String(b.title || "")));
  const pages = Math.max(1, Math.ceil(matching.length / REVIEW_SIZE));
  view.page = Math.min(view.page, pages - 1);
  const rows = matching.slice(
    view.page * REVIEW_SIZE,
    (view.page + 1) * REVIEW_SIZE,
  );
  const visibleIds = rows.map((g) => g.id);
  for (const id of view.selected)
    if (!visibleIds.includes(id)) view.selected.delete(id);
  return {
    lists,
    batches,
    batch,
    rows,
    visibleIds,
    pages,
    total: matching.length,
  };
}

export function feedbackFor(op, reason, groupLabel, id) {
  const text = String(reason || "").trim();
  if (!op?.rows?.length || !text) return null;
  return {
    id,
    vacancy_ids: [...new Set(op.rows.flatMap((r) => r.member_ids))],
    decision: op.status,
    reason: text,
    group_label: groupLabel,
  };
}

let recoveredFeedback = false;
function persistFeedback() {
  try {
    if (view.feedback)
      localStorage.setItem(
        "screen-pending-feedback",
        JSON.stringify(view.feedback),
      );
    else localStorage.removeItem("screen-pending-feedback");
  } catch {
    /* The visible retry remains available if browser storage is blocked. */
  }
}
async function saveReviewFeedback() {
  if (!view.feedback) return;
  persistFeedback();
  view.feedbackState = "saving";
  try {
    const response = await fetch(API_BASE + "/api/screening-feedback", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(view.feedback),
    });
    if (!response.ok) throw new Error("Feedback save failed");
    view.feedback = null;
    view.feedbackState = "saved";
    persistFeedback();
  } catch {
    view.feedbackState = "failed";
  }
}

/** "{n} of {m}" → values. */
export function fill(template, vars) {
  return String(template).replace(/\{(\w+)\}/g, (m, k) =>
    k in vars ? String(vars[k]) : m,
  );
}

// ---------------------------------------------------------------------------
// Write path
// ---------------------------------------------------------------------------

export const liveIo = {
  members: (id) => {
    const g = groupsById.get(id);
    return [...new Set([id, ...(g?.member_ids || [])])].filter((mid) => state.dbData[mid]);
  },
  current: (mid) => state.dbData[mid]?.status,
  revision: (mid) => state.dbData[mid]?.revision,
  async write(memberIds, targetOf, expected, context) {
    if (!API_BASE) return null;
    if (!pendingDecision && memberIds.some((id) => !state.dbData[id]?.revision)) await loadFromServer();
    const changes = memberIds.map((id) => ({
      id, status: targetOf(id), expected_status: state.dbData[id]?.status,
      expected_revision: expected ? expected[id] : state.dbData[id]?.revision,
    }));
    if (!pendingDecision && changes.some((c) => !c.expected_revision)) return null;
    pendingDecision ||= { operation_id: crypto.randomUUID(), changes, ...context,
      reason: view.reason, batch: view.batch };
    persistDecisions();
    // Replaying the same receipt is safe even if the first response was lost.
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const response = await fetch(API_BASE + "/api/screening-decision", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ operation_id: pendingDecision.operation_id, changes: pendingDecision.changes }),
        });
        const result = await response.json();
        if (!response.ok) {
          if (response.status < 500) {
            pendingDecision = null;
            persistDecisions();
            await loadFromServer();
            return null;
          }
          continue;
        }
        const previous = {};
        for (const row of result.rows) {
          previous[row.id] = row.previous;
          setStatusLocal([row.id], row.status);
          state.dbData[row.id].revision = row.revision;
          // When the status changed, not when we learned it. getGroupStatus
          // reads this to decide whether an "unsure" row is still deferred, so
          // without it a just-deferred role reappears in the Inbox at once.
          if (row.status_updated_at)
            state.dbData[row.id].status_changed_at = row.status_updated_at;
        }
        return previous;
      } catch { /* Keep the receipt for an explicit retry after reconnecting. */ }
    }
    return null;
  },
};

/**
 * Keep (liked) or Put aside (passed) the given canonical ids. Pushes one
 * operation with the saved rows only. @returns {{saved, total, op}}
 */
export async function bulkSet(ids, status, io, onlyUndecided = false) {
  io = io || liveIo;
  const rows = [];
  const op = { status, rows };
  for (const id of ids) {
    const members = io.members(id);
    if (!members.length || (onlyUndecided &&
        members.some(
          (mid) => !["unseen", "expiring", "unsure"].includes(io.current(mid)),
        ))) continue;
    const previous = await io.write(members, () => status, undefined, { id, status });
    if (previous) {
      const row = { id, member_ids: members, previous };
      if (io.revision) row.revisions = Object.fromEntries(members.map((mid) => [mid, io.revision(mid)]));
      rows.push(row);
      if (rows.length === 1) history.push(op);
      if (io === liveIo) pendingDecision = null;
      persistDecisions();
    }
    if (io === liveIo && pendingDecision) break;
  }
  return { saved: rows.length, total: ids.length, op: rows.length ? op : null };
}

export function decisionState() {
  return {pending: !!pendingDecision, canUndo: history.length > 0};
}
export function retryDecision() {
  if (!pendingDecision) return Promise.resolve(null);
  return pendingDecision.undo ? undoLast() : bulkSet([pendingDecision.id], pendingDecision.status);
}

/** Restore the last operation, retaining failed rows for another Undo attempt. */
export async function undoLast(io) {
  io = io || liveIo;
  const op = history.at(-1);
  if (!op) return null;
  let restored = 0;
  const total = op.rows.length;
  for (const row of [...op.rows]) {
    // Only members still carrying this operation's status are restored; a
    // decision made after the bulk action (say, "applied") is never overwritten.
    const still = io === liveIo && pendingDecision?.undo && pendingDecision.id === row.id
      ? pendingDecision.changes.map((c) => c.id)
      : row.member_ids.filter((mid) => io.current(mid) === op.status);
    const ok = still.length && await io.write(still, (mid) => row.previous[mid], row.revisions, { id: row.id, undo: true });
    if (ok || !still.length) {
      if (ok) restored++;
      op.rows.splice(op.rows.indexOf(row), 1);
      if (io === liveIo) pendingDecision = null;
      persistDecisions();
    }
    if (io === liveIo && pendingDecision) break;
  }
  if (!op.rows.length) history.pop();
  persistDecisions();
  return { restored, total };
}

// ---------------------------------------------------------------------------
// Row assembly (pure)
//
// The dense row this view renders. It used to live in catalog.js; the review
// screen there now has its own row, and this is the only remaining caller.
// ---------------------------------------------------------------------------

// First location's text plus a "+N" hint when there are more; the full list
// is one click away on the vacancy detail page's facts rail.
function primaryLocationInfo(g) {
  const locs = (g.locations || []).filter((l) => l && l.location);
  if (!locs.length) return null;
  const extra = locs.length - 1;
  return {
    text: locs[0].location + (extra > 0 ? " +" + extra : ""),
    title: locs.map((l) => l.location).join(", "),
  };
}

function catalogRowHtml(g, basket, opts) {
  const o = opts || {};
  const t = o.t || ((k, fb) => fb);
  const locale = o.locale || "en-US";

  const score = g.llm_score;
  const scoreCls =
    score == null ? "vac-score--none" : "q-" + qualityBand(score) + "-bg";
  const scoreTxt = score == null ? "—" : String(score);

  const idAttr = jsAttr(g.id);

  const deadlineHtml = g.deadline
    ? formatDeadlineHtml(g.deadline, "card-deadline", { t, locale })
    : "";

  const tierHtml = g.calculated_tier
    ? '<span class="catalog-row-tier ' +
      tierClass(g.calculated_tier) +
      '">' +
      escHtml(g.calculated_tier) +
      "</span>"
    : "";

  const loc = primaryLocationInfo(g);
  const locHtml = loc
    ? '<span title="' +
      escHtml(loc.title) +
      '">' +
      escHtml(loc.text) +
      "</span>"
    : "—";

  const compText = g.compensation ? escHtml(g.compensation) : "—";
  const seenText = g.first_seen ? escHtml(relativeTime(g.first_seen, t)) : "—";

  const subText = g.llm_summary || g.screening?.posting_facts?.duties || g.snippet || "";
  const subHtml = subText
    ? '<div class="catalog-row-sub">' + escHtml(subText) + "</div>"
    : "";

  const mids = jsAttr(JSON.stringify(g.member_ids));
  const likeLabel = escHtml(t("vac_like", "Keep"));
  const passLabel = escHtml(t("vac_pass", "Pass"));
  const likeBtn =
    '<button class="catalog-row-btn like" onclick="event.stopPropagation();catalogThumbAction(\'' +
    idAttr +
    "'," +
    mids +
    ",'like')\" title=\"" +
    likeLabel +
    '" aria-label="' +
    likeLabel +
    '">✓</button>';
  const passBtn =
    '<button class="catalog-row-btn pass" onclick="event.stopPropagation();catalogThumbAction(\'' +
    idAttr +
    "'," +
    mids +
    ",'pass')\" title=\"" +
    passLabel +
    '" aria-label="' +
    passLabel +
    '">✕</button>';
  let actionsHtml = "";
  if (basket === "liked") actionsHtml = passBtn;
  else if (basket === "unseen") actionsHtml = likeBtn + passBtn;
  else if (basket === "passed") actionsHtml = likeBtn;

  const dates = screenDateFacts(g);
  const url = safeUrl((g.locations || []).find(l => l?.url)?.url || "");
  const metadata = [
    dates.firstSeen && `<span class="scr-meta scr-meta--date">${escHtml(t("vac_first_seen","First seen"))}: ${dates.firstSeen}</span>`,
    dates.lastSeen && `<span class="scr-meta scr-meta--date">${escHtml(t("screen_last_seen","Last seen"))}: ${dates.lastSeen}</span>`,
    g.source_board && `<span class="scr-meta">${escHtml(g.source_board)}</span>`,
  ].filter(Boolean).join(" ");
  const progress = !["unseen","liked","passed","skipped","expiring"].includes(basket);
  const current = g.screening_state === "ready" && (!window.VACANCY_DATA.config.screening_prompt_fingerprint ||
    g.screening_fingerprint === `${g.posting_fingerprint}:${window.VACANCY_DATA.config.screening_prompt_fingerprint}`);
  const prepLabel = current ? "" : t(g.screening ? "inbox_older_facts" : "inbox_no_facts", g.screening ? "Facts need updating" : "Facts not prepared");
  if (o.review) {
    actionsHtml = progress ? '<span>' + escHtml(t("vac_status_" + basket,basket)) + '</span>' :
      [["liked","screen_keep","Like"],["passed","screen_put_aside","Pass"]].map(([status,key,label]) =>
        '<button class="catalog-row-btn" data-decision="' + status + '" data-vacancy="' + escHtml(g.id) + '"' +
        (o.disabled ? ' disabled' : '') + '>' + escHtml(t(key,label)) + '</button>').join('');
  }
  const selectHtml = o.review ? '<input type="checkbox" data-toggle="' + escHtml(g.id) + '" aria-label="' + escHtml(g.title) + '"' +
    (o.checked ? ' checked' : '') + (o.disabled || progress ? ' disabled' : '') + '>' : escHtml(scoreTxt);
  return (
    '<div class="catalog-row" data-id="' +
    escHtml(g.id) +
    '" role="button" tabindex="0" onclick="if(!event.target.closest(\'button,input,a,label,summary,details\'))openCatalogRow(\'' +
    idAttr +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openCatalogRow('" +
    idAttr +
    "')}\">" +
    '<div class="catalog-row-score ' +
    scoreCls +
    '">' +
    selectHtml +
    "</div>" +
    '<div class="catalog-row-role">' +
    '<div class="catalog-row-title-line">' +
    '<span class="catalog-row-title">' +
    escHtml(g.title) +
    "</span>" +
    deadlineHtml +
    "</div>" +
    subHtml +
    (o.review ? '<div class="scr-row-meta">' + metadata + '</div>' : "") +
    (o.review && prepLabel ? '<div class="scr-concern">' + escHtml(prepLabel) + '</div>' : '') +
    (o.review && url ? '<a class="scr-posting" href="' + escHtml(url) + '" target="_blank" rel="noopener noreferrer">' + escHtml(t("vac_open_posting","Open posting")) + ' ↗</a>' : '') +
    "</div>" +
    '<div class="catalog-row-company">' +
    '<span class="catalog-row-org">' +
    escHtml(g.company_name || g.org) +
    "</span>" +
    (o.review ? "" : tierHtml) +
    "</div>" +
    '<div class="catalog-row-loc">' +
    '<span class="scr-meta scr-meta--location">' + locHtml + "</span>" +
    "</div>" +
    '<div class="catalog-row-comp">' +
    compText +
    "</div>" +
    '<div class="catalog-row-seen">' +
    seenText +
    "</div>" +
    '<div class="catalog-row-actions">' +
    actionsHtml +
    "</div>" +
    "</div>"
  );
}


const STRENGTH_LABEL = {
  required: ["screen_required", "Required"],
  preferred: ["screen_preferred", "Preferred"],
  unknown: ["screen_unknown", "Unknown"],
};

function strengthOf(req) {
  const s = String((req && req.strength) || "").toLowerCase();
  return STRENGTH_LABEL[s] ? s : "unknown";
}

function requirementBadgeHtml(req, t) {
  const strength = strengthOf(req);
  const label = t(STRENGTH_LABEL[strength][0], STRENGTH_LABEL[strength][1]);
  const value = req && req.value ? " · " + escHtml(req.value) : "";
  return (
    '<span class="scr-badge scr-badge--' +
    strength +
    '">' +
    escHtml(label) +
    value +
    "</span>"
  );
}

function firstSentence(text) {
  const s = String(text || "").trim();
  if (!s) return "";
  const m = s.match(/^.+?[.!?](\s|$)/);
  const sentence = (m ? m[0] : s).trim();
  return sentence.length > 180 ? sentence.slice(0, 177) + "…" : sentence;
}

function factLine(facts) {
  const duties = firstSentence(facts.duties);
  if (duties) return duties;
  return [facts.function, facts.seniority]
    .filter((v) => v && String(v).toLowerCase() !== "unknown")
    .join(" · ");
}

function locationOf(g, facts) {
  const locs = (g.locations || []).filter((l) => l && l.location);
  if (locs.length) return locs[0].location;
  return facts.location || "";
}

function evidenceHtml(g, reqs, t) {
  const s = g.screening || {};
  const quotes = reqs.map((r) => {
    const q = r && r.quote ? String(r.quote).trim() : "";
    return (
      "<li><span>" +
      requirementBadgeHtml(r, t) +
      "</span>" +
      (q
        ? "<blockquote>" + escHtml(q) + "</blockquote>"
        : '<em class="scr-noquote">' +
          escHtml(t("screen_no_quote", "no quote")) +
          "</em>") +
      "</li>"
    );
  });
  const notes = (
    Array.isArray(s.profile_comparison) ? s.profile_comparison : []
  )
    .filter((c) => c && (c.note || c.finding))
    .map(
      (c) =>
        '<li><span class="scr-finding scr-finding--' +
        escHtml(String(c.finding || "unknown")) +
        '">' +
        escHtml(String(c.finding || "unknown").replace("_", " ")) +
        "</span> " +
        escHtml(c.note || "") +
        "</li>",
    );
  const unknowns = (Array.isArray(s.unknowns) ? s.unknowns : []).map(
    (u) => "<li>" + escHtml(String(u)) + "</li>",
  );
  const work = s.work_profile;
  const workEvidence = work
    ? [...(work.activities || []), work.technical_depth, work.purpose]
        .filter((item) => item?.quote)
        .map(
          (item) =>
            "<li>" +
            escHtml(
              item === work.technical_depth && item.level === "specialist"
                ? t(
                    "screen_technical_specialist",
                    "Specialist technical expertise",
                  )
                : flowLabel(item.kind || item.level, t),
            ) +
            "<blockquote>" +
            escHtml(item.quote) +
            "</blockquote></li>",
        )
        .join("")
    : "";
  return (
    (workEvidence ? '<ul class="scr-quotes">' + workEvidence + "</ul>" : "") +
    (quotes.length
      ? '<ul class="scr-quotes">' + quotes.join("") + "</ul>"
      : '<em class="scr-noquote">' +
        escHtml(t("screen_no_quote", "no quote")) +
        "</em>") +
    (notes.length || unknowns.length
      ? '<div class="scr-notes-title">' +
        escHtml(t("screen_profile_notes", "Fit")) +
        '</div><ul class="scr-notes">' +
        notes.join("") +
        unknowns.join("") +
        "</ul>"
      : "") +
    '<button type="button" class="scr-open" data-open="' +
    escHtml(g.id) +
    '">' +
    escHtml(t("screen_open", "Open")) +
    " →</button>"
  );
}

/** One row. opts: { t, checked, open, disabled } */
export function screenRowHtml(g, opts) {
  const o = opts || {};
  const t = o.t || ((k, fb) => fb);
  const facts = (g.screening && g.screening.posting_facts) || {};
  const reqs = screenRequirements(g).filter(Boolean);
  const id = escHtml(g.id);
  const org = g.company_name || g.org || "";
  const loc = locationOf(g, facts);
  const fact = factLine(facts);
  const dates = screenDateFacts(g, o.today);
  const company = resolveVacancyCompany(g, getCompanies());
  const source = g.source_board || (company && sourceLabel(company.strategy)) || "";
  const sourceText = source || t("screen_unknown", "unknown");
  const metadata = [
    ["date", dates.firstSeen && `${t("vac_first_seen", "First seen")}: ${dates.firstSeen}`],
    ["date", dates.lastSeen && `${t("screen_last_seen", "Last seen")}: ${dates.lastSeen}`],
    ["source", `${t("vac_source", "Source")}: ${sourceText}`],
    [dates.expired ? "expired" : "deadline", dates.deadline && (dates.expired
      ? fill(t("screen_deadline_passed", "Deadline passed: {date}"), { date: dates.deadline })
      : `${t("vac_deadline", "Deadline")}: ${dates.deadline}`)],
  ].filter(([, text]) => text).map(([kind, text]) =>
    `<span class="scr-meta scr-meta--${kind}">${escHtml(text)}</span>`
  ).join("");
  const postingUrl = safeUrl((g.locations || []).find((l) => l && l.url)?.url || "");
  const work = g.screening?.work_profile;
  const activities = work
    ? work.activities?.map((a) => flowLabel(a.kind, t)).join(" · ") ||
      flowLabel("unknown", t)
    : t("screen_work_unprepared", "Work details not prepared");
  const active = o.filters || {};
  const relevant =
    active.kind || active.strength || active.requirementText || active.finding
      ? screenMatchRequirements(g, active).slice(0, 2)
      : [];
  return (
    '<article class="scr-row' +
    (o.checked ? " scr-row--selected" : "") +
    '" data-id="' +
    id +
    '">' +
    '<div class="scr-row-head" role="checkbox" tabindex="0" aria-checked="' +
    (o.checked ? "true" : "false") +
    '" aria-label="' +
    escHtml(g.title || "") +
    '" data-toggle="' +
    id +
    '">' +
    '<input type="checkbox" tabindex="-1" aria-hidden="true"' +
    (o.checked ? " checked" : "") +
    ">" +
    '<div class="scr-row-main">' +
    '<div class="scr-row-title">' +
    escHtml(g.title || "") +
    "</div>" +
    '<div class="scr-row-sub">' +
    escHtml(org) +
    (loc ? ' <span class="scr-meta scr-meta--location">' + escHtml(loc) + "</span>" : "") +
    "</div>" +
    '<div class="scr-row-meta">' + metadata + "</div>" +
    (fact ? '<div class="scr-row-fact">' + escHtml(fact) + "</div>" : "") +
    (o.compact
      ? '<div class="scr-concern">' + escHtml(batchConcern(g, t)) + "</div>"
      : '<div class="scr-work">' +
        escHtml(
          [
            activities,
            facts.seniority && flowLabel(facts.seniority, t),
            facts.work_mode && flowLabel(facts.work_mode, t),
          ]
            .filter(Boolean)
            .join(" · "),
        ) +
        "</div>") +
    (relevant.length
      ? '<div class="scr-badges">' +
        relevant.map((r) => requirementBadgeHtml(r, t)).join("") +
        "</div>"
      : "") +
    "</div></div>" +
    '<div class="scr-row-actions">' +
    [
      ["liked", "screen_keep", "Like"],
      ["passed", "screen_put_aside", "Pass"],
    ].map(([status, key, label]) =>
      '<button type="button" class="scr-btn" data-decision="' + status +
      '" data-vacancy="' + id + '"' + (o.disabled ? " disabled" : "") +
      '>' + escHtml(t(key, label)) + '</button>'
    ).join("") +
    (postingUrl ? '<a class="scr-btn scr-posting" href="' + escHtml(postingUrl) +
      '" target="_blank" rel="noopener noreferrer">' +
      escHtml(t("vac_open_posting", "Open posting")) + ' ↗</a>' : "") +
    "</div>" +
    '<details class="scr-evidence" data-evidence="' +
    id +
    '"' +
    (o.open ? " open" : "") +
    "><summary>" +
    escHtml(t("screen_evidence", "Facts")) +
    "</summary>" +
    evidenceHtml(g, reqs, t) +
    "</details></article>"
  );
}

export function screenListHtml(rows, opts) {
  const t = (opts && opts.t) || ((k, fb) => fb);
  if (!rows.length)
    return (
      '<p class="scr-empty">' +
      escHtml(t("screen_empty", "No roles left in this list.")) +
      "</p>"
    );
  return rows
    .map((g) =>
      screenRowHtml(g, {
        t,
        checked: view.selected.has(g.id),
        open: view.open.has(g.id),
        filters: view.filters,
      }),
    )
    .join("");
}

/** The sticky footer. opts: { t, selected, visible, list, loaded, busy, canUndo } */
export function screenFooterHtml(o) {
  const t = o.t || ((k, fb) => fb);
  const none = !o.selected;
  const allSelected = o.visible > 0 && o.selected === o.visible;
  const off = !o.loaded || o.busy;
  const dis = (cond) => (cond ? " disabled" : "");
  return (
    '<div class="scr-footer">' +
    '<span class="scr-footer-count" aria-live="polite">' +
    (o.loaded
      ? escHtml(fill(t("screen_selected", "{n} selected"), { n: o.selected }))
      : escHtml(t("screen_loading", "Loading statuses…"))) +
    "</span>" +
    '<button type="button" class="scr-btn" id="scrSelectAll"' +
    dis(off || !o.visible) +
    ">" +
    escHtml(
      allSelected
        ? t("screen_clear", "Clear selection")
        : t("screen_select_page", "Select this page"),
    ) +
    "</button>" +
    '<button type="button" class="scr-btn scr-btn--keep" id="scrKeep"' +
    dis(off || o.decisionBlocked || none || o.list === "kept") +
    ">" +
    escHtml(t("screen_keep", "Like")) +
    "</button>" +
    '<button type="button" class="scr-btn scr-btn--aside" id="scrAside"' +
    dis(off || o.decisionBlocked || none || o.list === "putAside") +
    ">" +
    escHtml(t("screen_put_aside", "Pass")) +
    "</button>" +
    '<button type="button" class="scr-btn" id="scrUndo"' +
    dis(off || !o.canUndo) +
    ">" +
    escHtml(t("screen_undo", "Undo")) +
    "</button>" +
    "</div>"
  );
}

const LIST_KEYS = {
  toScreen: "screen_list_to_screen",
  kept: "screen_list_kept",
  putAside: "screen_list_aside",
};

function tabsHtml(lists, t) {
  return (
    '<div class="scr-tabs" role="tablist">' +
    Object.keys(LIST_KEYS)
      .map(
        (k) =>
          '<button type="button" class="scr-tab" role="tab" data-list="' +
          k +
          '" aria-selected="' +
          (view.list === k) +
          (view.busy ? '" disabled>' : '">') +
          escHtml(t(LIST_KEYS[k], k)) +
          ' <span class="scr-count">' +
          lists[k].size +
          "</span></button>",
      )
      .join("") +
    "</div>"
  );
}

export const SCREEN_FLOW_TEXT = {
  screen_take: "Can I take it?",
  screen_do: "Can I do it?",
  screen_enjoy: "Would I enjoy it?",
  screen_activity: "What would I spend my week doing?",
  screen_all_values: "Any",
  screen_building: "Building and launching",
  screen_running: "Running and improving",
  screen_selling: "Selling and relationships",
  screen_specialist: "Specialist analysis and production",
  screen_unclassified: "Work details not prepared",
  screen_work_unprepared: "Work details not prepared",
  screen_coordination: "Coordinate technical work",
  screen_practical: "Practical tools and automation",
  screen_technical_specialist: "Specialist technical expertise",
  screen_unknown: "Unknown / not stated",
  screen_direct_impact: "Direct social impact",
  screen_enabling_impact: "Enabling social impact",
  screen_commercial: "Commercial outcomes",
  screen_purpose: "Purpose of the work",
  screen_technical: "Technical depth",
  screen_seniority_filter: "Seniority",
  screen_age: "First seen",
  screen_last7: "Within 7 days",
  screen_last14: "Within 14 days",
  screen_last30: "Within 30 days",
  screen_older30: "More than 30 days ago",
  screen_deadline_filter: "Application deadline",
  screen_no_passed_deadline: "No passed deadline",
  screen_expired: "Deadline passed",
  screen_first_seen_days: "First seen {n}d ago",
  screen_deadline_passed: "Deadline passed: {date}",
  screen_work_mode: "Work arrangement",
  screen_remote: "Remote",
  screen_hybrid: "Hybrid",
  screen_onsite: "Onsite",
  screen_requirement_kind: "Requirement type",
  screen_strength_filter: "Requirement strength",
  screen_requirement_text: "Requirement name contains",
  screen_requirement_placeholder: "Language, location, skill…",
  screen_language: "Language",
  screen_location: "Location",
  screen_authorisation: "Work authorisation",
  screen_domain: "Domain knowledge",
  screen_skill: "Skill",
  screen_experience: "Experience",
  screen_education: "Education",
  screen_other: "Other",
  screen_required: "Required",
  screen_preferred: "Preferred",
  screen_finding: "Fit",
  screen_match: "Evidence in profile",
  screen_possible_conflict: "Possible conflict",
  screen_search: "Title or company",
  screen_filter_hint:
    "Filters combine. Requirement filters refer to the same requirement; text searches its name, not the quote. Unknown is not a rejection reason.",
  screen_clear_filters: "Clear filters",
  screen_matches: "{n} matching · {m} in this list",
  screen_work_availability:
    "{n} roles in this list have no work details yet. Requirement filters still work.",
  screen_page: "Page {n} of {m}",
  screen_previous: "Previous",
  screen_next: "Next batch",
  screen_select_page: "Select this page",
  screen_flow_title: "Find a batch with a shared reason to decide.",
};

function flowLabel(value, t) {
  const key = "screen_" + value;
  return t(key, SCREEN_FLOW_TEXT[key] || String(value).replaceAll("_", " "));
}

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

let wired = false;
let lastVisible = [];

function toast(text, cls) {
  showToastText(text, cls, 2500);
}

function notesHtml() {
  if (!view.notes) return "";
  return (
    '<div class="scr-review-notes">' +
    (view.notes.length
      ? view.notes
          .map(
            (n) =>
              "<article><strong>" +
              escHtml(n.group_label) +
              " · " +
              escHtml(
                n.decision === "liked"
                  ? T("screen_keep", "Like")
                  : T("screen_put_aside", "Pass"),
              ) +
              "</strong><p>" +
              escHtml(n.reason) +
              "</p><small>" +
              escHtml(
                n.status === "reviewed"
                  ? T("screen_note_reviewed", "Reviewed by AI")
                  : T("screen_note_pending", "Waiting for AI review"),
              ) +
              "</small>" +
              (n.review_outcome
                ? "<p>" + escHtml(n.review_outcome) + "</p>"
                : "") +
              "</article>",
          )
          .join("")
      : "<p>" +
        escHtml(T("screen_no_notes", "No review notes yet.")) +
        "</p>") +
    "</div>"
  );
}

export function renderScreen() {
  const el = document.getElementById("screenSection");
  if (!el) return;
  if (!recoveredFeedback) {
    recoveredFeedback = true;
    try {
      const pending = JSON.parse(
        localStorage.getItem("screen-pending-feedback") || "null",
      );
      if (
        pending?.id &&
        Array.isArray(pending.vacancy_ids) &&
        typeof pending.reason === "string"
      ) {
        view.feedback = pending;
        view.feedbackState = "failed";
      }
    } catch {
      /* No stored draft to recover. */
    }
  }
  const model = reviewModel(groups, getGroupStatus);
  lastVisible = model.visibleIds;
  const pendingFeedback = view.feedbackState === "failed";
  const dis = view.busy ? " disabled" : "";
  const pagination = '<div class="scr-pagination"><button class="scr-btn" data-page="' +
    (view.page - 1) + '"' + (view.page === 0 || view.busy ? ' disabled' : '') + '>' +
    escHtml(T("screen_previous", "Previous")) + '</button><span>' + (view.page + 1) + ' / ' + model.pages +
    '</span><button class="scr-btn" data-page="' + (view.page + 1) + '"' +
    (view.page >= model.pages - 1 || view.busy ? ' disabled' : '') + '>' +
    escHtml(T("screen_next", "Next batch")) + '</button></div>';
  el.innerHTML =
    '<div class="scr-head"><h2 class="scr-title">' +
    escHtml(T("screen_review_title", "Review vacancies")) +
    "</h2></div>" +
    tabsHtml(model.lists, T) +
    inboxFiltersHtml() +
    '<div class="scr-review-layout"><nav class="scr-batch-nav" aria-label="Job functions">' +
    [{key: "", label: T("contacts_all_groups", "All"), roles: {length: model.batches.reduce((n,b) => n + b.roles.length, 0)}}, ...model.batches].map(b =>
      '<button class="scr-batch" data-batch="' + escHtml(b.key) + '" aria-pressed="' +
      (!b.key ? !view.batch : view.batch === b.key) + '"' + dis + '><strong>' +
      escHtml(b.key ? T("screen_batch_" + b.key,b.label) : b.label) + '</strong><span>' + b.roles.length +
      ' ' + escHtml(T("screen_roles", "roles")) + '</span></button>').join('') + '</nav>' +
    '<section class="scr-review-sheet scr-inbox"><h3>' + escHtml(model.batch ? T("screen_batch_" + model.batch.key,model.batch.label) : T("contacts_all_groups","All")) + '</h3>' +
    '<p class="scr-matches" tabindex="-1">' +
    escHtml(
      fill(
        T(
          "screen_batch_showing",
          "Showing {start}–{end} of {total} in this group",
        ),
        {
          start: model.total ? view.page * REVIEW_SIZE + 1 : 0,
          end: Math.min((view.page + 1) * REVIEW_SIZE, model.total),
          total: model.total,
        },
      ),
    ) +
    "</p>" +
    pagination +
    '<div class="catalog-sheet"><div class="catalog-table">' +
    (model.rows.length ? model.rows.map(g => catalogRowHtml(g, getGroupStatus(g), {
      t: T, review: true, checked: view.selected.has(g.id),
      disabled: view.busy || !state.statusesLoaded || !!pendingFeedback || !!pendingDecision,
    })).join("") : '<p>' + escHtml(T("screen_empty", "No roles left in this list.")) + '</p>') +
    '</div></div>' +
    pagination +
    '<label class="scr-reason">' +
    escHtml(
      T(
        "screen_reason_label",
        "Why? Optional — saved with your next decision.",
      ),
    ) +
    '<textarea id="scrReason" maxlength="4000" rows="2"' +
    dis +
    ">" +
    escHtml(view.reason) +
    "</textarea></label>" +
    '<p class="scr-feedback-state" role="status">' +
    (pendingFeedback
      ? escHtml(
          T(
            "screen_feedback_failed",
            "Your job decisions were saved, but your reason was not. Retry before another decision.",
          ),
        ) +
        ' <button class="scr-btn" id="scrRetryFeedback">' +
        escHtml(T("screen_retry", "Retry")) +
        "</button>"
      : view.feedbackState === "saved"
        ? escHtml(
            T(
              "screen_feedback_saved",
              "Reason saved · waiting for AI review. No preference changed.",
            ),
          )
        : "") +
    "</p>" +
    '<p class="scr-notice" role="status">' +
    (pendingDecision
      ? escHtml(T("screen_decision_pending", "A save needs confirmation. Retry to recover it safely.")) +
        ' <button class="scr-btn" id="scrRetryDecision">' + escHtml(T("screen_retry", "Retry")) + '</button>'
      : escHtml(view.notice)) +
    "</p>" +
    screenFooterHtml({
      t: T,
      selected: view.selected.size,
      visible: model.rows.filter(g => canDecide(g)).length,
      list: view.list,
      loaded: state.statusesLoaded,
      busy: view.busy,
      decisionBlocked: pendingFeedback || !!pendingDecision,
      canUndo: history.length > 0 && !pendingDecision,
    }) +
    "</section></div>" +
    '<button class="scr-btn" id="scrLoadNotes">' +
    escHtml(T("screen_past_notes", "Past review notes")) +
    "</button>" +
    (view.notesError
      ? '<p role="alert">' +
        escHtml(
          T("screen_notes_failed", "Could not load review notes. Try again."),
        ) +
        "</p>"
      : "") +
    notesHtml();
  if (!wired) {
    el.addEventListener("click", onClick);
    el.addEventListener("keydown", onKeydown);
    el.addEventListener("toggle", onToggle, true);
    el.addEventListener("change", (e) => {
      if (!e.target.dataset.filter) return;
      const key = e.target.dataset.filter;
      if (key === "function") view.batch = e.target.value || null;
      else view.filters[key] = e.target.value;
      view.page = 0; view.selected.clear(); renderScreen();
    });
    el.addEventListener("input", (e) => {
      if (e.target.id === "scrReason") view.reason = e.target.value;
    });
    wired = true;
  }
}

function onToggle(e) {
  if (!e.target.isConnected) return;
  const id = e.target && e.target.getAttribute("data-evidence");
  if (!id) return;
  if (e.target.open) view.open.add(id);
  else view.open.delete(id);
}

function onKeydown(e) {
  if (view.busy) return;
  const head = e.target.closest && e.target.closest("[data-toggle]");
  if (!head || (e.key !== " " && e.key !== "Enter")) return;
  e.preventDefault();
  toggleSelected(head.getAttribute("data-toggle"));
  renderScreen();
}

function onClick(e) {
  if (view.busy) return;
  const t = e.target;
  const hit = (sel) => t.closest && t.closest(sel);
  let el;
  if (hit("#inboxClear")) {
    view.filters = {}; view.batch = null; view.newOnly = false; view.page = 0; view.selected.clear(); renderScreen();
  } else if (hit("#inboxNew")) {
    view.newOnly = !view.newOnly; view.page = 0; view.selected.clear(); renderScreen();
  } else if (hit("#inboxFinish")) {
    try {
      const ids = groups.map(g => g.id);
      localStorage.setItem("inbox-review-ids", JSON.stringify(ids));
      reviewedIds = new Set(ids); view.newOnly = false;
      view.notice = T("inbox_review_saved", "Review checkpoint saved on this device. Undecided vacancies remain in Inbox.");
    } catch { view.notice = T("screen_save_failed", "Could not save."); }
    renderScreen();
  } else if (hit("#scrRetryDecision")) {
    view.reason = pendingDecision.reason || "";
    view.batch = pendingDecision.batch;
    if (pendingDecision.undo) runUndo(true);
    else runBulk(pendingDecision.status, [pendingDecision.id], true);
  } else if (hit("#scrLoadNotes")) {
    view.busy = true;
    renderScreen();
    fetch(API_BASE + "/api/screening-feedback", { credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) throw new Error("notes");
        const data = await r.json();
        if (!Array.isArray(data.items)) throw new Error("notes");
        view.notes = data.items;
        view.notesError = false;
      })
      .catch(() => {
        view.notesError = true;
      })
      .finally(() => {
        view.busy = false;
        renderScreen();
      });
  } else if (hit("#scrRetryFeedback")) {
    view.busy = true;
    renderScreen();
    saveReviewFeedback().finally(() => {
      view.busy = false;
      renderScreen();
    });
  } else if ((el = hit("[data-batch]"))) {
    view.batch = el.getAttribute("data-batch") || null;
    view.reason = "";
    setPage(0);
    renderScreen();
  } else if ((el = hit("[data-page]"))) {
    view.reason = "";
    setPage(Number(el.getAttribute("data-page")));
    renderScreen();
    const row =
      document.querySelector("#screenSection [data-toggle]") ||
      document.querySelector("#screenSection .scr-matches");
    row?.focus({ preventScroll: true });
    row?.scrollIntoView({ block: "start" });
  } else if ((el = hit("[data-toggle]"))) {
    e.preventDefault();
    toggleSelected(el.getAttribute("data-toggle"));
    renderScreen();
  } else if ((el = hit("[data-list]"))) {
    setList(el.getAttribute("data-list"));
    renderScreen();
  } else if ((el = hit("[data-group]"))) {
    setGroup(el.getAttribute("data-group"));
    renderScreen();
  } else if ((el = hit("[data-open]"))) {
    if (window.openVacancyRoute)
      window.openVacancyRoute(el.getAttribute("data-open"), {
        context: "screen",
        queue: view.list === "toScreen" ? lastVisible.slice() : [],
      });
  } else if (hit("#scrSelectAll")) {
    toggleSelectAll(lastVisible.filter(id => canDecide(groupsById.get(id))));
    renderScreen();
  } else if ((el = hit("[data-decision]"))) {
    const id = el.getAttribute("data-vacancy");
    const status = el.getAttribute("data-decision");
    if (lastVisible.includes(id) && canDecide(groupsById.get(id)) && ["liked", "passed"].includes(status))
      runBulk(status, [id]);
  } else if (hit("#scrKeep")) {
    runBulk("liked");
  } else if (hit("#scrAside")) {
    runBulk("passed");
  } else if (hit("#scrUndo")) {
    runUndo();
  }
}

async function runBulk(status, requestedIds, retry = false) {
  if (view.busy || view.feedback || !state.statusesLoaded || (pendingDecision && !retry)) return;
  const ids = requestedIds || lastVisible.filter((id) => view.selected.has(id) && canDecide(groupsById.get(id)));
  if (!ids.length) return;
  view.busy = true;
  renderScreen();
  const groupLabel =
    view.list === "toScreen"
      ? reviewModel(groups, getGroupStatus).batch?.label || "Other roles"
      : T(LIST_KEYS[view.list], view.list);
  let r;
  try {
    r = await bulkSet(ids, status);
    const note = feedbackFor(
      r.op,
      view.reason,
      groupLabel,
      crypto.randomUUID(),
    );
    if (note) {
      view.feedback = note;
      await saveReviewFeedback();
    }
    if (r.saved) view.reason = "";
  } catch {
    view.notice = T(
      "screen_save_failed",
      "Could not finish saving. Check the list before retrying.",
    );
    view.busy = false;
    renderScreen();
    return;
  }
  view.busy = false;
  for (const id of ids) view.selected.delete(id);
  view.notice = fill(T("screen_saved", "{n} of {m} saved"), {
    n: r.saved,
    m: r.total,
  });
  toast(view.notice, r.saved === r.total ? status : "passed");
  scheduleRender();
  renderScreen();
}

async function runUndo(retry = false) {
  if (view.busy || !state.statusesLoaded || (pendingDecision && !retry)) return;
  view.busy = true;
  renderScreen();
  const r = await undoLast();
  view.busy = false;
  view.selected.clear();
  if (r) {
    view.notice = fill(T("screen_undone", "{n} of {m} restored"), {
      n: r.restored,
      m: r.total,
    });
    toast(view.notice, r.restored === r.total ? "liked" : "passed");
  }
  scheduleRender();
  renderScreen();
}
