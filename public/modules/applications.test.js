// applications.test.js — the Applications table: selection, sort, counts,
// waiting arithmetic, the remembered view, and the row markup's escaping.
//
// applications.js imports state.js, which reads window.VACANCY_DATA at import
// time, so a minimal browser shell goes up first (mirrors today.test.js).
// The timezone is forced far off UTC: "17 Aug" must be 17 Aug everywhere, and
// a date-formatting bug that only shows outside UTC is exactly the kind this
// file exists to catch.

import { test } from "node:test";
import assert from "node:assert/strict";

process.env.TZ = "Etc/GMT+12"; // UTC-12, as far behind UTC as IANA goes

globalThis.window = {
  VACANCY_DATA: {
    config: {},
    stats: {},
    vacancy_ids: [],
    groups: [],
    companies: [],
    triage_reviews: [],
    archived_groups: [],
  },
};
globalThis.location = { protocol: "file:", origin: "" };

const {
  APPLICATION_STATUSES,
  CLOSED_STATUSES,
  KIND_LABELS,
  TRIAGE_VIEW_KEY,
  selectApplicationRows,
  applicationCounts,
  countStripText,
  formatDayMonth,
  daysSince,
  formatWaiting,
  buildApplicationRow,
  buildApplicationsTable,
  buildTriageToggle,
  readTriageView,
  writeTriageView,
} = await import("./applications.js");

const { STATUS_PRI, TRIAGE_COLUMNS } = await import("./state.js");

const DAY = 86400000;
const NOW = Date.parse("2026-08-26T12:00:00Z");

/** One vacancy group, with only the fields the table reads. */
function group(over) {
  return Object.assign(
    {
      id: "v1",
      org: "Northwind Aid Trust",
      title: "Programme Manager",
      status: "applied",
      kind: "job",
      applied_at: "2026-08-17T09:00:00Z",
      status_updated_at: "2026-08-17T09:00:00Z",
      locations: [],
      member_ids: [],
    },
    over,
  );
}

const select = (groups, over) =>
  selectApplicationRows(groups, {
    getStatus: (g) => g.status,
    statusPri: STATUS_PRI,
    now: NOW,
    ...over,
  });

// ---------------------------------------------------------------------------
// Which rows the table shows
// ---------------------------------------------------------------------------

test("the table lists every application status and nothing else", () => {
  // The point of the view: one row per application ever sent. A liked or
  // to_apply role is not an application — counting it would inflate the
  // funnel with things he never sent.
  const groups = [
    ...APPLICATION_STATUSES.map((s, i) =>
      group({ id: `sent-${s}`, title: `Role ${i}`, status: s }),
    ),
    group({ id: "liked", title: "Not sent", status: "liked" }),
    group({ id: "queued", title: "Queued", status: "to_apply" }),
    group({ id: "unseen", title: "Untouched", status: "unseen" }),
  ];
  const ids = select(groups).map((r) => r.id);
  assert.deepEqual(
    ids.slice().sort(),
    APPLICATION_STATUSES.map((s) => `sent-${s}`).sort(),
  );
});

test("APPLICATION_STATUSES matches the board's own funnel columns", () => {
  // Drift guard: a status that has a board column but is missing here is an
  // application the table silently omits.
  const columns = TRIAGE_COLUMNS.filter((c) => !c.derived).map((c) => c.key);
  for (const status of APPLICATION_STATUSES) {
    assert.ok(columns.includes(status), `no board column for ${status}`);
  }
});

test("an application at an unapproved company still appears", () => {
  // Unlike the board, the table is not filtered by company approval. A course
  // or a career-advising session lives at a company nobody ever reviewed, and
  // hiding it would make the funnel undercount what he actually sent.
  const rows = select([group({ company_id: "never-reviewed" })]);
  assert.equal(rows.length, 1);
});

test("the same role listed twice collapses to one application", () => {
  // Deduped through the board's own reduction (helpers.dedupeTriageEntries),
  // so the table and the board can never disagree about how many applications
  // exist. The survivor is whichever copy STATUS_PRI ranks highest — the same
  // tie-break the board applies, deliberately not a second rule of our own.
  const rows = select([
    group({ id: "a", status: "applied" }),
    group({ id: "b", status: "interview" }),
  ]);
  assert.equal(rows.length, 1);
  const winner = STATUS_PRI.applied < STATUS_PRI.interview
    ? "applied"
    : "interview";
  assert.equal(rows[0].status, winner);
});

// ---------------------------------------------------------------------------
// Sort
// ---------------------------------------------------------------------------

