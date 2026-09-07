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
  groups,
  groupsById,
  stats,
  getGroupStatus,
  setStatusLocal,
  scheduleRender,
} from "./state.js";
import { saveToServer } from "./api.js";
import { escHtml, showToastText } from "./helpers.js";
import { T } from "./i18n.js";
import {
  screenLists,
  screenGroups,
  screenRequirements,
  SCREEN_ACTIVITIES,
  screenMatches,
  screenMatchRequirements,
  screenDateFacts,
} from "./derive.js";

// ---------------------------------------------------------------------------
// View state (module-local). Selection survives render ticks; any list or
// group change clears it (R10).
// ---------------------------------------------------------------------------

export const view = {
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
const history = [];

export function setList(name) {
  if (view.list === name) return;
  view.list = name;
  view.page = 0;
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
  const lists = screenLists(roles, getStatus);
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

/** "{n} of {m}" → values. */
export function fill(template, vars) {
  return String(template).replace(/\{(\w+)\}/g, (m, k) =>
    k in vars ? String(vars[k]) : m,
  );
}

// ---------------------------------------------------------------------------
// Write path
// ---------------------------------------------------------------------------

const liveIo = {
  members: (id) => {
    const g = groupsById.get(id);
    const ids = [id].concat((g && g.member_ids) || []);
    const seen = new Set();
    return ids.filter(
      (mid) => state.dbData[mid] && !seen.has(mid) && seen.add(mid),
    );
  },
  set: (mid, status) => setStatusLocal([mid], status)[mid],
  current: (mid) => state.dbData[mid] && state.dbData[mid].status,
  save: saveToServer,
};

// Write one row's member ids, await every save, revert the whole row when any
// member fails (re-saving the members that had already landed). Returns the
// previous status per member id, or null when the row reverted.
async function writeRow(memberIds, targetOf, io) {
  const previous = {};
  for (const mid of memberIds) previous[mid] = io.set(mid, targetOf(mid));
  const results = await Promise.all(
    memberIds.map((mid) => io.save(mid, targetOf(mid))),
  );
  if (results.every(Boolean)) return previous;
  memberIds.forEach((mid, i) => {
    io.set(mid, previous[mid]);
    if (results[i]) io.save(mid, previous[mid]);
  });
  return null;
}

/**
 * Keep (liked) or Put aside (passed) the given canonical ids. Pushes one
 * operation with the saved rows only. @returns {{saved, total, op}}
 */
export async function bulkSet(ids, status, io) {
  io = io || liveIo;
  const rows = [];
  for (const id of ids) {
    const members = io.members(id);
    if (!members.length) continue;
    const previous = await writeRow(members, () => status, io);
    if (previous) rows.push({ id, member_ids: members, previous });
  }
  const op = rows.length ? { status, rows } : null;
  if (op) history.push(op);
  return { saved: rows.length, total: ids.length, op };
}

/** Pop the last operation and restore each of its rows to its recorded previous. */
export async function undoLast(io) {
  io = io || liveIo;
  const op = history.pop();
  if (!op) return null;
  let restored = 0;
  for (const row of op.rows) {
    // Only members still carrying this operation's status are restored; a
    // decision made after the bulk action (say, "applied") is never overwritten.
    const still = row.member_ids.filter((mid) => io.current(mid) === op.status);
    if (!still.length) continue;
    const ok = await writeRow(still, (mid) => row.previous[mid], io);
    if (ok) restored++;
  }
  return { restored, total: op.rows.length };
}

// ---------------------------------------------------------------------------
// Row assembly (pure)
// ---------------------------------------------------------------------------

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
        escHtml(t("screen_profile_notes", "Compared with your profile")) +
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

/** One row. opts: { t, checked, open } */
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
  const seen =
    dates.age != null && dates.age >= 0
      ? fill(t("screen_first_seen_days", "First seen {n}d ago"), {
          n: dates.age,
        })
      : "";
  const expiry = dates.expired
    ? fill(t("screen_deadline_passed", "Deadline passed: {date}"), {
        date: dates.deadline,
      })
    : "";
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
    (loc ? " · " + escHtml(loc) : "") +
    "</div>" +
    (seen ? '<div class="scr-work">' + escHtml(seen) + "</div>" : "") +
    (expiry ? '<div class="scr-expired">' + escHtml(expiry) + "</div>" : "") +
    (fact ? '<div class="scr-row-fact">' + escHtml(fact) + "</div>" : "") +
    '<div class="scr-work">' +
    escHtml(
      [
        activities,
        facts.seniority && flowLabel(facts.seniority, t),
        facts.work_mode && flowLabel(facts.work_mode, t),
      ]
        .filter(Boolean)
        .join(" · "),
    ) +
    "</div>" +
    (relevant.length
      ? '<div class="scr-badges">' +
        relevant.map((r) => requirementBadgeHtml(r, t)).join("") +
        "</div>"
      : "") +
    "</div></div>" +
    '<details class="scr-evidence" data-evidence="' +
    id +
    '"' +
    (o.open ? " open" : "") +
    "><summary>" +
    escHtml(t("screen_evidence", "Read posting evidence")) +
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
    dis(off || none || o.list === "kept") +
    ">" +
    escHtml(t("screen_keep", "Keep")) +
    "</button>" +
    '<button type="button" class="scr-btn scr-btn--aside" id="scrAside"' +
    dis(off || none || o.list === "putAside") +
    ">" +
    escHtml(t("screen_put_aside", "Put aside")) +
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
  screen_finding: "Compared with my profile",
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

function filtersHtml(roles, t) {
  const label = (key) => t(key, SCREEN_FLOW_TEXT[key]);
  const select = (key, title, values) =>
    "<label>" +
    escHtml(label(title)) +
    '<select data-filter="' +
    key +
    '"><option value="">' +
    escHtml(label("screen_all_values")) +
    "</option>" +
    values
      .map(
        (value) =>
          '<option value="' +
          escHtml(value) +
          '"' +
          (view.filters[key] === value ? " selected" : "") +
          ">" +
          escHtml(
            key === "deadline" && value === "open"
              ? label("screen_no_passed_deadline")
              : key === "technical" && value === "specialist"
                ? label("screen_technical_specialist")
                : flowLabel(value, t),
          ) +
          "</option>",
      )
      .join("") +
    "</select></label>";
  const input = (key, title) =>
    "<label>" +
    escHtml(label(title)) +
    '<input type="search" data-filter="' +
    key +
    '" value="' +
    escHtml(view.filters[key] || "") +
    '"></label>';
  const questionKeys = {
    screen_enjoy: ["activity", "purpose"],
    screen_take: [
      "age",
      "deadline",
      "workMode",
      "kind",
      "strength",
      "requirementText",
      "finding",
    ],
    screen_do: ["technical", "seniority"],
  };
  const fieldset = (title, content) => {
    const active = questionKeys[title].filter(
      (key) => view.filters[key],
    ).length;
    return (
      '<details class="scr-question" data-question="' +
      title +
      '"' +
      (view.filterOpen.has(title) ? " open" : "") +
      "><summary>" +
      escHtml(label(title)) +
      (active ? ' <span class="scr-count">' + active + "</span>" : "") +
      '</summary><div class="scr-question-fields">' +
      content +
      "</div></details>"
    );
  };
  const seniorities = [
    ...new Set(
      roles.map((g) => g.screening?.posting_facts?.seniority || "unknown"),
    ),
  ].sort();
  return (
    '<fieldset class="scr-filters"' +
    (view.busy ? " disabled" : "") +
    ">" +
    fieldset(
      "screen_enjoy",
      select("activity", "screen_activity", [
        ...SCREEN_ACTIVITIES,
        "unknown",
        "unclassified",
      ]) +
        select("purpose", "screen_purpose", [
          "direct_impact",
          "enabling_impact",
          "commercial",
          "unknown",
        ]),
    ) +
    fieldset(
      "screen_take",
      select("age", "screen_age", ["last7", "last14", "last30", "older30"]) +
        select("deadline", "screen_deadline_filter", [
          "open",
          "expired",
          "unknown",
        ]) +
        select("workMode", "screen_work_mode", [
          "remote",
          "hybrid",
          "onsite",
          "unknown",
        ]) +
        select("kind", "screen_requirement_kind", [
          "language",
          "location",
          "authorisation",
          "skill",
          "domain",
          "experience",
          "education",
          "other",
        ]) +
        select("strength", "screen_strength_filter", [
          "required",
          "preferred",
          "unknown",
        ]) +
        input("requirementText", "screen_requirement_text") +
        select("finding", "screen_finding", [
          "match",
          "possible_conflict",
          "unknown",
        ]),
    ) +
    fieldset(
      "screen_do",
      select("technical", "screen_technical", [
        "coordination",
        "practical",
        "specialist",
        "unknown",
      ]) + select("seniority", "screen_seniority_filter", seniorities),
    ) +
    input("search", "screen_search") +
    '<button type="button" class="scr-btn" id="scrClearFilters">' +
    escHtml(label("screen_clear_filters")) +
    "</button></fieldset>"
  );
}

function pageHtml(model, t) {
  return (
    '<nav class="scr-pagination" aria-label="' +
    escHtml(t("screen_batches", "Screening batches")) +
    '"><button class="scr-btn" data-page="' +
    (view.page - 1) +
    '"' +
    (view.busy || view.page === 0 ? " disabled" : "") +
    ">" +
    escHtml(t("screen_previous", "Previous")) +
    "</button><span>" +
    escHtml(
      fill(t("screen_page", "Page {n} of {m}"), {
        n: view.page + 1,
        m: model.pages,
      }),
    ) +
    '</span><button class="scr-btn" data-page="' +
    (view.page + 1) +
    '"' +
    (view.busy || view.page + 1 >= model.pages ? " disabled" : "") +
    ">" +
    escHtml(t("screen_next", "Next batch")) +
    "</button></nav>"
  );
}

function processingHtml(t) {
  const p = stats && stats.screening_processing;
  if (!p) return "";
  return (
    '<p class="scr-processing">' +
    escHtml(
      fill(
        t(
          "screen_processing",
          "Not prepared yet: {unprepared} · Failed: {failed}",
        ),
        {
          unprepared: p.unprepared || 0,
          failed: p.failed || 0,
        },
      ),
    ) +
    "</p>"
  );
}

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

let wired = false;
let lastVisible = [];

function toast(text, cls) {
  showToastText(text, cls, 2500);
}

export function renderScreen() {
  const el = document.getElementById("screenSection");
  if (!el) return;
  const model = screenModel(groups, getGroupStatus);
  lastVisible = model.visibleIds;
  const rows = model.visibleIds.map((id) => groupsById.get(id)).filter(Boolean);
  el.innerHTML =
    '<div class="scr-head"><h2 class="scr-title">' +
    escHtml(T("screen_flow_title", SCREEN_FLOW_TEXT.screen_flow_title)) +
    "</h2>" +
    processingHtml(T) +
    "</div>" +
    tabsHtml(model.lists, T) +
    filtersHtml(groups, T) +
    '<p class="scr-hint">' +
    escHtml(T("screen_filter_hint", SCREEN_FLOW_TEXT.screen_filter_hint)) +
    "</p>" +
    '<p class="scr-hint">' +
    escHtml(
      fill(
        T(
          "screen_work_availability",
          SCREEN_FLOW_TEXT.screen_work_availability,
        ),
        { n: model.unclassified },
      ),
    ) +
    "</p>" +
    '<p class="scr-matches" role="status" tabindex="-1">' +
    escHtml(
      fill(T("screen_matches", SCREEN_FLOW_TEXT.screen_matches), {
        n: model.matchingIds.length,
        m: model.lists[view.list].size,
      }),
    ) +
    "</p>" +
    pageHtml(model, T) +
    '<div class="scr-list">' +
    screenListHtml(rows, { t: T }) +
    "</div>" +
    pageHtml(model, T) +
    '<p class="scr-notice" role="status" aria-live="polite">' +
    escHtml(view.notice) +
    "</p>" +
    screenFooterHtml({
      t: T,
      selected: view.selected.size,
      visible: rows.length,
      list: view.list,
      loaded: state.statusesLoaded,
      busy: view.busy,
      canUndo: history.length > 0,
    });
  if (!wired) {
    el.addEventListener("click", onClick);
    el.addEventListener("keydown", onKeydown);
    el.addEventListener("toggle", onToggle, true);
    el.addEventListener("change", onFilterChange);
    el.addEventListener("input", (e) => {
      if (e.target.matches("input[data-filter]")) onFilterChange(e);
    });
    wired = true;
  }
}

function onFilterChange(e) {
  const key = e.target.getAttribute("data-filter");
  if (!key || view.busy || (view.filters[key] || "") === e.target.value) return;
  const start = e.target.selectionStart;
  const end = e.target.selectionEnd;
  setFilter(key, e.target.value);
  renderScreen();
  const control = document.querySelector('[data-filter="' + key + '"]');
  control?.focus();
  if (start != null) control?.setSelectionRange(start, end);
}

function onToggle(e) {
  if (!e.target.isConnected) return;
  const question = e.target?.getAttribute("data-question");
  if (question) {
    if (e.target.open) view.filterOpen.add(question);
    else view.filterOpen.delete(question);
    return;
  }
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
  if ((el = hit("[data-page]"))) {
    setPage(Number(el.getAttribute("data-page")));
    renderScreen();
    const row =
      document.querySelector("#screenSection [data-toggle]") ||
      document.querySelector("#screenSection .scr-matches");
    row?.focus({ preventScroll: true });
    row?.scrollIntoView({ block: "start" });
  } else if (hit("#scrClearFilters")) {
    view.filters = {};
    setFilter("search", "");
    renderScreen();
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
    toggleSelectAll(lastVisible);
    renderScreen();
  } else if (hit("#scrKeep")) {
    runBulk("liked");
  } else if (hit("#scrAside")) {
    runBulk("passed");
  } else if (hit("#scrUndo")) {
    runUndo();
  }
}

async function runBulk(status) {
  if (view.busy || !state.statusesLoaded) return;
  const ids = lastVisible.filter((id) => view.selected.has(id));
  if (!ids.length) return;
  view.busy = true;
  renderScreen();
  const r = await bulkSet(ids, status);
  view.busy = false;
  view.selected.clear();
  view.notice = fill(T("screen_saved", "{n} of {m} saved"), {
    n: r.saved,
    m: r.total,
  });
  toast(view.notice, r.saved === r.total ? status : "passed");
  scheduleRender();
  renderScreen();
}

async function runUndo() {
  if (view.busy || !state.statusesLoaded) return;
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
