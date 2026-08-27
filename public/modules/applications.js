// =============================================================================
// applications.js — the Triage tab's second view: every application ever sent,
// as one table.
//
// The kanban board answers "where is each application". It cannot answer the
// two questions asked more often: what have I sent, and what is waiting on
// whom. Those need one row per application, sorted by date, with a send date
// and a waiting time — none of which fits on a card, and all of which the
// board's column layout actively hides (a role sent in May sits beside one
// sent yesterday, in whatever order the score put them).
//
// Same data as the board, second view: no separate fetch, no second source of
// truth. The toggle above the board switches between them and is remembered.
//
// The pure half (selection, sort, counts, formatting, the row markup) is
// exported for `node --test`; only renderApplicationsTable touches the DOM.
// Same split palette.js / keys.js use, and for the same reason — pipeline.js
// cannot be imported under the test runner, so nothing testable may live
// beside it.
// =============================================================================

import {
  state,
  groups,
  triageReviews,
  TRIAGE_COLUMNS,
  STATUS_PRI,
  getGroupStatus,
} from "./state.js";
import {
  escHtml,
  jsAttr,
  safeUrl,
  dedupeTriageEntries,
  pluralForm,
} from "./helpers.js";
import { T, dateLocale } from "./i18n.js";

// The statuses that mean an application was actually sent. Twin of
// scripts/statuses.py APPLICATION_STATUSES and server.js's copy — a status
// missing here is an application the table silently omits.
export const APPLICATION_STATUSES = [
  "applied",
  "test_task",
  "interview",
  "declined",
  "accepted",
];

// Outcomes: the application ended, so nothing is waiting on anyone. Their rows
// show no waiting time — a rejection from March is not "168 days late".
export const CLOSED_STATUSES = new Set(["declined", "accepted"]);

// Applications still in flight where the ball is in the employer's court.
const WAITING_STATUSES = new Set(["applied"]);
// Applications where something is actually happening.
const IN_PROGRESS_STATUSES = new Set(["test_task", "interview"]);

// What was applied to. Plain words, not codes: the table is read, not parsed.
export const KIND_LABELS = {
  job: "Job",
  programme: "Programme",
  advising: "Advising",
  consulting: "Consulting",
  grant: "Grant",
  course: "Course",
};

// localStorage key for the Board | Table choice. Namespaced like the language
// key so an unrelated key can never collide with it.
export const TRIAGE_VIEW_KEY = "jobdash.triageView";
export const TRIAGE_VIEWS = ["board", "table"];

const MS_PER_DAY = 86400000;

// ---------------------------------------------------------------------------
// Remembered view
// ---------------------------------------------------------------------------

/** The remembered Board|Table choice, defaulting to the board.
 *  `store` is a localStorage-like object; a throwing or absent one reads as
 *  "no preference" rather than breaking the tab (private-mode Safari throws
 *  on getItem, and the module also loads under the test runner). */
export function readTriageView(store) {
  let saved = null;
  try {
    saved = store && store.getItem(TRIAGE_VIEW_KEY);
  } catch (e) {
    saved = null;
  }
  return TRIAGE_VIEWS.includes(saved) ? saved : "board";
}

/** Persist the choice. Returns the value now in effect, so a failed write
 *  still drives this session's render instead of silently reverting. */
export function writeTriageView(store, view) {
  const next = TRIAGE_VIEWS.includes(view) ? view : "board";
  try {
    if (store) store.setItem(TRIAGE_VIEW_KEY, next);
  } catch (e) {
    /* ignore — the return value still applies it for this session */
  }
  return next;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

/** A date as "17 Aug" — the shortest form that stays unambiguous down a
 *  column. Returns "" for a missing or unparseable value, never "Invalid
 *  Date".
 *
 *  Composed from the two parts rather than handed to toLocaleDateString as a
 *  whole date: the app runs under en-US, which orders the same options as
 *  "Aug 17". Day-first is the column's design (the day is what the eye scans,
 *  and it lines up), so the ORDER is fixed here while the month NAME still
 *  comes from the active locale, so a Russian dashboard gets a Russian month
 *  name rather than a hardcoded English table.
 *
 *  Pinned to UTC so a timestamp written just after midnight in one timezone
 *  does not render as the previous day in another. */
export function formatDayMonth(value, locale) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const month = d.toLocaleDateString(locale || "en-GB", {
    month: "short",
    timeZone: "UTC",
  });
  return d.getUTCDate() + " " + month;
}

/** Whole days between `since` and `now`, or null when there is no usable date.
 *  Never negative: a clock skew or a future-dated row reads as 0, not "-1 d". */