test("rows sort by send date, newest first", () => {
  const rows = select([
    group({ id: "may", title: "May role", applied_at: "2026-05-04T10:00:00Z" }),
    group({ id: "aug", title: "Aug role", applied_at: "2026-08-17T10:00:00Z" }),
    group({ id: "jun", title: "Jun role", applied_at: "2026-06-21T10:00:00Z" }),
  ]);
  assert.deepEqual(
    rows.map((r) => r.id),
    ["aug", "jun", "may"],
  );
});

test("a row with no date at all sorts last, not first", () => {
  // An unknown date is not "the beginning of time". Sorting it as one would
  // push every row that DOES have a date out of the first screen.
  const rows = select([
    group({
      id: "undated",
      title: "Undated",
      applied_at: null,
      status_updated_at: null,
    }),
    group({ id: "dated", title: "Dated", applied_at: "2026-05-04T10:00:00Z" }),
  ]);
  assert.deepEqual(
    rows.map((r) => r.id),
    ["dated", "undated"],
  );
});

test("sent-on falls back to the stage date, and says that it did", () => {
  // Rows written before applied_at existed have no send date. The fallback is
  // shown but marked, so an estimate never passes for a record.
  const rows = select([
    group({ id: "old", applied_at: null, status_updated_at: "2026-05-04T10:00:00Z" }),
  ]);
  assert.equal(rows[0].sentOn, "2026-05-04T10:00:00Z");
  assert.equal(rows[0].sentOnIsFallback, true);

  const fresh = select([group({ id: "new" })]);
  assert.equal(fresh[0].sentOnIsFallback, false);
});

// ---------------------------------------------------------------------------
// Waiting
// ---------------------------------------------------------------------------

test("waiting counts whole days since the stage last changed", () => {
  const rows = select([
    group({ status_updated_at: new Date(NOW - 9 * DAY).toISOString() }),
  ]);
  assert.equal(rows[0].waitingDays, 9);
  assert.equal(formatWaiting(rows[0].waitingDays), "9 d");
});

test("a closed application waits on nobody", () => {
  // A rejection from March is not "168 days late" — nobody owes an answer.
  for (const status of CLOSED_STATUSES) {
    const rows = select([
      group({ status, status_updated_at: new Date(NOW - 168 * DAY).toISOString() }),
    ]);
    assert.equal(rows[0].waitingDays, null, `${status} still shows a wait`);
    assert.equal(formatWaiting(rows[0].waitingDays), "");
  }
});

test("daysSince never returns a negative day count", () => {
  // A future-dated row (clock skew, a hand-entered date) must read as 0, not
  // "-3 d" — a negative wait is nonsense on screen.
  assert.equal(daysSince(new Date(NOW + 3 * DAY).toISOString(), NOW), 0);
  assert.equal(daysSince(new Date(NOW).toISOString(), NOW), 0);
  assert.equal(daysSince(null, NOW), null);
  assert.equal(daysSince("not-a-date", NOW), null);
});

// ---------------------------------------------------------------------------
// The count strip
// ---------------------------------------------------------------------------

test("the count strip separates waiting from in progress", () => {
  // Two different situations: "sent, no answer" needs nothing from him,
  // "test task / interview" needs an evening. One number for both would hide
  // the only part of the funnel that is actually work.
  const counts = applicationCounts([
    { status: "applied" },
    { status: "applied" },
    { status: "applied" },
    { status: "applied" },
    { status: "test_task" },
    { status: "accepted" },
    { status: "declined" },
  ]);
  assert.deepEqual(counts, {
    sent: 7,
    waiting: 4,
    inProgress: 1,
    accepted: 1,
    declined: 1,
  });
});

test("in progress counts test tasks and interviews together", () => {
  const counts = applicationCounts([
    { status: "test_task" },
    { status: "interview" },
  ]);
  assert.equal(counts.inProgress, 2);
  assert.equal(counts.waiting, 0);
});

test("the count strip reads as a sentence of plain words", () => {
  const text = countStripText({
    sent: 11,
    waiting: 4,
    inProgress: 1,
    accepted: 1,
    declined: 6,
  });
  assert.equal(
    text,
    "11 sent · 4 waiting · 1 in progress · 1 accepted · 6 declined",
  );
});

test("a zero count is dropped, but the total always shows", () => {
  // "0 accepted" is noise. "0 sent" is the answer to the question.
  assert.equal(
    countStripText({
      sent: 2,
      waiting: 2,
      inProgress: 0,
      accepted: 0,
      declined: 0,
    }),
    "2 sent · 2 waiting",
  );
  assert.equal(
    countStripText({
      sent: 0,
      waiting: 0,
      inProgress: 0,
      accepted: 0,
      declined: 0,
    }),
    "0 sent",
  );
});

