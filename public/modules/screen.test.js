// screen.js — the Screen view (bulk screening inbox). The write path takes an
// injected `io`, the row/footer builders return strings, and the selection
// state is module-local, so all twelve U4 scenarios run without a DOM.
//
// screen.js imports state.js (reads window.VACANCY_DATA at import) and
// api.js (touches document in initApi only), so a minimal shell goes up first.

import { test } from "node:test";
import assert from "node:assert/strict";

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
globalThis.location = { protocol: "https:", origin: "https://dashboard.test" };

const {
  view,
  setList,
  setGroup,
  toggleSelected,
  toggleSelectAll,
  screenModel,
  bulkSet,
  undoLast,
  fill,
  screenRowHtml,
  screenListHtml,
  screenFooterHtml,
} = await import("./screen.js");

const t = (k, fb) => fb;

test("real screening IO sends versions and keeps the saved version for Undo", async () => {
  const { state, groupsById } = await import("./state.js");
  const { liveIo } = await import("./screen.js");
  groupsById.set("live", { id: "live", member_ids: [] });
  state.dbData.live = { status: "unseen", revision: "100" };
  const requests = [];
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    requests.push({ url, ...JSON.parse(options.body) });
    const c = requests.at(-1).changes[0];
    return { ok: true, json: async () => ({ rows: [{ id: "live", status: c.status,
      previous: c.expected_status, revision: requests.length === 1 ? "101" : "103" }] }) };
  };
  try {
    await bulkSet(["live"], "liked", liveIo);
    assert.equal(state.dbData.live.status, "liked");
    assert.equal(state.dbData.live.revision, "101");
    // Another device changed away and back. Undo must still send *our* version.
    state.dbData.live.revision = "102";
    await undoLast(liveIo);
    assert.match(requests[0].url, /\/api\/screening-decision$/);
    assert.equal(requests[0].changes[0].expected_revision, "100");
    assert.equal(requests[1].changes[0].expected_revision, "101");
  } finally { globalThis.fetch = oldFetch; groupsById.delete("live"); delete state.dbData.live; }
});

// --- A fake write path: a status map, member ids, and a save that can fail --

function fakeIo(db, members, failFor) {
  const saved = [];
  return {
    saved,
    members: (id) => members[id] || [id],
    current: (mid) => db[mid],
    write: async (ids, targetOf) => {
      if (ids.some((id) => failFor?.has(id))) return null;
      const previous = {};
      for (const id of ids) {
        previous[id] = db[id];
        db[id] = targetOf(id);
        saved.push([id, targetOf(id)]);
      }
      return previous;
    },
  };
}

const facts = (extra) => ({
  screening_state: "ready",
  screening: {
    posting_facts: Object.assign({ requirements: [] }, extra || {}),
    profile_comparison: [],
    unknowns: [],
  },
});

const lang = (id) =>
  Object.assign(
    { id, title: id, org: "Org" },
    facts({
      requirements: [
        {
          kind: "language",
          value: "Spanish",
          strength: "required",
          quote: "Spanish required.",
        },
      ],
    }),
  );

const ROLES = [
  lang("a"),
  lang("b"),
  lang("c"),
  Object.assign({ id: "d" }, facts()),
];

test("select all in group language, deselect one, Keep: only the rest become liked", async () => {
  const db = { a: "unseen", b: "unseen", c: "unseen", d: "unseen" };
  setList("toScreen");
  setGroup("language");
  const model = screenModel(ROLES, (g) => db[g.id]);
  assert.deepEqual(model.visibleIds, ["a", "b", "c"]);
  toggleSelectAll(model.visibleIds);
  toggleSelected("b");
  const ids = model.visibleIds.filter((id) => view.selected.has(id));
  const r = await bulkSet(ids, "liked", fakeIo(db, {}));
  assert.deepEqual(db, { a: "liked", b: "unseen", c: "liked", d: "unseen" });
  assert.equal(r.saved, 2);
  assert.deepEqual(
    r.op.rows.map((row) => row.id),
    ["a", "c"],
  );
});

test("Undo after a bulk action restores each saved row's previous status and leaves the exception alone", async () => {
  const db = { a: "liked", b: "unseen", c: "liked" };
  await undoLast(fakeIo(db, {}));
  assert.deepEqual(db, { a: "unseen", b: "unseen", c: "unseen" });
  assert.equal(await undoLast(fakeIo(db, {})), null);
});