export function daysSince(since, now) {
  if (!since) return null;
  const then = new Date(since).getTime();
  if (Number.isNaN(then)) return null;
  const ref = now == null ? Date.now() : now;
  return Math.max(0, Math.floor((ref - then) / MS_PER_DAY));
}

/**
 * The waiting cell: "9 days", or "" when nothing is waiting.
 *
 * The unit is a word, not the letter "d". A bare "9 d" is a code the reader has
 * to decode, and the house style asks for every number on screen to be labelled
 * in words. Languages with three plural forms need the form chosen by the
 * count's last digit rather than by `n === 1`, which is what pluralForm does.
 */
export function formatWaiting(days, t) {
  if (days == null) return "";
  const translate = t || ((k, fb) => fb);
  const form = pluralForm(days);
  const fallback = days === 1 ? "day" : "days";
  return days + " " + translate("apps_waiting_day_" + form, fallback);
}

// ---------------------------------------------------------------------------
// Selection
// ---------------------------------------------------------------------------

/** Look up one group's private triage review by its id or any member id. */
function reviewFor(g, reviewByVid) {
  if (reviewByVid[g.id]) return reviewByVid[g.id];
  for (const mid of Array.isArray(g.member_ids) ? g.member_ids : []) {
    if (reviewByVid[mid]) return reviewByVid[mid];
  }
  return null;
}

/**
 * Every application ever sent, newest first.
 *
 * Injected opts (so this stays testable without state.js):
 *   getStatus(g)  — the group's current status
 *   reviews       — optional [{vacancy_id, ...}] private triage reviews
 *   statusPri     — STATUS_PRI, for the same dedupe the board uses
 *   now           — ms epoch, for the waiting arithmetic
 *
 * Deliberately NOT filtered by company approval, unlike the board. An
 * application is a fact about what he did; hiding one because its company was
 * never approved would make the funnel undercount itself. Non-job applications
 * (a course, career advising) live at companies nobody reviews.
 *
 * Deduped through the SAME dedupeTriageEntries reduction the board uses, so
 * the table and the board can never disagree about how many applications exist.
 */
export function selectApplicationRows(allGroups, opts) {
  const options = opts || {};
  const getStatus = options.getStatus || ((g) => g.status);
  // The live stage timestamp, once /api/statuses has merged one in — after a
  // drag on the board it is fresher than the baked snapshot's copy.
  const getStageSince =
    options.getStageSince || ((g) => g.status_updated_at || "");
  const now = options.now == null ? Date.now() : options.now;
  const reviewByVid = {};
  for (const r of options.reviews || []) reviewByVid[r.vacancy_id] = r;

  const entries = [];
  for (const g of allGroups || []) {
    const status = getStatus(g);
    if (!APPLICATION_STATUSES.includes(status)) continue;
    const entry = Object.assign({}, g);
    entry._status = status;
    entry._review = reviewFor(g, reviewByVid);
    entries.push(entry);
  }

  const deduped = dedupeTriageEntries(entries, options.statusPri || {});

  const rows = [];
  for (const entry of deduped.values()) {
    // "Sent on" prefers the recorded send date. status_updated_at is the
    // fallback and a lie of a different size on every row — it moves with each
    // stage — so rows using it are marked, and the cell says so on hover.
    const sentOn = entry.applied_at || "";
    const stageSince = getStageSince(entry) || entry.status_updated_at || "";
    const closed = CLOSED_STATUSES.has(entry._status);
    rows.push({
      id: entry.id,
      org: entry.company_name || entry.org || "",
      title: entry.title || "",
      kind: entry.kind || "job",
      status: entry._status,
      sentOn: sentOn || stageSince,
      sentOnIsFallback: !sentOn,
      stageSince: stageSince,
      // A closed application waits on nobody — the cell stays empty rather
      // than counting days since an answer already arrived.
      waitingDays: closed ? null : daysSince(stageSince, now),
      nextStep: nextStepFor(entry),
      url: sourceUrl(entry),
    });
  }

  // Newest first. A row with no date at all sorts last: an unknown date is not
  // "the beginning of time", and floating it to the top would push the rows
  // that DO have one out of view.
  rows.sort((a, b) => {
    if (!a.sentOn && !b.sentOn) return 0;
    if (!a.sentOn) return 1;
    if (!b.sentOn) return -1;
    return b.sentOn < a.sentOn ? -1 : b.sentOn > a.sentOn ? 1 : 0;
  });
  return rows;
}

/** The "what happens next" line: the field data_prep ships, else the private
 *  triage review's own next step or note. Free text the user typed — every
 *  caller escapes it. */