test("the strip counts exactly what the table renders", () => {
  const groups = APPLICATION_STATUSES.map((s, i) =>
    group({ id: `g-${s}`, title: `Role ${i}`, status: s }),
  );
  const rows = select(groups);
  assert.equal(applicationCounts(rows).sent, rows.length);
});

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

test("a date renders as day + short month, whatever the local timezone", () => {
  // The process runs at UTC-12. A timestamp just after midnight UTC must still
  // read as its UTC day, or half the table shifts back by one.
  assert.equal(formatDayMonth("2026-08-17T09:00:00Z", "en-GB"), "17 Aug");
  assert.equal(formatDayMonth("2026-08-25T00:30:00Z", "en-GB"), "25 Aug");
  assert.equal(formatDayMonth("2026-05-04", "en-GB"), "4 May");
});

test("the day comes first even under en-US, which the app actually uses", () => {
  // The regression this guards: handing the whole date to toLocaleDateString
  // renders "Aug 17" under en-US — the app's own locale — so the column the
  // eye scans stopped starting with the number it scans for.
  assert.equal(formatDayMonth("2026-08-17T09:00:00Z", "en-US"), "17 Aug");
  assert.equal(formatDayMonth("2026-05-04T09:00:00Z", "en-US"), "4 May");
});

test("a missing or unparseable date renders as nothing, never 'Invalid Date'", () => {
  assert.equal(formatDayMonth(null, "en-GB"), "");
  assert.equal(formatDayMonth("", "en-GB"), "");
  assert.equal(formatDayMonth("not-a-date", "en-GB"), "");
});

test("every kind has a plain-word label", () => {
  // No category codes on screen: the table is read, not parsed.
  for (const kind of [
    "job",
    "programme",
    "advising",
    "consulting",
    "grant",
    "course",
  ]) {
    assert.ok(KIND_LABELS[kind], `no label for kind ${kind}`);
    assert.ok(/^[A-Z]/.test(KIND_LABELS[kind]));
  }
});

// ---------------------------------------------------------------------------
// Markup
// ---------------------------------------------------------------------------

test("a row carries the stage label and the board column's own colour", () => {
  // One source for the accent: the table's dot and the board's column dot are
  // the same colour by construction, not by a second hand-kept list.
  const rows = select([group({ status: "test_task" })]);
  const html = buildApplicationRow(rows[0], { locale: "en-GB" });
  const col = TRIAGE_COLUMNS.find((c) => c.key === "test_task");
  assert.ok(html.includes(col.label));
  assert.ok(html.includes("background:" + col.color));
});

test("a row is clickable and operable from the keyboard", () => {
  // Same contract as a Browse or Archive row: mouse and keyboard reach the
  // same vacancy page.
  const rows = select([group({})]);
  const html = buildApplicationRow(rows[0], { locale: "en-GB" });
  assert.ok(html.includes('role="button"'));
  assert.ok(html.includes('tabindex="0"'));
  assert.ok(html.includes("openApplicationRow('v1')"));
  assert.ok(html.includes("event.key==='Enter'"));
});

test("free text in a row is escaped, not executed", () => {
  // Next step and title are text the user types. A note like
  // "<img onerror=...>" must render inert.
  const rows = select([
    group({
      title: '<img src=x onerror="alert(1)">',
      next_step: '<script>alert(2)</script>',
      org: '"><b>oops</b>',
    }),
  ]);
  const html = buildApplicationRow(rows[0], { locale: "en-GB" });
  assert.ok(!html.includes("<img src=x"));
  assert.ok(!html.includes("<script>"));
  assert.ok(!html.includes("<b>oops"));
  assert.ok(html.includes("&lt;img"));
});

test("the source link opens outward without also opening the row", () => {
  const rows = select([
    group({ locations: [{ url: "https://example.org/job/1" }] }),
  ]);
  const html = buildApplicationRow(rows[0], { locale: "en-GB" });
  assert.ok(html.includes('href="https://example.org/job/1"'));
  assert.ok(html.includes('rel="noopener"'));
  assert.ok(html.includes("event.stopPropagation()"));
});

test("the table header names all nine columns in order", () => {
  const html = buildApplicationsTable(select([group({})]), { locale: "en-GB" });
  const headers = [...html.matchAll(/<th>([^<]*)<\/th>/g)].map((m) => m[1]);
  assert.deepEqual(headers, [
    "Sent on",
    "Organisation",
    "What",
    "Kind",
    "Stage",
    "Stage since",
    "Waiting",
    "Next step",
    "Link",
  ]);
});

test("wide content scrolls inside the table's own container", () => {
  // Nine columns are wider than a phone. The table scrolls, the page does not.
  const html = buildApplicationsTable([], {});
  assert.ok(html.includes('class="apps-table-scroll"'));
});

