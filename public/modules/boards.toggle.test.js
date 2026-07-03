// boards.js — the enabled toggle in FULL/LOCAL mode.
//
// location.protocol is "http:" here (unlike boards.test.js's file://), so
// state.js resolves API_BASE to the page origin and the enabled dot renders as
// an accessible toggle button. globalThis.fetch is stubbed so toggleBoard's
// optimistic-write / revert-on-error path runs with no network. Each test file
// is its own node process, so this http-mode module load is isolated from the
// file:// load in boards.test.js.

import { test } from "node:test";
import assert from "node:assert/strict";

const grid = { innerHTML: "" };
const byId = { boardsGrid: grid };

const catalog = [];

globalThis.document = {
  getElementById: (id) => byId[id] || null,
};
globalThis.window = {
  VACANCY_DATA: {
    config: { i18n: {}, i18n_all: null, language: "en" },
    stats: {},
    vacancy_ids: [],
    groups: [],
    companies: [],
    triage_reviews: [],
    archived_groups: [],
    boards_catalog: catalog,
  },
};
globalThis.location = { protocol: "http:", origin: "http://localhost:8000" };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };

// Stubbed fetch: records calls and answers { ok: fetchOk }.
let fetchCalls = [];
let fetchOk = true;
globalThis.fetch = (url, opts) => {
  fetchCalls.push({ url, opts });
  return Promise.resolve({ ok: fetchOk });
};

const { renderBoards, toggleBoard } = await import("./boards.js");

function boardRow(overrides = {}) {
  return Object.assign(
    {
      id: "board-one",
      name: "Board One",
      audience: "Some audience",
      strategy: "api",
      tier: "B",
      ttl_days: 3,
      url: "https://example.org/board",
      enabled: true,
      last_fetched: "",
    },
    overrides,
  );
}

function setCatalog(next) {
  catalog.length = 0;
  catalog.push(...next);
  fetchCalls = [];
}

test("full mode renders an accessible toggle button (button + aria-pressed reflecting state)", () => {
  setCatalog([
    boardRow({ id: "on", name: "OnBoard", enabled: true }),
    boardRow({ id: "off", name: "OffBoard", enabled: false }),
  ]);
  renderBoards();

  const html = grid.innerHTML;
  assert.ok(html.includes('class="brd-toggle"'), "toggle button renders");
  assert.ok(
    html.includes('type="button"'),
    "it is a real button (keyboard operable)",
  );
  assert.ok(
    html.includes('aria-pressed="true"'),
    "enabled board is aria-pressed true",
  );
  assert.ok(
    html.includes('aria-pressed="false"'),
    "disabled board is aria-pressed false",
  );
  assert.ok(html.includes("toggleBoard("), "click writes through toggleBoard");
  // Interactive mode swaps the CLI hint for the honest what-it-does note.
  assert.ok(html.includes("boards-toggle-note"), "interactive note shown");
  assert.ok(
    !html.includes("boards-cli-hint"),
    "CLI hint hidden in interactive mode",
  );
});

test("toggleBoard optimistically flips, POSTs the exact payload, and keeps it on success", async () => {
  setCatalog([boardRow({ id: "a", name: "Alpha", enabled: false })]);
  fetchOk = true;

  const ok = await toggleBoard("a", true);

  assert.equal(ok, true, "resolves true on a 2xx");
  assert.equal(catalog[0].enabled, true, "catalogue entry stays enabled");
  assert.equal(fetchCalls.length, 1, "exactly one write");
  assert.ok(
    fetchCalls[0].url.endsWith("/api/board-toggle"),
    "posts to the board-toggle endpoint",
  );
  assert.equal(fetchCalls[0].opts.method, "POST");
  assert.deepEqual(JSON.parse(fetchCalls[0].opts.body), {
    board_id: "a",
    enabled: true,
  });
  // Re-render reflects the new state.
  assert.ok(grid.innerHTML.includes('aria-pressed="true"'));
});

test("toggleBoard reverts the optimistic change when the write fails", async () => {
  setCatalog([boardRow({ id: "b", name: "Beta", enabled: true })]);
  fetchOk = false;

  const ok = await toggleBoard("b", false);

  assert.equal(ok, false, "resolves false on a non-2xx");
  assert.equal(
    catalog[0].enabled,
    true,
    "the entry is reverted to its prior state",
  );
  assert.ok(
    grid.innerHTML.includes('aria-pressed="true"'),
    "re-render shows the reverted (enabled) state",
  );
});

test("toggleBoard on an unknown board id is a harmless no-op (no write)", async () => {
  setCatalog([boardRow({ id: "a", enabled: true })]);
  const ok = await toggleBoard("does-not-exist", true);
  assert.equal(ok, false);
  assert.equal(fetchCalls.length, 0, "no write attempted for an unknown board");
});

test("a board id with an apostrophe is safely escaped in the inline onclick", () => {
  setCatalog([boardRow({ id: "women's_board", name: "WWB", enabled: false })]);
  renderBoards();
  // jsAttr escapes the apostrophe so the onclick JS string is not broken.
  assert.ok(
    grid.innerHTML.includes("toggleBoard('women\\'s_board',true)"),
    "the apostrophe is backslash-escaped inside the JS string",
  );
});