test("a save that fails for one of three rows: that row reverts, the record holds two rows, message reads 2 of 3", async () => {
  const db = { a: "unseen", b: "unseen", c: "unseen" };
  const io = fakeIo(db, {}, new Set(["b"]));
  const r = await bulkSet(["a", "b", "c"], "passed", io);
  assert.deepEqual(db, { a: "passed", b: "unseen", c: "passed" });
  assert.equal(r.saved, 2);
  assert.equal(r.total, 3);
  assert.deepEqual(
    r.op.rows.map((row) => row.id),
    ["a", "c"],
  );
  assert.equal(
    fill("{n} of {m} saved", { n: r.saved, m: r.total }),
    "2 of 3 saved",
  );
  await undoLast(io);
});

test("member rows write and revert with the canonical row; previous is recorded per member", async () => {
  const db = { a: "unseen", a2: "to_research" };
  const io = fakeIo(db, { a: ["a", "a2"] });
  const r = await bulkSet(["a"], "liked", io);
  assert.deepEqual(db, { a: "liked", a2: "liked" });
  assert.deepEqual(r.op.rows[0], {
    id: "a",
    member_ids: ["a", "a2"],
    previous: { a: "unseen", a2: "to_research" },
  });
  await undoLast(io);
  assert.deepEqual(db, { a: "unseen", a2: "to_research" });
});

test("a partially failed row reverts the members that had saved", async () => {
  const db = { a: "unseen", a2: "unseen" };
  const io = fakeIo(db, { a: ["a", "a2"] }, new Set(["a2"]));
  const r = await bulkSet(["a"], "liked", io);
  assert.equal(r.saved, 0);
  assert.equal(r.op, null);
  assert.deepEqual(db, { a: "unseen", a2: "unseen" });
  // The server transaction leaves the entire canonical role unchanged.
  assert.deepEqual(io.saved, []);
});

test("two bulk actions then one Undo: only the second is reverted", async () => {
  const db = { a: "unseen", b: "unseen" };
  const io = fakeIo(db, {});
  await bulkSet(["a"], "liked", io);
  await bulkSet(["b"], "passed", io);
  await undoLast(io);
  assert.deepEqual(db, { a: "liked", b: "unseen" });
  await undoLast(io);
  assert.deepEqual(db, { a: "unseen", b: "unseen" });
  assert.equal(await undoLast(io), null);
});

test("switching group or list clears the selection", () => {
  setList("toScreen");
  setGroup("all");
  toggleSelected("a");
  assert.equal(view.selected.size, 1);
  setGroup("language");
  assert.equal(view.selected.size, 0);
  toggleSelected("a");
  setList("kept");
  assert.equal(view.selected.size, 0);
  setList("toScreen");
});

test("Keep and Put aside are disabled before statuses load and enabled after", () => {
  const base = {
    t,
    selected: 2,
    visible: 3,
    list: "toScreen",
    busy: false,
    canUndo: false,
  };
  const before = screenFooterHtml(Object.assign({ loaded: false }, base));
  assert.match(before, /id="scrKeep" disabled/);
  assert.match(before, /id="scrAside" disabled/);
  assert.match(before, /Loading statuses/);
  const after = screenFooterHtml(Object.assign({ loaded: true }, base));
  assert.match(after, /id="scrKeep">/);
  assert.match(after, /id="scrAside">/);
  assert.match(after, /2 selected/);
});