// ---------------------------------------------------------------------------
// The remembered view
// ---------------------------------------------------------------------------

function fakeStore(initial) {
  const data = Object.assign({}, initial);
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = String(v);
    },
  };
}

test("the view defaults to the board", () => {
  assert.equal(readTriageView(fakeStore()), "board");
});

test("the chosen view is remembered", () => {
  const store = fakeStore();
  assert.equal(writeTriageView(store, "table"), "table");
  assert.equal(store.data[TRIAGE_VIEW_KEY], "table");
  assert.equal(readTriageView(store), "table");
});

test("a junk or absent stored value falls back to the board", () => {
  // A hand-edited or stale key must not leave the tab rendering nothing.
  assert.equal(readTriageView(fakeStore({ [TRIAGE_VIEW_KEY]: "kanban" })), "board");
  assert.equal(readTriageView(null), "board");
  assert.equal(writeTriageView(fakeStore(), "kanban"), "board");
});

test("a storage that throws still leaves a usable tab", () => {
  // Private-mode Safari throws on getItem. The view degrades to the default
  // instead of taking the whole section down with it.
  const throwing = {
    getItem() {
      throw new Error("denied");
    },
    setItem() {
      throw new Error("denied");
    },
  };
  assert.equal(readTriageView(throwing), "board");
  // The write fails, but the return value still drives this session.
  assert.equal(writeTriageView(throwing, "table"), "table");
});

test("the toggle marks exactly one button as pressed", () => {
  const html = buildTriageToggle("table");
  assert.ok(html.includes(">Board<"));
  assert.ok(html.includes(">Table<"));
  assert.equal((html.match(/aria-pressed="true"/g) || []).length, 1);
  assert.match(html, /data-triage-view="table"[^>]*aria-pressed="true"/);
});

test("clipped columns keep their full text one hover away", () => {
  // Organisation / What / Next step are cut by a fixed column width. The full
  // string must stay recoverable, and it must be escaped in the title too —
  // an attribute is as good an injection point as a text node.
  const rows = select([
    group({
      org: 'Centre for "Effective" Altruism',
      title: "A very long role title that will not fit the column",
      next_step: "Submit the exercise by 1 September",
    }),
  ]);
  const html = buildApplicationRow(rows[0], { locale: "en-GB" });
  assert.ok(html.includes('title="A very long role title that will not fit the column"'));
  assert.ok(html.includes('title="Submit the exercise by 1 September"'));
  assert.ok(html.includes("&quot;Effective&quot;"));
});

test("an empty cell gets no empty tooltip", () => {
  const rows = select([group({ next_step: "" })]);
  const html = buildApplicationRow(rows[0], { locale: "en-GB" });
  assert.ok(!html.includes('title=""'));
});

// --- The stage timestamp the table rests on --------------------------------
//
// "Stage since" and "Waiting" both read status_updated_at. data_prep did not
// ship it, so both columns were empty for every row on the live dashboard
// while every test here passed — the fixtures set the field by hand. These
// tests pin the reading side; tests/test_vacancy_applied_at.py pins the
// shipping side.

test("a live stage timestamp beats the baked one", () => {
  // After a drag on the board, /api/statuses has a fresher timestamp than the
  // snapshot the page loaded with.
  const rows = select([group({ status_updated_at: "2026-08-01T09:00:00Z" })], {
    getStageSince: () => "2026-08-25T09:00:00Z",
  });
  assert.equal(rows[0].stageSince, "2026-08-25T09:00:00Z");
});

test("with no live timestamp the baked one is still used", () => {
  const rows = select([group({ status_updated_at: "2026-08-01T09:00:00Z" })], {
    getStageSince: () => "",
  });
  assert.equal(rows[0].stageSince, "2026-08-01T09:00:00Z");
});

test("a row with no stage timestamp at all shows no wait, not a wrong one", () => {
  // The failure mode this guards: a missing date read as epoch zero, giving
  // "20693 d" in the Waiting column.
  const rows = select([
    group({ status_updated_at: null, applied_at: "2026-08-17T09:00:00Z" }),
  ]);
  assert.equal(rows[0].waitingDays, null);
  assert.equal(formatWaiting(rows[0].waitingDays), "");
});

test("a date stored at UTC midnight renders as the day that was typed", () => {
  // `vac add --applied-at 2026-08-10` stores 2026-08-10T00:00:00+00:00. The
  // formatter pins to UTC, so this must read "10 Aug" and not "9 Aug".
  assert.equal(formatDayMonth("2026-08-10T00:00:00+00:00", "en-GB"), "10 Aug");
});