function nextStepFor(entry) {
  if (entry.next_step) return String(entry.next_step);
  const r = entry._review;
  if (r && r.next_step) return String(r.next_step);
  if (r && r.note) return String(r.note);
  return "";
}

/** The external posting, resolved the way the board's cards resolve it. */
function sourceUrl(entry) {
  const fromLoc = (entry.locations || []).find((l) => l && l.url);
  return safeUrl((fromLoc && fromLoc.url) || entry.org_url || "");
}

/**
 * The one-line count strip above the table.
 * `waiting` is 'applied' alone — sent, and the employer has not answered.
 * `inProgress` is test_task + interview — sent, and something is happening.
 * The two are separate because only one of them needs an evening.
 */
export function applicationCounts(rows) {
  const counts = {
    sent: 0,
    waiting: 0,
    inProgress: 0,
    accepted: 0,
    declined: 0,
  };
  for (const row of rows || []) {
    counts.sent += 1;
    if (WAITING_STATUSES.has(row.status)) counts.waiting += 1;
    if (IN_PROGRESS_STATUSES.has(row.status)) counts.inProgress += 1;
    if (row.status === "accepted") counts.accepted += 1;
    if (row.status === "declined") counts.declined += 1;
  }
  return counts;
}

/** The count strip as plain words: "11 sent · 4 waiting · 1 in progress · …".
 *  Zero counts are dropped — "0 accepted" is noise, not information — but
 *  "sent" always shows, so an empty search still says what it counted. */
