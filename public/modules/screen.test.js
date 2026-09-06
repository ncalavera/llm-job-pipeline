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
globalThis.location = { protocol: "file:", origin: "" };

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

// --- A fake write path: a status map, member ids, and a save that can fail --

function fakeIo(db, members, failFor) {
  const saved = [];
  return {
    saved,
    members: (id) => members[id] || [id],
    set: (mid, status) => {
      const prev = db[mid];
      db[mid] = status;
      return prev;
    },
    save: async (mid, status) => {
      saved.push([mid, status]);
      return !(failFor && failFor.has(mid));
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
  // a saved 'liked' first, then was re-saved back to 'unseen'.
  assert.deepEqual(io.saved, [
    ["a", "liked"],
    ["a2", "liked"],
    ["a", "unseen"],
  ]);
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
  assert.match(html, /Org &amp; Co · Berlin/);
  assert.match(html, /scr-row-fact">Run the office\./);
});

test("an empty list renders No roles left in this list.", () => {
  assert.match(screenListHtml([], { t }), /No roles left in this list\./);
});
