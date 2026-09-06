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
import { escHtml } from "./helpers.js";
import { T } from "./i18n.js";
import {
  screenLists,
  screenGroups,
  screenRequirements,
  SCREEN_GROUP_KEYS,
} from "./derive.js";

// ---------------------------------------------------------------------------
// View state (module-local). Selection survives render ticks; any list or
// group change clears it (R10).
// ---------------------------------------------------------------------------

export const view = {
  list: "toScreen", // "toScreen" | "kept" | "putAside"
  group: "all", // one of SCREEN_GROUP_KEYS; filters the To screen list only
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

export function screenModel(roles, getStatus) {
  const lists = screenLists(roles, getStatus);
  const groupSets = screenGroups(roles.filter((g) => lists.toScreen.has(g.id)));
  const visibleIds =
    view.list === "toScreen"
      ? [...(groupSets[view.group] || groupSets.all)]
      : [...lists[view.list]];
  return { lists, groupSets, visibleIds };
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
    return ids.filter((mid, i) => state.dbData[mid] && ids.indexOf(mid) === i);
  },
  set: (mid, status) => setStatusLocal([mid], status)[mid],
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
    const ok = await writeRow(row.member_ids, (mid) => row.previous[mid], io);
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
  return (m ? m[0] : s).trim();
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
      escHtml((r && r.value) || "") +
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
  return (
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
    (fact ? '<div class="scr-row-fact">' + escHtml(fact) + "</div>" : "") +
    (reqs.length
      ? '<div class="scr-badges">' +
        reqs.map((r) => requirementBadgeHtml(r, t)).join("") +
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
        : t("screen_select_all", "Select all"),
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
          '">' +
          escHtml(t(LIST_KEYS[k], k)) +
          ' <span class="scr-count">' +
          lists[k].size +
          "</span></button>",
      )
      .join("") +
    "</div>"
  );
}

function groupsHtml(groupSets, t) {
  return (
    '<div class="scr-groups"' +
    (view.list === "toScreen" ? "" : " hidden") +
    ">" +
    SCREEN_GROUP_KEYS.map(
      (k) =>
        '<button type="button" class="scr-group scr-group--' +
        k +
        '" data-group="' +
        k +
        '" aria-pressed="' +
        (view.group === k) +
        '"><span class="scr-group-dot"></span>' +
        escHtml(t("screen_group_" + k, k)) +
        ' <span class="scr-count">' +
        groupSets[k].size +
        "</span></button>",
    ).join("") +
    "</div>"
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
let toastTimer = null;

function toast(text, cls) {
  const el = document.querySelector(".toast");
  if (!el) return;
  el.className = "toast toast-" + cls;
  el.textContent = text;
  requestAnimationFrame(() =>
    requestAnimationFrame(() => el.classList.add("visible")),
  );
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("visible"), 2500);
}

export function renderScreen() {
  const el = document.getElementById("screenSection");
  if (!el) return;
  const model = screenModel(groups, getGroupStatus);
  lastVisible = model.visibleIds;
  const rows = model.visibleIds.map((id) => groupsById.get(id)).filter(Boolean);
  el.innerHTML =
    '<div class="scr-head"><h2 class="scr-title">' +
    escHtml(T("screen_title", "Make one decision about several roles.")) +
    "</h2>" +
    processingHtml(T) +
    "</div>" +
    tabsHtml(model.lists, T) +
    groupsHtml(model.groupSets, T) +
    '<p class="scr-hint"' +
    (view.list === "toScreen" ? "" : " hidden") +
    ">" +
    escHtml(T("screen_group_hint", "Groups filter the To screen list only.")) +
    "</p>" +
    '<div class="scr-list">' +
    screenListHtml(rows, { t: T }) +
    "</div>" +
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
    wired = true;
  }
}

function onToggle(e) {
  const id = e.target && e.target.getAttribute("data-evidence");
  if (!id) return;
  if (e.target.open) view.open.add(id);
  else view.open.delete(id);
}

function onKeydown(e) {
  const head = e.target.closest && e.target.closest("[data-toggle]");
  if (!head || (e.key !== " " && e.key !== "Enter")) return;
  e.preventDefault();
  toggleSelected(head.getAttribute("data-toggle"));
  renderScreen();
}

function onClick(e) {
  const t = e.target;
  const hit = (sel) => t.closest && t.closest(sel);
  let el;
  if ((el = hit("[data-toggle]"))) {
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
        queue: lastVisible.slice(),
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