export function countStripText(counts, t) {
  const translate = t || ((k, fb) => fb);
  const parts = [counts.sent + " " + translate("apps_count_sent", "sent")];
  const optional = [
    [counts.waiting, "apps_count_waiting", "waiting"],
    [counts.inProgress, "apps_count_in_progress", "in progress"],
    [counts.accepted, "apps_count_accepted", "accepted"],
    [counts.declined, "apps_count_declined", "declined"],
  ];
  for (const [n, key, fallback] of optional) {
    if (n > 0) parts.push(n + " " + translate(key, fallback));
  }
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// Markup
// ---------------------------------------------------------------------------

/** The board column that owns a status — its label and accent, so the table's
 *  stage dot and the board's column dot are the same colour by construction. */
function columnFor(status) {
  return TRIAGE_COLUMNS.find((c) => c.key === status) || null;
}

export function buildApplicationRow(row, opts) {
  const options = opts || {};
  const translate = options.t || ((k, fb) => fb);
  const locale = options.locale || "en-GB";
  const col = columnFor(row.status);
  // The stage name is translated through the SAME keys as the vacancy page's
  // status chip (vac_status_*), so one status has one name everywhere in the
  // product. TRIAGE_COLUMNS supplies the English fallback and the colour.
  const stageLabel = col
    ? translate("vac_status_" + row.status, col.label)
    : row.status;
  const stageColor = col ? col.color : "var(--muted)";
  const idAttr = jsAttr(row.id);

  const sentTitle = row.sentOnIsFallback
    ? translate(
        "apps_sent_estimated",
        "No send date recorded — showing when the stage last changed",
      )
    : "";
  const sentCell =
    '<td class="apps-cell apps-cell-date' +
    (row.sentOnIsFallback ? " apps-cell-date--fallback" : "") +
    '"' +
    (sentTitle ? ' title="' + escHtml(sentTitle) + '"' : "") +
    ">" +
    escHtml(formatDayMonth(row.sentOn, locale) || "—") +
    "</td>";

  const linkCell = row.url
    ? '<td class="apps-cell apps-cell-link"><a href="' +
      escHtml(row.url) +
      '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' +
      escHtml(translate("apps_open_source", "Open ↗")) +
      "</a></td>"
    : '<td class="apps-cell apps-cell-link">—</td>';

  // A long organisation, role or next step is clipped by the fixed column
  // width. The full text stays one hover away rather than being lost — the
  // same recovery the Browse rows give a truncated location.
  const full = (value) => (value ? ' title="' + escHtml(value) + '"' : "");

  return (
    '<tr class="apps-row" data-id="' +
    escHtml(row.id) +
    '" role="button" tabindex="0" onclick="openApplicationRow(\'' +
    idAttr +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openApplicationRow('" +
    idAttr +
    "')}\">" +
    sentCell +
    '<td class="apps-cell apps-cell-org"' +
    full(row.org) +
    ">" +
    escHtml(row.org) +
    "</td>" +
    '<td class="apps-cell apps-cell-what"' +
    full(row.title) +
    ">" +
    escHtml(row.title) +
    "</td>" +
    '<td class="apps-cell apps-cell-kind">' +
    escHtml(
      translate("apps_kind_" + row.kind, KIND_LABELS[row.kind] || row.kind),
    ) +
    "</td>" +
    '<td class="apps-cell apps-cell-stage">' +
    '<span class="apps-stage-dot" style="background:' +
    stageColor +
    '"></span>' +
    escHtml(stageLabel) +
    "</td>" +
    '<td class="apps-cell apps-cell-date">' +
    escHtml(formatDayMonth(row.stageSince, locale) || "—") +
    "</td>" +
    '<td class="apps-cell apps-cell-waiting">' +
    escHtml(formatWaiting(row.waitingDays, translate)) +
    "</td>" +
    '<td class="apps-cell apps-cell-next"' +
    full(row.nextStep) +
    ">" +
    escHtml(row.nextStep) +
    "</td>" +
    linkCell +
    "</tr>"
  );
}

/** The whole table, header included. Pure: takes rows, returns HTML. */
export function buildApplicationsTable(rows, opts) {
  const options = opts || {};
  const translate = options.t || ((k, fb) => fb);
  const headers = [
    ["apps_col_sent_on", "Sent on"],
    ["apps_col_organisation", "Organisation"],
    ["apps_col_what", "What"],
    ["apps_col_kind", "Kind"],
    ["apps_col_stage", "Stage"],
    ["apps_col_stage_since", "Stage since"],
    ["apps_col_waiting", "Waiting"],
    ["apps_col_next_step", "Next step"],
    ["apps_col_link", "Link"],
  ];
  const head = headers
    .map(
      ([key, fallback]) => "<th>" + escHtml(translate(key, fallback)) + "</th>",
    )
    .join("");

  return (
    '<div class="apps-table-scroll">' +
    '<table class="apps-table">' +
    "<thead><tr>" +
    head +
    "</tr></thead><tbody>" +
    rows.map((row) => buildApplicationRow(row, options)).join("") +
    "</tbody></table></div>"
  );
}

// ---------------------------------------------------------------------------
// DOM shell
// ---------------------------------------------------------------------------

// The ordered id queue for the currently-rendered rows — what a row click hands
// the vacancy route as its arrow-key queue (mirrors archive.js / catalog.js).
let _applicationsQueue = [];

export function renderApplicationsTable() {
  const host = document.getElementById("applicationsTable");
  if (!host) return;

  const rows = selectApplicationRows(groups, {
    getStatus: getGroupStatus,
    getStageSince: (g) => {
      const live = state.dbData[g.id];
      return (live && live.status_changed_at) || g.status_updated_at || "";
    },
    reviews: triageReviews || [],
    statusPri: STATUS_PRI,
  });
  _applicationsQueue = rows.map((r) => r.id);

  const counts = applicationCounts(rows);
  const strip =
    '<div class="apps-count-strip">' +
    escHtml(countStripText(counts, T)) +
    "</div>";

  if (!rows.length) {
    host.innerHTML =
      strip +
      '<div class="catalog-empty"><div class="catalog-empty-icon">📬</div>' +
      escHtml(
        T(
          "apps_empty",
          "Nothing sent yet. Move a role to Applied on the board, or add a non-job application with `vac add`.",
        ),
      ) +
      "</div>";
    return;
  }

  host.innerHTML =
    strip + buildApplicationsTable(rows, { t: T, locale: dateLocale() });
}

/** Open one application's vacancy page, handing the route the current row
 *  order so the arrow keys walk this table (mirrors archive.js). */
export function openApplicationRow(id) {
  if (typeof window !== "undefined" && window.openVacancyRoute) {
    window.openVacancyRoute(id, {
      context: "applications",
      queue: _applicationsQueue,
    });
  }
}

// ---------------------------------------------------------------------------
// Board | Table toggle
// ---------------------------------------------------------------------------

/** Render the two-button toggle above the board. `active` is the current view.
 *  Pure markup so the button copy and pressed state are testable. */
export function buildTriageToggle(active, t) {
  const translate = t || ((k, fb) => fb);
  const buttons = [
    ["board", "triage_view_board", "Board"],
    ["table", "triage_view_table", "Table"],
  ];
  return (
    '<div class="triage-view-toggle" role="group" aria-label="' +
    escHtml(translate("triage_view_label", "Triage view")) +
    '">' +
    buttons
      .map(
        ([view, key, fallback]) =>
          '<button type="button" class="triage-view-btn' +
          (view === active ? " is-active" : "") +
          '" data-triage-view="' +
          view +
          '" aria-pressed="' +
          (view === active ? "true" : "false") +
          '">' +
          escHtml(translate(key, fallback)) +
          "</button>",
      )
      .join("") +
    "</div>"
  );
}

if (typeof window !== "undefined") {
  window.openApplicationRow = openApplicationRow;
}