test("required, preferred and unknown strengths render distinct badge classes with labels; two requirements give two badges", () => {
  const g = Object.assign(
    { id: "r", title: "Role", org: "Org" },
    facts({
      requirements: [
        {
          kind: "language",
          value: "Spanish",
          strength: "required",
          quote: "q1",
        },
        { kind: "skill", value: "SQL", strength: "preferred", quote: "q2" },
      ],
    }),
  );
  const html = screenRowHtml(g, { t });
  assert.equal((html.match(/class="scr-badge /g) || []).length, 2);
  assert.match(html, /scr-badge--required">Required · Spanish/);
  assert.match(html, /scr-badge--preferred">Preferred · SQL/);
  const unknown = screenRowHtml(
    Object.assign(
      { id: "u" },
      facts({ requirements: [{ kind: "other", value: "x", strength: "" }] }),
    ),
    { t },
  );
  assert.match(unknown, /scr-badge--unknown">Unknown · x/);
});

test("a requirement without a quote renders the words no quote", () => {
  const g = Object.assign(
    { id: "n", title: "Role" },
    facts({
      requirements: [
        { kind: "skill", value: "Excel", strength: "required", quote: "" },
      ],
    }),
  );
  assert.match(screenRowHtml(g, { t }), /scr-noquote">no quote</);
  // Zero requirements at all: still "no quote".
  assert.match(
    screenRowHtml(Object.assign({ id: "z" }, facts()), { t }),
    /no quote/,
  );
});

test("the row head is a 44px checkbox target with the title, org, location, fact line", () => {
  const g = Object.assign(
    {
      id: "h",
      title: "Ops <Lead>",
      company_name: "Org & Co",
      locations: [{ location: "Berlin" }],
    },
    facts({ duties: "Run the office. Then more." }),
  );
  const html = screenRowHtml(g, { t, checked: true });
  assert.match(
    html,
    /scr-row-head" role="checkbox" tabindex="0" aria-checked="true"/,
  );
  assert.match(html, /Ops &lt;Lead&gt;/);
  assert.match(html, /Org &amp; Co <span class="scr-meta scr-meta--location">Berlin<\/span>/);
  assert.match(html, /scr-row-fact">Run the office\./);
});

test("an empty list renders No roles left in this list.", () => {
  assert.match(screenListHtml([], { t }), /No roles left in this list\./);
});

test("individual rows offer Like and Pass outside the selection checkbox", async () => {
  const row = { id: "one", title: "Role", ...facts() };
  const html = screenRowHtml(row);
  assert.match(html, /<\/div><\/div><div class="scr-row-actions">/);
  assert.match(html, /data-decision="liked" data-vacancy="one">Like<\/button>/);
  assert.match(html, /data-decision="passed" data-vacancy="one">Pass<\/button>/);
  assert.match(screenRowHtml(row, { disabled: true }), /data-vacancy="one" disabled/);

  const db = { one: "unseen", other: "unseen" };
  const io = fakeIo(db, {});
  view.selected = new Set(["other"]);
  await bulkSet(["one"], "passed", io);
  assert.equal(db.one, "passed");
  assert.equal(db.other, "unseen");
  await undoLast(io);
  assert.equal(db.one, "unseen");
  view.selected.clear();
});

test("undo leaves a decision made after the bulk action untouched", async () => {
  const db = { a: "unseen", b: "unseen" };
  const io = fakeIo(db, {});
  await bulkSet(["a", "b"], "liked", io);
  db.a = "applied"; // the user moved on before pressing Undo
  const r = await undoLast(io);
  assert.equal(db.a, "applied");
  assert.equal(db.b, "unseen");
  assert.equal(r.restored, 1);
  assert.equal(r.total, 2);
});

test("a failing member leaves the whole role unchanged", async () => {
  const db = { a: "unseen", a2: "unseen" };
  const io = fakeIo(db, { a: ["a", "a2"] }, new Set(["a2"]));
  const r = await bulkSet(["a"], "liked", io);
  assert.equal(r.saved, 0);
  assert.deepEqual(db, { a: "unseen", a2: "unseen" });
  assert.deepEqual(io.saved, []);
});

const { setFilter, setPage, PAGE_SIZE } = await import("./screen.js");

test("20-row pages bound selection, navigation clears it and filters apply to kept too", () => {
  view.filters = {};
  setGroup("all");
  setList("toScreen");
  const roles = Array.from({ length: 45 }, (_, n) => lang(String(n)));
  let model = screenModel(roles, () => "unseen");
  assert.equal(model.matchingIds.length, 45);
  assert.equal(model.visibleIds.length, PAGE_SIZE);
  assert.equal(model.pages, 3);
  toggleSelectAll(model.visibleIds);
  assert.equal(view.selected.size, 20);
  setPage(2);
  assert.equal(view.selected.size, 0);
  model = screenModel(roles, () => "unseen");
  assert.equal(model.visibleIds.length, 5);
  toggleSelectAll(model.visibleIds);
  setFilter("requirementText", "German");
  assert.equal(view.selected.size, 0);
  assert.equal(view.page, 0);
  setList("kept");
  model = screenModel(roles, () => "liked");
  assert.equal(model.lists.kept.size, 45);
  assert.equal(model.matchingIds.length, 0);
  setFilter("requirementText", "Spanish");
  assert.equal(screenModel(roles, () => "liked").matchingIds.length, 45);
  setList("toScreen");
  view.filters = {};
});

test("remote status changes prune selected rows that disappear from the visible batch", () => {
  const db = { a: "unseen", b: "unseen" };
  setGroup("all");
  setList("toScreen");
  toggleSelectAll(["a", "b"]);
  db.a = "applied";
  screenModel(ROLES.slice(0, 2), (g) => db[g.id]);
  assert.deepEqual([...view.selected], ["b"]);
  view.selected.clear();
});

test("compact row keeps complete escaped evidence behind disclosure and only two matching badges outside", () => {
  const g = lang("safe");
  g.screening.posting_facts.requirements = Array.from(
    { length: 12 },
    (_, n) => ({
      kind: "language",
      value: `German ${n}`,
      strength: "required",
      quote: "<script>evidence</script>",
    }),
  );
  g.screening.work_profile = {
    activities: [{ kind: "building", quote: '<img src=x onerror="alert(1)">' }],
  };
  const html = screenRowHtml(g, { filters: { kind: "language" } });
  const [head, evidence] = html.split("<details");
  assert.equal((head.match(/scr-badge--required/g) || []).length, 2);
  assert.equal((evidence.match(/scr-badge--required/g) || []).length, 12);
  assert.match(evidence, /&lt;script&gt;/);
  assert.match(evidence, /&lt;img/);
  assert.doesNotMatch(html, /<script>|<img/);
  assert.equal(
    (
      screenRowHtml(g)
        .split("<details")[0]
        .match(/scr-badge--required/g) || []
    ).length,
    0,
  );
});

test("first-seen is labelled separately from expired deadline on compact rows", () => {
  const g = {
    ...lang("dates"),
    first_seen: "2026-09-05",
    deadline: "2026-09-06",
  };
  const head = screenRowHtml(g, { today: "2026-09-07" }).split("<details")[0];
  assert.match(head, /First seen: 2026-09-05/);
  assert.match(head, /Deadline passed: 2026-09-06/);
  const current = screenRowHtml(
    { ...g, deadline: "2026-09-07" },
    { today: "2026-09-07" },
  );
  assert.doesNotMatch(current, /Deadline passed:/);
  view.filters = {};
  setFilter("deadline", "expired");
  assert.deepEqual(screenModel([g], () => "unseen", "2026-09-07").visibleIds, [
    "dates",
  ]);
  setFilter("age", "older30");
  assert.equal(
    screenModel([g], () => "unseen", "2026-09-07").matchingIds.length,
    0,
  );
  view.filters = {};
});

test("compact row metadata uses validated dates, escapes source, and shows unknowns", () => {
  const html = screenRowHtml(
    {
      id: "meta",
      title: "Role",
      source_board: "Board <A>",
      first_seen: "2026-09-01T10:00:00Z",
      last_seen: "2026-09-07",
      deadline: "2026-09-20",
      screening: { posting_facts: {} },
    },
    { t, compact: true, today: "2026-09-08" },
  );
  assert.match(html, /First seen: 2026-09-01/);
  assert.match(html, /Last seen: 2026-09-07/);
  assert.match(html, /Source: Board &lt;A&gt;/);
  assert.match(html, /Deadline: 2026-09-20/);
  assert.doesNotMatch(html, /First seen: undefined|Last seen: undefined/);
  const missing = screenRowHtml({ id: "missing", title: "Role" }, { t, compact: true });
  assert.match(missing, /Source: unknown/);
  assert.doesNotMatch(missing, /First seen:|Last seen:|Deadline:/);
});

test("technical specialist evidence has a technical label, distinct from specialist activity", () => {
  const g = lang("tech");
  g.screening.work_profile = {
    activities: [],
    technical_depth: {
      level: "specialist",
      quote: "Own production architecture",
    },
  };
  assert.match(
    screenRowHtml(g),
    /Specialist technical expertise<blockquote>Own production architecture/,
  );
});

const { reviewModel, REVIEW_SIZE, feedbackFor } = await import("./screen.js");
test("functional review paginates roles and only selection from the current batch survives", () => {
  view.batch = "product";
  view.list = "toScreen";
  view.page = 0;
  view.selected.clear();
  const roles = Array.from({ length: 7 }, (_, i) => ({
    ...lang("p" + i),
    title: "Product Manager " + i,
  }));
  roles.push({ ...lang("ops"), title: "Head of Operations" });
  let m = reviewModel(roles, () => "unseen");
  assert.equal(m.rows.length, 7);
  toggleSelectAll(m.visibleIds);
  view.batch = "operations";
  view.page = 0;
  m = reviewModel(roles, () => "unseen");
  assert.deepEqual(m.visibleIds, ["ops"]);
  assert.equal(view.selected.size, 0);
  m = reviewModel(roles, (g) => (g.id === "ops" ? "declined" : "unseen"));
  assert(!m.visibleIds.includes("ops"));
  assert.equal(m.batch, undefined);
});
test("feedback refers only to successfully saved members and never creates a preference", () => {
  const op = { status: "passed", rows: [{ id: "a", member_ids: ["a", "a2"] }] };
  assert.deepEqual(
    feedbackFor(op, "  Location does not work  ", "Product", "note-id"),
    {
      id: "note-id",
      vacancy_ids: ["a", "a2"],
      decision: "passed",
      reason: "Location does not work",
      group_label: "Product",
    },
  );
  assert.equal(feedbackFor(null, "reason", "Product", "id"), null);
  assert.equal(feedbackFor(op, "   ", "Product", "id"), null);
});
test("kept and put-aside pages remain navigable after the screening inbox is empty", () => {
  const roles = Array.from({ length: REVIEW_SIZE + 3 }, (_, i) => ({
    ...lang("k" + i),
    title: "Product Manager " + i,
  }));
  for (const [list, status] of [
    ["kept", "liked"],
    ["putAside", "passed"],
  ]) {
    view.list = list;
    view.batch = null;
    view.page = 1;
    view.selected.clear();
    const m = reviewModel(roles, () => status);
    assert.equal(view.page, 1);
    assert.equal(m.rows.length, 3);
    assert.equal(m.visibleIds.length, 3);
  }
  view.list = "toScreen";
  view.page = 0;
});


test("lost responses survive reload and replay the same receipt with recoverable Undo", async () => {
  const { state, groupsById } = await import("./state.js");
  const oldFetch = globalThis.fetch;
  const oldStorage = globalThis.localStorage;
  const storage = new Map();
  globalThis.localStorage = { getItem: (k) => storage.get(k), setItem: (k, v) => storage.set(k, v) };
  groupsById.set("retry", { id: "retry", member_ids: [] });
  state.dbData.retry = { status: "unseen", revision: "200" };
  const requests = [];
  try {
    const first = await import("./screen.js?receipt-first");
    globalThis.fetch = async (_, opts) => {
      requests.push(JSON.parse(opts.body));
      throw new Error("response lost after COMMIT");
    };
    assert.equal((await first.bulkSet(["retry"], "liked")).saved, 0);
    assert.equal(requests[0].operation_id, requests[1].operation_id);
    assert.ok(JSON.parse(storage.get("screen-decisions")).pending);
    // The next page load has already fetched the committed status.
    state.dbData.retry = { status: "liked", revision: "201" };
    const reloaded = await import("./screen.js?receipt-reloaded");
    globalThis.fetch = async (_, opts) => {
      const request = JSON.parse(opts.body);
      requests.push(request);
      return { ok: true, json: async () => ({ rows: [{ id: "retry", status: request.changes[0].status,
        previous: "unseen", revision: "201" }] }) };
    };
    assert.equal((await reloaded.bulkSet(["retry"], "liked")).saved, 1);
    assert.deepEqual(requests[2], requests[0]);
    assert.equal(JSON.parse(storage.get("screen-decisions")).pending, null);
    assert.equal((await reloaded.undoLast()).restored, 1);
    assert.equal(state.dbData.retry.status, "unseen");
  } finally {
    globalThis.fetch = oldFetch; globalThis.localStorage = oldStorage;
    groupsById.delete("retry"); delete state.dbData.retry;
  }
});

test("a failed Undo remains available for retry", async () => {
  const db = { retryUndo: "unseen" };
  await bulkSet(["retryUndo"], "liked", fakeIo(db, {}));
  assert.equal((await undoLast(fakeIo(db, {}, new Set(["retryUndo"])))).restored, 0);
  assert.equal((await undoLast(fakeIo(db, {}))).restored, 1);
  assert.equal(db.retryUndo, "unseen");
});


test("posting link is outside selection and rejects unsafe URLs", () => {
  const row = (url) => screenRowHtml({ id: "link", locations: [{ url }] }, { t });
  const html = row('https://example.org/job?q="test"');
  assert.match(html, /scr-row-actions[\s\S]*href="https:\/\/example.org\/job\?q=&quot;test&quot;"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.doesNotMatch(row("javascript:alert(1)"), /scr-posting/);
  assert.doesNotMatch(row(""), /scr-posting/);
});


test("Inbox function filters also classify liked roles", async () => {
  const {reviewModel} = await import("./screen.js");
  view.list = "kept"; view.batch = "product"; view.filters = {}; view.page = 0;
  const roles = [{id:"liked-product",title:"Product Manager",status:"liked"},{id:"unseen-product",title:"Product Manager",status:"unseen"}];
  assert.deepEqual(reviewModel(roles, g => g.status).rows.map(g => g.id), ["liked-product"]);
  view.list = "toScreen"; view.batch = null;
});
